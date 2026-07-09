"""Hermetic tests: whole-system spend gating (3 cross-phase HIGHs).

H1: the SINGLE-CALL lanes (normal ask, code's final generation, single-shot
    audit) must cost-gate + FLEET-RESERVE up front, so AMBIENT_MAX_SPEND is
    a true whole-system aggregate ceiling — with parity: no ceiling set and
    non-TTY means a SILENT pass (no refusal, no prompt, no estimate line).
H2: an opt-in --fallback swap must re-price and reserve the attempt at the
    ALT model before the swap — the original gate priced the REQUESTED
    model, so a pricier alt used to spend unreserved dollars.
H3: consensus + best-of estimates must price each worker/sample at the SAME
    resolved max_tokens the live run uses (with_output_budget / a.max_tokens)
    — not the profile default, which under-priced a LARGER explicit
    --max-tokens and weakened the 3x worst-case ceiling guard.

No network, no live API, no writes outside tempdirs.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")

KEY = "sk-test-key-abcdef1234567890"


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v17", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v17", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = load_module()


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


@contextlib.contextmanager
def env_var(name, value):
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


@contextlib.contextmanager
def fleet_dir():
    """Point the fleet machinery at a tmpdir with the fleet lane ON (the
    suite defaults it off for hermeticity) and no ceiling set."""
    with tempfile.TemporaryDirectory() as d:
        with patched(amb, USAGE_PATH=os.path.join(d, "usage.jsonl"),
                     _FLEET_RES_IDS=[]), \
                env_var("AMBIENT_FLEET_BUDGET", None), \
                env_var("AMBIENT_MAX_SPEND", None), \
                env_var("AMBIENT_RESERVATION_TTL", None):
            yield d


def seed(d, records):
    with open(os.path.join(d, "reservations.jsonl"), "w",
              encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def store(d):
    path = os.path.join(d, "reservations.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def rec(amount, pid=None, age=0.0, rid=None):
    return {"pid": pid if pid is not None else os.getpid(),
            "ts": time.time() - age, "amount": amount,
            "id": rid or f"seed-{amount}-{age}"}


def mid_catalog():
    """One priced non-reasoning model: expected for a tiny single call is
    ~0.04-0.09 — small enough to pass a 5 ceiling alone, big enough that a
    seeded 4.99 sibling pushes the fleet total over it."""
    return [
        {"id": "mid/model", "context_length": 120000,
         "max_output_length": 8000, "is_ready": True,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 2.0, "output": 10.0}},
    ]


def fallback_catalog(alt_out_price):
    return [
        {"id": "cheap/asker", "context_length": 120000,
         "max_output_length": 60000, "is_ready": False,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 0.1, "output": 0.4}},
        {"id": "alt/other", "context_length": 300000,
         "max_output_length": 60000, "is_ready": True,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 1.0, "output": alt_out_price}},
    ]


def ask_args(**kw):
    base = dict(prompt=["hello", "world"], system=None, allow_secrets=False,
                json=False, model="mid/model", max_tokens=None,
                temperature=0.7, timeout=30, raw=False, fallback=False,
                allow_partial=False, allow_cost=False, yes=True,
                no_cache=True, cache_ttl=None, parallel=None,
                reduce_model=None, best_of=None, consensus=None)
    base.update(kw)
    return argparse.Namespace(**base)


def code_args(**kw):
    base = dict(task=["write", "a", "thing"], context=[], system=None,
                allow_secrets=False, json=False, model="mid/model",
                max_tokens=None, temperature=0.7, timeout=30, raw=False,
                fallback=False, allow_partial=False, allow_cost=False,
                yes=True, no_cache=True, cache_ttl=None, parallel=None,
                reduce_model=None, best_of=None)
    base.update(kw)
    return argparse.Namespace(**base)


def audit_args(**kw):
    base = dict(paths=[], staged=False, diff=None, focus=None,
                allow_secrets=False, format="prose", dry_run=False,
                consensus=None, model="mid/model", max_tokens=None,
                temperature=0.1, timeout=30, raw=False, fallback=False,
                allow_partial=False, allow_cost=False, yes=True,
                no_cache=True, cache_ttl=None, parallel=None,
                reduce_model=None, json=False, repo=None, deep=None,
                best_of=None)
    base.update(kw)
    return argparse.Namespace(**base)


def cns(**kw):
    """complete()-level namespace (mirrors test_ambient_v2.ns)."""
    base = dict(max_tokens=8000, temperature=0.1, timeout=30, raw=False,
                fallback=False, allow_partial=False, allow_cost=False,
                yes=True, no_cache=True, cache_ttl=None, model=None,
                escalation_ceiling=30000, _auto_budget=False)
    base.update(kw)
    return argparse.Namespace(**base)


def stream_seq(*results):
    calls = []

    def fake(api_url, api_key, payload, timeout, on_delta=None):
        calls.append(payload)
        r = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    return fake, calls


def ok_body(content="ok", finish="stop"):
    return (200, {"content": content, "reasoning": "", "usage": None,
                  "finish_reason": finish})


def no_workers():
    return (429, {"error": {"message": "No workers available"}})


def small_file(d, name="a.py", content="x = 1\n"):
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


# --------------------------------------------------------------------------
# H1 — single-call lanes gate + fleet-reserve up front
# --------------------------------------------------------------------------

class TestH1SingleAskFleetGate(unittest.TestCase):
    def _run_ask(self, args, catalog=None):
        out, err = io.StringIO(), io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog
                     if catalog is not None else mid_catalog(),
                     complete=lambda *a, **k: (
                         "hi", None, {"finish_reason": "stop"}),
                     log_usage=lambda *a, **k: None,
                     read_config_file=lambda: {}), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            amb.cmd_ask(args, KEY, "https://x", {})
        return out.getvalue(), err.getvalue()

    def test_single_ask_writes_and_releases_fleet_reservation(self):
        with fleet_dir() as d:
            self._run_ask(ask_args())
            recs = store(d)
            self.assertEqual(len(recs), 1,
                             "a normal ask must reserve against the fleet")
            self.assertEqual(recs[0]["pid"], os.getpid())
            self.assertGreater(recs[0]["amount"], 0.0)
            amb._fleet_release_all()
            self.assertEqual(store(d), [],
                             "the reservation must be releasable on exit")

    def test_single_ask_refused_when_fleet_near_ceiling(self):
        with fleet_dir() as d:
            seed(d, [rec(4.99)])
            with env_var("AMBIENT_MAX_SPEND", "5"):
                err = io.StringIO()
                with patched(amb, safe_catalog=lambda *a, **k: mid_catalog(),
                             complete=lambda *a, **k: (
                                 "hi", None, {"finish_reason": "stop"}),
                             read_config_file=lambda: {}), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(err), \
                        self.assertRaises(SystemExit) as cm:
                    amb.cmd_ask(ask_args(), KEY, "https://x", {})
            msg = str(cm.exception)
            self.assertIn("already reserved", msg)       # fleet situation named
            self.assertIn("AMBIENT_MAX_SPEND", msg)      # the user's own ceiling
            self.assertNotIn("$", msg)                   # zero dollar figures

    def test_single_ask_silent_pass_with_no_ceiling(self):
        with fleet_dir():
            out, err = self._run_ask(ask_args())
            self.assertIn("hi", out)
            for banned in ("estimated", "Proceed", "ceiling", "reserved"):
                self.assertNotIn(banned, err)
                self.assertNotIn(banned, out)

    def test_allow_cost_skips_refusal_but_still_reserves(self):
        # A7: --allow-cost bypasses the REFUSAL, not the RESERVATION. The run
        # must still be fleet-accounted so concurrent siblings see its spend —
        # guarding the reservation behind `if not allow` let an --allow-cost run
        # silently defeat the aggregate ceiling.
        with fleet_dir() as d:
            seed(d, [rec(4.99)])
            with env_var("AMBIENT_MAX_SPEND", "5"):
                out, _err = self._run_ask(ask_args(allow_cost=True))
            self.assertIn("hi", out)             # not refused (allow bypass)
            self.assertEqual(len(store(d)), 2)   # seeded sibling + THIS run's reservation


class TestH1CodeAndAuditLanes(unittest.TestCase):
    def test_code_generation_gates_before_chat(self):
        order = []

        def spy_gate(catalog, model, input_chars, args, conf):
            order.append(("gate", model, input_chars))

        def spy_chat(api_key, api_url, model, messages, args, kind="ask",
                     session=None):
            order.append(("chat", model,
                          sum(len(m.get("content", "")) for m in messages)))

        with patched(amb, safe_catalog=lambda *a, **k: mid_catalog(),
                     _single_call_gate=spy_gate, chat=spy_chat,
                     read_config_file=lambda: {}), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_code(code_args(), KEY, "https://x", {})
        self.assertEqual([o[0] for o in order], ["gate", "chat"],
                         "the gate must run BEFORE the paid call")
        self.assertEqual(order[0][1], "mid/model")
        # gate prices the ACTUAL input the call sends
        self.assertEqual(order[0][2], order[1][2])

    def test_small_single_shot_audit_is_gated_unconditionally(self):
        seen = []

        def spy_gate(catalog, model, input_chars, args, conf):
            seen.append((model, input_chars))

        with tempfile.TemporaryDirectory() as d:
            path = small_file(d)
            with patched(amb, safe_catalog=lambda *a, **k: mid_catalog(),
                         _single_call_gate=spy_gate,
                         complete=lambda *a, **k: (
                             "no findings", None, {"finish_reason": "stop"}),
                         log_usage=lambda *a, **k: None,
                         read_config_file=lambda: {}), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                amb.cmd_audit(audit_args(paths=[path]), KEY, "https://x", {})
        self.assertEqual(len(seen), 1,
                         "a SMALL single-shot audit must be gated too "
                         "(the old 150k exception is gone)")
        self.assertEqual(seen[0][0], "mid/model")
        self.assertGreater(seen[0][1], 0)

    def test_single_shot_audit_refused_when_fleet_near_ceiling(self):
        with fleet_dir() as d:
            seed(d, [rec(4.99)])
            path = small_file(d)
            with env_var("AMBIENT_MAX_SPEND", "5"):
                with patched(amb, safe_catalog=lambda *a, **k: mid_catalog(),
                             complete=lambda *a, **k: (
                                 "no findings", None,
                                 {"finish_reason": "stop"}),
                             read_config_file=lambda: {}), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as cm:
                    amb.cmd_audit(audit_args(paths=[path]), KEY,
                                  "https://x", {})
            self.assertIn("already reserved", str(cm.exception))


# --------------------------------------------------------------------------
# H2 — the fallback attempt is re-priced + reserved at the ALT model
# --------------------------------------------------------------------------

class TestH2FallbackRepriced(unittest.TestCase):
    def setUp(self):
        self._logu = amb.log_usage
        amb.log_usage = lambda *a, **k: None

    def tearDown(self):
        amb.log_usage = self._logu

    def _complete_with_fallback(self, alt_out_price, seed_amount,
                                allow_cost=False, second=None):
        cat = fallback_catalog(alt_out_price)
        results = [no_workers()] + ([second] if second is not None else [])
        fake, calls = stream_seq(*results)
        with fleet_dir() as d:
            seed(d, [rec(seed_amount)])
            with env_var("AMBIENT_MAX_SPEND", "5"), \
                    patched(amb, stream_completion=fake,
                            fetch_models=lambda *a: cat,
                            pick_fallback_model=lambda *a, **k: "alt/other",
                            read_config_file=lambda: {}), \
                    contextlib.redirect_stderr(io.StringIO()):
                content = amb.complete(
                    "k", "u", "cheap/asker",
                    [{"role": "user", "content": "x"}],
                    cns(fallback=True, allow_cost=allow_cost))[0]
            return content, calls, store(d)

    def test_pricier_fallback_refused_at_fleet_ceiling(self):
        # alt at 40/M out × 6000 expected tokens ≈ 0.24 — pushes a 4.90
        # fleet over the 5 ceiling. The swap must never happen.
        with self.assertRaises(SystemExit) as cm:
            self._complete_with_fallback(40.0, 4.90)
        msg = str(cm.exception)
        self.assertIn("already reserved", msg)
        self.assertNotIn("$", msg)                       # zero dollar figures

    def test_pricier_fallback_never_sends_the_alt_call_when_refused(self):
        cat = fallback_catalog(40.0)
        fake, calls = stream_seq(no_workers(), ok_body("never"))
        with fleet_dir() as d:
            seed(d, [rec(4.90)])
            with env_var("AMBIENT_MAX_SPEND", "5"), \
                    patched(amb, stream_completion=fake,
                            fetch_models=lambda *a: cat,
                            pick_fallback_model=lambda *a, **k: "alt/other",
                            read_config_file=lambda: {}), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                amb.complete("k", "u", "cheap/asker",
                             [{"role": "user", "content": "x"}],
                             cns(fallback=True))
            self.assertEqual(len(calls), 1,
                             "the alt model must never be called unreserved")
            self.assertTrue(all(p["model"] == "cheap/asker" for p in calls))

    def test_cheaper_fallback_passes_and_reserves_the_alt(self):
        content, calls, recs = self._complete_with_fallback(
            0.4, 4.90, second=ok_body("j"))
        self.assertEqual(content, "j")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["model"], "alt/other")
        ours = [r for r in recs if r["id"].startswith(f"{os.getpid()}-")]
        self.assertEqual(len(ours), 1,
                         "the fallback attempt must hold a fleet reservation")
        self.assertGreater(ours[0]["amount"], 0.0)
        amb._fleet_release_all()

    def test_allow_cost_bypasses_the_fallback_gate(self):
        content, calls, recs = self._complete_with_fallback(
            40.0, 4.90, allow_cost=True, second=ok_body("j"))
        self.assertEqual(content, "j")
        self.assertEqual(calls[1]["model"], "alt/other")
        self.assertEqual(len(recs), 1)  # only the seeded sibling


# --------------------------------------------------------------------------
# H3 — consensus + best-of estimates price the EXACT per-worker max_tokens
# --------------------------------------------------------------------------

def h3_catalog():
    tiny = {"context_length": 160000, "max_output_length": 60000,
            "is_ready": True, "supported_features": [],
            "output_modalities": ["text"],
            "pricing": {"input": 0.2, "output": 0.8}}
    return [dict(tiny, id="tiny/auditor"), dict(tiny, id="tiny/second")]


class TestH3ExactWorkerPricing(unittest.TestCase):
    def test_consensus_estimate_uses_explicit_max_tokens(self):
        catalog = h3_catalog()
        labeled = [("a.py", "x = 1\n" * 200)]
        total = sum(len(t) for _, t in labeled)
        models = ["tiny/auditor", "tiny/second"]
        prof = amb.model_profile(catalog, models[0])
        explicit = prof.output_budget + 20_000  # LARGER than the default
        self.assertLess(explicit, 60_000)       # premise: within the cap
        seen = {}
        real_est = amb.estimate_cost

        def spy_est(catalog_, model_, input_chars, n_calls, max_tokens):
            seen[model_] = max_tokens
            return real_est(catalog_, model_, input_chars, n_calls,
                            max_tokens)

        with patched(amb, estimate_cost=spy_est):
            amb._consensus_estimate(catalog, models, labeled, total,
                                    explicit)
        for m in models:
            prof_m = amb.model_profile(catalog, m)
            want = amb.RequestSpec(max_tokens=explicit).with_output_budget(
                prof_m, total).max_tokens
            self.assertEqual(seen[m], want,
                             f"{m}: the estimate must price the SAME "
                             "max_tokens the live worker resolves")
            self.assertGreater(seen[m], prof_m.output_budget)

    def test_consensus_gate_receives_the_explicit_budget(self):
        """cmd_audit must thread the user's explicit --max-tokens into the
        shared consensus estimate (gate site AND plan site)."""
        catalog = h3_catalog()
        seen = {}
        real = amb._consensus_estimate

        def spy(catalog_, models_, labeled_, total_, explicit_mt=None):
            seen["explicit_mt"] = explicit_mt
            return real(catalog_, models_, labeled_, total_, explicit_mt)

        with tempfile.TemporaryDirectory() as d:
            path = small_file(d)
            args = audit_args(paths=[path], allow_cost=True,
                              consensus="tiny/auditor,tiny/second",
                              max_tokens=50_000)
            requested_mt = args.max_tokens  # capture BEFORE cmd_audit clamps args
            with patched(amb, safe_catalog=lambda *a, **k: catalog,
                         _consensus_estimate=spy,
                         run_one_audit=lambda *a, **k: ([], True),
                         read_config_file=lambda: {}), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                amb.cmd_audit(args, KEY, "https://x", {})
        self.assertIsNotNone(seen.get("explicit_mt"),
                             "an explicit --max-tokens must reach the "
                             "consensus estimate")
        # A5: the RAW user --max-tokens reaches the estimate/workers — NOT the
        # default-lane clamp (apply_output_budget mutates args.max_tokens down to
        # the default model's cap, but each consensus member re-derives against
        # its OWN profile, so the request must survive unclamped).
        self.assertEqual(seen["explicit_mt"], requested_mt)  # 50_000, unclamped

    def test_consensus_auto_budget_keeps_profile_default_for_nonreasoners(self):
        catalog = h3_catalog()
        labeled = [("a.py", "x = 1\n" * 200)]
        total = sum(len(t) for _, t in labeled)
        seen = {}
        real_est = amb.estimate_cost

        def spy_est(catalog_, model_, input_chars, n_calls, max_tokens):
            seen[model_] = max_tokens
            return real_est(catalog_, model_, input_chars, n_calls,
                            max_tokens)

        with patched(amb, estimate_cost=spy_est):
            amb._consensus_estimate(catalog, ["tiny/auditor"], labeled,
                                    total)
        prof = amb.model_profile(catalog, "tiny/auditor")
        self.assertEqual(seen["tiny/auditor"], prof.output_budget)

    def test_best_of_miss_plan_carries_resolved_sample_budget(self):
        catalog = h3_catalog()
        labeled = [("a.py", "x = 1\n" * 200)]
        prof = amb.model_profile(catalog, "tiny/auditor")
        explicit = prof.output_budget + 20_000
        spec = amb.RequestSpec.from_args(
            cns(max_tokens=explicit, _auto_budget=False))
        with patched(amb, _cache_get=lambda key, ttl: None):
            plans = amb._best_of_audit_misses(
                catalog, "tiny/auditor", labeled, "SYS", spec, 3, True,
                spec.max_tokens)
        self.assertEqual(len(plans), 3)
        total = sum(len(t) for _, t in labeled)
        want = spec.with_output_budget(prof, total).max_tokens
        for plan in plans:
            self.assertEqual(len(plan), 4,
                             "the miss-plan must carry the resolved "
                             "per-sample max_tokens AND the per-call "
                             "sizes for the fallback-aware gate")
            self.assertEqual(plan[2], want)
            self.assertGreater(plan[2], prof.output_budget)
            self.assertEqual(len(plan[3]), plan[0],
                             "one REAL size per billed call")
            self.assertEqual(sum(plan[3]), plan[1])

    def test_best_of_gate_prices_the_explicit_budget(self):
        catalog = h3_catalog()
        prof = amb.model_profile(catalog, "tiny/auditor")
        explicit = prof.output_budget + 20_000
        gate_tokens = []
        real_est = amb.estimate_cost

        def spy_est(catalog_, model_, input_chars, n_calls, max_tokens):
            gate_tokens.append(max_tokens)
            return real_est(catalog_, model_, input_chars, n_calls,
                            max_tokens)

        with tempfile.TemporaryDirectory() as d:
            path = small_file(d)
            args = audit_args(paths=[path], best_of=2, allow_cost=True,
                              model="tiny/auditor", max_tokens=explicit)
            with patched(amb, safe_catalog=lambda *a, **k: catalog,
                         estimate_cost=spy_est,
                         run_one_audit=lambda *a, **k: ([], True),
                         _gate_amount=lambda *a, **k: None,
                         read_config_file=lambda: {}), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                amb.cmd_audit(args, KEY, "https://x", {})
            total = sum(len(t) for _, t in
                        amb.with_line_gutters(amb.read_files([path])))
        want = amb.RequestSpec(max_tokens=explicit).with_output_budget(
            prof, total).max_tokens
        self.assertTrue(gate_tokens, "the best-of gate must price something")
        for mt in gate_tokens:
            self.assertNotEqual(
                mt, prof.output_budget,
                "the gate must NOT price the profile default when the user "
                "passed a larger explicit --max-tokens")
            self.assertEqual(mt, want)


if __name__ == "__main__":
    unittest.main()
