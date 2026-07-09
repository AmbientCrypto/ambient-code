"""Hermetic tests tests.

- a chunked consensus model is priced MAP-ONLY (n_chunks calls per
  model, not n_chunks*2) — consensus reduces with the deterministic
  findings_reducer, which makes NO synthesis LLM call. The old 2x figure
  spuriously refused batches that actually fit the spend ceiling.
- under --repo --consensus the machine-readable plan and the consensus
  gate must AGREE BY CONSTRUCTION — one shared estimate helper feeds both,
  and the plan's model field reflects the consensus SET, not the lone
  default model. An invalid consensus id must never yield a misleading
  plan. A plain (non-consensus) --repo plan is unchanged.
- LOW: skills/ambient/SKILL.md must not imply --consensus and the default
  repo deep pass apply together (the deep pass is skipped under consensus).

Every test patches safe_catalog/run_one_audit/_gate_amount or friends;
no network, no live API, no writes outside tempdirs.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")
SKILL_MD = os.path.join(ROOT, "skills", "ambient", "SKILL.md")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v8cons", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v8cons", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = load_module()

KEY = "sk-test-key-abcdef1234567890"


@contextlib.contextmanager
def patched(obj, **attrs):
    old = {}
    missing = object()
    for k, v in attrs.items():
        old[k] = getattr(obj, k, missing)
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is missing:
                delattr(obj, k)
            else:
                setattr(obj, k, v)


def consensus_catalog():
    tiny = {"context_length": 16000, "max_output_length": 8000,
            "is_ready": True, "supported_features": [],
            "output_modalities": ["text"],
            "pricing": {"input": 0.2, "output": 0.8}}
    return [
        dict(tiny, id="tiny/auditor"),
        dict(tiny, id="tiny/second"),
        {"id": "strong/reduce", "context_length": 200000,
         "max_output_length": 65536, "is_ready": True,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 1.0, "output": 4.0}},
    ]


def audit_args(**kw):
    base = dict(paths=[], staged=False, diff=None, focus=None,
                allow_secrets=False, format="prose", dry_run=False,
                consensus=None, model="tiny/auditor", max_tokens=None,
                temperature=0.1, timeout=30, raw=False, fallback=False,
                allow_partial=False, allow_cost=True, yes=True, no_cache=True,
                cache_ttl=None, parallel=None, reduce_model=None, json=False,
                repo=None, deep=None)
    base.update(kw)
    return argparse.Namespace(**base)


def make_repo(files):
    root = tempfile.mkdtemp()
    for rel, content in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
    return root


def no_git():
    def run(*a, **k):
        raise OSError("git unavailable in this test")
    return types.SimpleNamespace(run=run,
                                 TimeoutExpired=subprocess.TimeoutExpired)


def big_repo():
    # Big enough that tiny/auditor MUST chunk (and tiny/second too).
    return make_repo({
        "src/a.py": "x = 1\n" * 8000,
        "src/b.py": "y = 2\n" * 8000,
    })


def repo_inputs(root):
    with patched(amb, subprocess=no_git()), \
            contextlib.redirect_stderr(io.StringIO()):
        labeled, meta = amb.repo_audit_inputs(audit_args(repo=root), KEY)
    return labeled, meta


CONSENSUS = "tiny/auditor,tiny/second"


# --------------------------------------------------------------------------
# M2 — chunked consensus models are priced MAP-ONLY (no synthesis call)
# --------------------------------------------------------------------------

class TestM2MapOnlyConsensusPricing(unittest.TestCase):
    def test_consensus_estimate_prices_n_chunks_calls_per_model(self):
        catalog = consensus_catalog()
        labeled, _meta = repo_inputs(big_repo())
        total = sum(len(t) for _, t in labeled)
        models = CONSENSUS.split(",")
        seen_calls = {}
        real_est = amb.estimate_cost

        def spy_est(catalog_, model_, input_chars, n_calls, max_tokens):
            seen_calls[model_] = n_calls
            return real_est(catalog_, model_, input_chars, n_calls,
                            max_tokens)

        with patched(amb, estimate_cost=spy_est):
            _exp, _bnd, _parts, per_chunks, _assumed = \
                amb._consensus_estimate(catalog, models, labeled, total)
        for m in models:
            prof = amb.model_profile(catalog, m)
            self.assertGreater(total, prof.single_shot_chars,
                               "test premise: the model must chunk")
            n_chunks = len(amb.pack_chunks(
                labeled, min(prof.chunk_chars, prof.single_shot_chars)))
            self.assertGreater(n_chunks, 1)
            # THE fix: map-only pricing — n_chunks calls, not n_chunks * 2.
            self.assertEqual(seen_calls[m], n_chunks,
                             f"{m}: priced {seen_calls[m]} calls for "
                             f"{n_chunks} chunks — consensus makes NO "
                             "synthesis call")
            self.assertEqual(per_chunks[m], n_chunks)

    def test_fitting_batch_is_no_longer_refused(self):
        """A batch whose real (map-only) cost fits the ceiling must pass the
        REAL gate — the old 2x pricing refused it spuriously."""
        catalog = consensus_catalog()
        root = big_repo()
        labeled, _meta = repo_inputs(root)
        total = sum(len(t) for _, t in labeled)
        models = CONSENSUS.split(",")
        exp, bnd, _parts, per_chunks, _assumed = amb._consensus_estimate(
            catalog, models, labeled, total)
        # What the old bug charged: one extra (synthesis) call per chunk set.
        old = 0.0
        for m in models:
            prof = amb.model_profile(catalog, m)
            n = per_chunks[m]
            est_input = total + n * len(amb.build_code_map(
                labeled, budget=amb.code_map_budget(prof.single_shot_chars)))
            e, _b, _a = amb.estimate_cost(catalog, m, est_input, n * 2,
                                          prof.output_budget)
            old += e
        self.assertLess(exp, old, "map-only must be cheaper than 2x")
        ceiling = (exp + old) / 2  # fits for real cost, refused by 2x cost
        self.assertLessEqual(bnd, ceiling * 3,
                             "test premise: worst-case guard must not fire")
        args = audit_args(repo=root, format="json", consensus=CONSENSUS,
                          allow_cost=False)
        out = io.StringIO()
        env_old = os.environ.get("AMBIENT_MAX_SPEND")
        os.environ["AMBIENT_MAX_SPEND"] = str(ceiling)
        try:
            with patched(amb, safe_catalog=lambda *a, **k: catalog,
                         subprocess=no_git(),
                         run_one_audit=lambda *a, **k: ([], True),
                         _fleet_reserve=lambda *a, **k: None), \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                amb.cmd_audit(args, KEY, "https://x", {})  # must NOT exit
        finally:
            if env_old is None:
                os.environ.pop("AMBIENT_MAX_SPEND", None)
            else:
                os.environ["AMBIENT_MAX_SPEND"] = env_old
        lines = out.getvalue().strip().splitlines()
        env = json.loads("\n".join(lines[1:]))  # after the plan line
        self.assertNotEqual(env.get("status"), "error")


# --------------------------------------------------------------------------
# M1 — the --repo --consensus plan and the consensus gate agree
# --------------------------------------------------------------------------

class TestM1PlanMatchesConsensusGate(unittest.TestCase):
    def _run(self, args, catalog):
        seen = {}

        def spy_gate(expected, args_, conf_, bound=None):
            seen["expected"] = expected
            seen["bound"] = bound

        out, err = io.StringIO(), io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(),
                     run_one_audit=lambda *a, **k: ([], True),
                     _gate_amount=spy_gate), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            amb.cmd_audit(args, KEY, "https://x", {})
        return out.getvalue(), err.getvalue(), seen

    def test_consensus_plan_structure_and_gate_invoked(self):
        # The plan no longer exposes a dollar estimate (founder policy), but the
        # gate must STILL be invoked with a real expected amount, and the plan
        # must carry the consensus structure.
        catalog = consensus_catalog()
        root = big_repo()
        args = audit_args(repo=root, format="json", consensus=CONSENSUS)
        out, _err, seen = self._run(args, catalog)
        plan = json.loads(out.strip().splitlines()[0])
        self.assertEqual(plan["status"], "plan")
        self.assertNotIn("est_cost", plan)     # no dollars in the plan
        self.assertNotIn("est_bound", plan)
        self.assertGreater(seen["expected"], 0)  # gate still charged internally
        # The model field reflects the consensus SET, not the default model.
        self.assertEqual(plan["consensus"], CONSENSUS.split(","))
        for m in CONSENSUS.split(","):
            self.assertIn(m, plan["model"])
        self.assertIs(plan["deep"], False)

    def test_consensus_plan_uses_summed_multi_model_chunks(self):
        catalog = consensus_catalog()
        root = big_repo()
        labeled, _meta = repo_inputs(root)
        total = sum(len(t) for _, t in labeled)
        _exp, _bnd, _p, per_chunks, _a = amb._consensus_estimate(
            catalog, CONSENSUS.split(","), labeled, total)
        args = audit_args(repo=root, format="json", consensus=CONSENSUS)
        out, _err, _seen = self._run(args, catalog)
        plan = json.loads(out.strip().splitlines()[0])
        self.assertNotIn("est_cost", plan)     # no dollars in the plan
        self.assertEqual(plan["n_chunks"], sum(per_chunks.values()))

    def test_plain_repo_plan_unchanged(self):
        catalog = consensus_catalog()
        root = big_repo()
        labeled, _meta = repo_inputs(root)
        total = sum(len(t) for _, t in labeled)
        args = audit_args(repo=root, format="json")

        def fake_mr(*a, **k):
            return ('{"findings": [], "verdict": "SHIP"}', False, "")

        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(), run_map_reduce=fake_mr,
                     complete=lambda *a, **k: (
                         '{"findings": [], "verdict": "SHIP"}',
                         {}, {"finish_reason": "stop"}),
                     log_usage=lambda *a, **k: None,
                     cost_gate_soft=lambda *a, **k: False,
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, KEY, "https://x", {})
        plan = json.loads(out.getvalue().strip().splitlines()[0])
        self.assertEqual(plan["model"], "tiny/auditor")
        self.assertNotIn("consensus", plan)
        self.assertNotIn("est_cost", plan)     # no dollars in the plan
        dens = amb.density_factor(
            "".join(t for _, t in labeled[:8])[:200_000])
        profile = amb.model_profile(catalog, "tiny/auditor")
        n_chunks, _expected, _bound, _assumed = amb._audit_split_estimate(
            catalog, "tiny/auditor", "tiny/auditor", labeled, total,
            int(total * dens), profile, dens, plan_max_tokens(profile),
            True)
        self.assertEqual(plan["n_chunks"], n_chunks)

    def test_invalid_consensus_model_never_yields_misleading_plan(self):
        catalog = consensus_catalog()
        root = big_repo()
        args = audit_args(repo=root, format="json",
                          consensus="tiny/auditor,typo/nonexistent")
        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(),
                     run_one_audit=lambda *a, **k: ([], True),
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                amb.cmd_audit(args, KEY, "https://x", {})
        text = out.getvalue()
        # No plan object may be printed pricing a model set that will be
        # refused — the plan must be honest or absent.
        self.assertNotIn('"status": "plan"', text)
        self.assertNotIn('"status":"plan"', text)
        self.assertIn("unknown consensus model", text)


def plan_max_tokens(profile):
    """The output budget the plan path resolves for the default model —
    mirrors apply_output_budget on a chunked repo run."""
    ns = audit_args(max_tokens=None)
    amb.apply_output_budget(ns, profile, profile.chunk_chars)
    return ns.max_tokens


# --------------------------------------------------------------------------
# LOW — SKILL.md must not imply --consensus + deep pass run together
# --------------------------------------------------------------------------

class TestSkillMdConsensusDeepDrift(unittest.TestCase):
    def test_skill_md_states_deep_pass_skipped_under_consensus(self):
        with open(SKILL_MD, encoding="utf-8") as fh:
            text = fh.read()
        row = next((ln for ln in text.splitlines()
                    if ln.startswith("| `/ambient audit")), None)
        self.assertIsNotNone(row, "SKILL.md audit dispatch row missing")
        low = row.lower()
        self.assertIn("--consensus", row)
        self.assertIn("skip", low,
                      "the audit row must state the deep pass is SKIPPED "
                      "under --consensus")
        self.assertTrue("no effect" in low or "no-op" in low,
                        "the audit row must state --deep/--no-deep have no "
                        "effect with --consensus")


class TestConsensusDryRunPlan(unittest.TestCase):
    """--consensus --dry-run must validate the model set and
    price CONSENSUS (map-only), never the lone default model."""

    def test_dry_run_prices_consensus_map_only(self):
        root = make_repo({"a.py": "def f(x):\n    return x\n"})
        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: consensus_catalog()):
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                amb.cmd_audit(
                    audit_args(repo=root,
                               consensus="tiny/auditor,tiny/second",
                               dry_run=True),
                    "k", "https://x", {})
        s = out.getvalue()
        self.assertIn("consensus: tiny/auditor, tiny/second", s)
        self.assertIn("across 2 model(s)", s)
        self.assertIn("no synthesis", s)  # deterministic merge = map-only

    def test_dry_run_invalid_consensus_id_errors_with_no_plan(self):
        root = make_repo({"a.py": "def f(x):\n    return x\n"})
        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: consensus_catalog()):
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    amb.cmd_audit(
                        audit_args(repo=root,
                                   consensus="tiny/auditor,no-such-model",
                                   dry_run=True),
                        "k", "https://x", {})
        self.assertNotIn("dry run — nothing sent", out.getvalue())


if __name__ == "__main__":
    unittest.main()
