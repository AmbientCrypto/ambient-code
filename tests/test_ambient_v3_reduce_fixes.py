"""Hermetic REMEDIATION tests:
- H1: an unknown --reduce-model / stale AMBIENT_MODEL_MAP reduce= entry fails
  with the clean [model] diagnosis BEFORE any map chunk is billed (zero
  complete() calls), prose AND --json envelope; a reduce=auto spec resolves
  to a concrete pick instead of being silently dropped.
- H2: when the reduce model has a SMALLER window than the map model, the
  synthesis inputs are packed to the REDUCE window and the synthesis call
  gets reduce-sized max_tokens — never the map model's.
- the split cost gate uses the explicit formula (map input 1.0x at map
  prices + synthesis input 0.3x at reduce prices + each lane's output) and
  stays byte-identical to the classic gate when map == reduce.
- audit/build --dry-run share the split estimate helper with the live
  gate and display map=/reduce= when they differ.
- a deterministic reducer (structured audit) makes NO synthesis call —
  the gate prices only the map lane, and the reduce model plays no role.
No network, no live API."""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v3rf", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v3rf", loader)
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


def fix_catalog():
    """map/big has a huge window; reduce/tiny a small one; strong/reduce is a
    pricey big-window synthesizer. All READY, all text."""
    return [
        {"id": "map/big", "context_length": 262144,
         "max_output_length": 65536, "is_ready": True,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 0.2, "output": 0.8}},
        {"id": "reduce/tiny", "context_length": 16000,
         "max_output_length": 8000, "is_ready": True,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 0.1, "output": 0.4}},
        {"id": "strong/reduce", "context_length": 200000,
         "max_output_length": 65536, "is_ready": True,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 1.0, "output": 4.0}},
        {"id": "cheap/reason", "context_length": 131072,
         "max_output_length": 32768, "is_ready": True,
         "supported_features": ["reasoning"], "output_modalities": ["text"],
         "pricing": {"input": 0.2, "output": 0.8}},
    ]


def ask_args(**kw):
    base = dict(prompt=["hello", "world"], system=None, model=None,
                max_tokens=None, temperature=0.1, timeout=30, raw=True,
                fallback=False, allow_partial=False, allow_cost=True,
                yes=True, no_cache=True, cache_ttl=None, parallel=None,
                allow_secrets=False, json=False, reduce_model=None)
    base.update(kw)
    return argparse.Namespace(**base)


def mr_args(**kw):
    base = dict(max_tokens=16000, temperature=0.1, timeout=30, raw=False,
                fallback=False, allow_partial=False, allow_cost=True,
                yes=True, no_cache=True, cache_ttl=None, model=None,
                parallel=None, escalation_ceiling=30000, _auto_budget=True,
                reduce_model=None)
    base.update(kw)
    return argparse.Namespace(**base)


def audit_args(path, **kw):
    base = dict(paths=[path], staged=False, diff=None, focus=None,
                allow_secrets=False, format="prose", dry_run=False,
                consensus=None, model=None, max_tokens=None, temperature=0.1,
                timeout=30, raw=False, fallback=False, allow_partial=False,
                allow_cost=True, yes=True, no_cache=True, cache_ttl=None,
                parallel=None, reduce_model=None, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


def build_args(root, **kw):
    base = dict(task=["make", "a", "thing"], dir=root, context=None,
                apply=False, force=False, plan_only=False, dry_run=False,
                max_files=32, max_file_bytes=200_000, no_resume=False,
                json=False, allow_secrets=False, model=None, max_tokens=None,
                temperature=0.1, timeout=30, raw=False, fallback=False,
                allow_partial=True, allow_cost=True, yes=True, no_cache=True,
                cache_ttl=None, parallel=None, reduce_model=None)
    base.update(kw)
    return argparse.Namespace(**base)


class TestH1ValidateReduceBeforeSpend(unittest.TestCase):
    """A bad reduce id must be diagnosed BEFORE the first map chunk bills."""

    def test_unknown_reduce_flag_zero_map_calls_and_clean_error(self):
        seen = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            seen.append(model)
            return ("x", None, {"finish_reason": "stop"})

        # big enough to force the map-reduce path if validation didn't fire
        args = ask_args(model="map/big", reduce_model="reduce/tny",
                        prompt=["y" * 700_000])
        with patched(amb, safe_catalog=lambda *a, **k: fix_catalog(),
                     complete=fake_complete,
                     log_usage=lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_ask(args, "key-abcdef123456", "https://x", {})
        msg = str(cm.exception.code)
        self.assertTrue(msg.startswith("ambient [model]:"), msg)
        self.assertIn("reduce/tiny", msg)      # did-you-mean suggestion
        self.assertIn("billed", msg)           # "nothing was run or billed"
        self.assertEqual(seen, [])             # ZERO map complete() calls

    def test_unknown_reduce_flag_json_envelope(self):
        seen = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            seen.append(model)
            return ("x", None, {"finish_reason": "stop"})

        args = ask_args(model="map/big", reduce_model="reduce/tny",
                        prompt=["y" * 700_000], json=True)
        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: fix_catalog(),
                     complete=fake_complete,
                     log_usage=lambda *a, **k: None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_ask(args, "key-abcdef123456", "https://x", {})
        self.assertEqual(cm.exception.code, 1)
        env = json.loads(out.getvalue())
        self.assertEqual(env["schema_version"], 1)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["category"], "model")
        self.assertIn("reduce/tiny", env["diagnosis"])
        self.assertEqual(seen, [])

    def test_stale_model_map_reduce_entry_fails_clean(self):
        a = argparse.Namespace(model=None, reduce_model=None)
        conf = {"AMBIENT_MODEL_MAP": "reduce=gone/model"}
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.resolve_reduce_model(a, conf, "map/big",
                                         catalog=fix_catalog())
        msg = str(cm.exception.code)
        self.assertTrue(msg.startswith("ambient [model]:"), msg)
        self.assertIn("gone/model", msg)

    def test_reduce_auto_map_entry_resolves_to_concrete_pick(self):
        a = argparse.Namespace(model=None, reduce_model=None)
        conf = {"AMBIENT_MODEL_MAP": "reduce=auto:cheapest"}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            pick = amb.resolve_reduce_model(a, conf, "map/big",
                                            catalog=fix_catalog())
        self.assertEqual(pick, "reduce/tiny")  # cheapest READY by output $
        self.assertIn("reduce/tiny", err.getvalue())  # the pick is printed

    def test_reduce_auto_without_catalog_warns_and_keeps_map_model(self):
        a = argparse.Namespace(model=None, reduce_model=None)
        conf = {"AMBIENT_MODEL_MAP": "reduce=auto"}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            pick = amb.resolve_reduce_model(a, conf, "map/big", catalog=[])
        self.assertEqual(pick, "map/big")
        self.assertIn("auto", err.getvalue())  # explicitly warned, not silent

    def test_explicit_concrete_m_still_pins_whole_run(self):
        # SACRED: the map entry must not reroute (or fail) a -m pinned run.
        a = argparse.Namespace(model="map/big", reduce_model=None)
        conf = {"AMBIENT_MODEL_MAP": "reduce=gone/model"}
        pick = amb.resolve_reduce_model(a, conf, "map/big",
                                        catalog=fix_catalog())
        self.assertEqual(pick, "map/big")

    def test_known_reduce_id_passes_validation(self):
        a = argparse.Namespace(model=None, reduce_model="strong/reduce")
        pick = amb.resolve_reduce_model(a, {}, "map/big",
                                        catalog=fix_catalog())
        self.assertEqual(pick, "strong/reduce")

    def test_degraded_catalog_skips_validation(self):
        # No catalog to check against — behavior must stay pre-fix (the API
        # call diagnoses it later); never a false refusal while offline.
        a = argparse.Namespace(model=None, reduce_model="anything/goes")
        pick = amb.resolve_reduce_model(a, {}, "map/big", catalog=[])
        self.assertEqual(pick, "anything/goes")


class TestH2SynthesisSizedToReduceModel(unittest.TestCase):
    """A smaller-window reduce model gets merge prompts and token budgets
    that fit ITS window, not the map model's."""

    def setUp(self):
        self._logu = amb.log_usage
        amb.log_usage = lambda *a, **k: None

    def tearDown(self):
        amb.log_usage = self._logu

    def test_synthesis_packed_and_budgeted_to_reduce_window(self):
        catalog = fix_catalog()
        map_prof = amb.model_profile(catalog, "map/big")
        red_prof = amb.model_profile(catalog, "reduce/tiny")
        self.assertLess(red_prof.single_shot_chars,
                        map_prof.single_shot_chars)  # test premise
        calls = []

        def fake(api_key, api_url, model, messages, args, **kw):
            calls.append((model, messages[0]["content"],
                          len(messages[1]["content"]), args.max_tokens))
            if "map instructions" in messages[0]["content"]:
                return ("p" * 8000, None, {"finish_reason": "stop"})
            return ("m" * 6000, None, {"finish_reason": "stop"})

        args = mr_args(max_tokens=16000)
        with patched(amb, complete=fake), \
                contextlib.redirect_stderr(io.StringIO()):
            final, partial, _r = amb.run_map_reduce(
                "k", "u", "map/big", "map instructions",
                ["chunk %d" % i for i in range(6)], args,
                "SYNTH merge the parts", map_prof.single_shot_chars,
                reduce_model="reduce/tiny", catalog=catalog)
        self.assertFalse(partial)
        synth = [(m, n, mt) for m, s, n, mt in calls if "SYNTH" in s]
        maps = [(m, mt) for m, s, n, mt in calls
                if "map instructions" in s]
        self.assertTrue(synth)  # synthesis happened, on the reduce model
        for model, n_chars, max_tok in synth:
            self.assertEqual(model, "reduce/tiny")
            # packed to the REDUCE window, not the map model's 660k budget
            self.assertLessEqual(n_chars, red_prof.single_shot_chars)
            # reduce-sized output budget, not the map model's 16000
            self.assertLessEqual(max_tok, red_prof.output_budget)
        for model, max_tok in maps:
            self.assertEqual(model, "map/big")
            self.assertEqual(max_tok, 16000)  # the map lane is untouched

    def test_bigger_reduce_model_changes_nothing(self):
        catalog = fix_catalog()
        map_prof = amb.model_profile(catalog, "reduce/tiny")
        calls = []

        def fake(api_key, api_url, model, messages, args, **kw):
            calls.append((model, messages[0]["content"], args.max_tokens))
            return ("part", None, {"finish_reason": "stop"})

        args = mr_args(max_tokens=4000)
        with patched(amb, complete=fake), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.run_map_reduce(
                "k", "u", "reduce/tiny", "map instructions",
                ["c1", "c2"], args, "SYNTH", map_prof.single_shot_chars,
                reduce_model="strong/reduce", catalog=catalog)
        synth = [(m, mt) for m, s, mt in calls if "SYNTH" in s]
        self.assertEqual(synth, [("strong/reduce", 4000)])  # conservative


class TestM1SplitCostFormula(unittest.TestCase):
    """The split gate must not double-count synthesis input."""

    def _gated(self, catalog, model, reduce_model, chars, n, args,
               **gate_kw):
        got = {}

        def rec(expected, a, conf, bound=None):
            got["expected"], got["bound"] = expected, bound

        with patched(amb, _gate_amount=rec), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cost_gate_mr(catalog, model, reduce_model, chars, n, args,
                             {}, **gate_kw)
        return got

    def test_split_gate_matches_explicit_formula(self):
        catalog = fix_catalog()
        args = mr_args(max_tokens=8000)
        got = self._gated(catalog, "map/big", "strong/reduce", 100_000, 4,
                          args)
        in_tok = 100_000 / amb.CHARS_PER_TOKEN
        eo = min(8000, amb.ANSWER_TOKENS_RESERVE)
        # map input 1.0x at map prices + synth input 0.3x at reduce prices
        # + each lane's own output calls at its own price.
        expected = (in_tok * 1.0 * 0.2 + in_tok * 0.3 * 1.0
                    + 4 * eo * 0.8 + 4 * eo * 4.0) / 1e6
        bound = (in_tok * 1.0 * 0.2 + in_tok * 0.3 * 1.0
                 + 4 * 8000 * 0.8 + 4 * 8000 * 4.0) / 1e6
        self.assertAlmostEqual(got["expected"], expected, places=9)
        self.assertAlmostEqual(got["bound"], bound, places=9)
        # …and it is strictly below the old double-counted figure.
        em, bm, _ = amb.estimate_cost(catalog, "map/big", 100_000, 4, 8000)
        er, br, _ = amb.estimate_cost(catalog, "strong/reduce", 30_000, 4,
                                      8000)
        self.assertLess(got["expected"], em + er)

    def test_same_model_byte_identical_to_classic_gate(self):
        catalog = fix_catalog()
        args = mr_args(max_tokens=8000)
        for rm in (None, "map/big"):
            got = self._gated(catalog, "map/big", rm, 100_000, 4, args)
            classic = {}

            def rec(expected, a, conf, bound=None, _c=classic):
                _c["expected"], _c["bound"] = expected, bound

            with patched(amb, _gate_amount=rec), \
                    contextlib.redirect_stderr(io.StringIO()):
                amb.cost_gate(catalog, "map/big", 100_000, 8, args, {})
            self.assertEqual(got["expected"], classic["expected"])
            self.assertEqual(got["bound"], classic["bound"])

    def test_estimate_helper_same_model_identical_to_estimate_cost(self):
        catalog = fix_catalog()
        split = amb.estimate_cost_mr(catalog, "map/big", "map/big",
                                     100_000, 4, 8000, extra_calls=1)
        classic = amb.estimate_cost(catalog, "map/big", 100_000, 9, 8000)
        self.assertEqual(split, classic)


class TestM3DeterministicReducerPricesMapOnly(unittest.TestCase):
    """reducer=findings_reducer means NO synthesis LLM call — the gate must
    price only the map lane, and the reduce model must not matter."""

    def test_gate_prices_map_lane_only(self):
        catalog = fix_catalog()
        args = mr_args(max_tokens=8000)
        got = {}

        def rec(expected, a, conf, bound=None):
            got["expected"], got["bound"] = expected, bound

        with patched(amb, _gate_amount=rec), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cost_gate_mr(catalog, "map/big", "strong/reduce", 100_000,
                             4, args, {}, synthesis=False)
        in_tok = 100_000 / amb.CHARS_PER_TOKEN
        eo = min(8000, amb.ANSWER_TOKENS_RESERVE)
        expected = (in_tok * 0.2 + 4 * eo * 0.8) / 1e6
        bound = (in_tok * 0.2 + 4 * 8000 * 0.8) / 1e6
        self.assertAlmostEqual(got["expected"], expected, places=9)
        self.assertAlmostEqual(got["bound"], bound, places=9)

    def test_reduce_model_plays_no_role_in_structured_price(self):
        catalog = fix_catalog()
        args = mr_args(max_tokens=8000)
        seen = []

        def rec(expected, a, conf, bound=None):
            seen.append((expected, bound))

        with patched(amb, _gate_amount=rec), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cost_gate_mr(catalog, "map/big", "strong/reduce", 100_000,
                             4, args, {}, synthesis=False)
            amb.cost_gate_mr(catalog, "map/big", None, 100_000,
                             4, args, {}, synthesis=False)
        self.assertEqual(seen[0], seen[1])

    def test_structured_audit_gates_without_synthesis(self):
        # Command-level: the structured audit lane passes synthesis=False.
        catalog = fix_catalog()
        src = os.path.join(tempfile.mkdtemp(), "big.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n" * 20_000)  # ~120k chars > cheap/reason single
        recorded = []
        real = amb.estimate_cost_mr

        def spy(*a, **k):
            recorded.append((a, k))
            return real(*a, **k)

        def fake_mr(*a, **k):
            return ('{"findings": []}', False, "")

        args = audit_args(src, format="json", model="cheap/reason",
                          reduce_model="strong/reduce")
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     estimate_cost_mr=spy, run_map_reduce=fake_mr,
                     _gate_amount=lambda *a, **k: None,
                     log_usage=lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, "key-abcdef123456", "https://x", {})
        self.assertTrue(recorded)
        a, k = recorded[-1]
        self.assertFalse(k.get("synthesis", True))  # deterministic reducer


class TestM2DryRunParity(unittest.TestCase):
    """--dry-run must preview with the SAME split helper as the live gate and
    show map=/reduce= when they differ."""

    def _big_src(self):
        src = os.path.join(tempfile.mkdtemp(), "big.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n" * 20_000)  # ~120k chars
        return src

    def test_audit_dry_run_split_matches_live_gate(self):
        catalog = fix_catalog()
        src = self._big_src()
        recorded = []
        real = amb.estimate_cost_mr

        def spy(*a, **k):
            recorded.append((a, k))
            return real(*a, **k)

        out = io.StringIO()
        args = audit_args(src, dry_run=True, model="cheap/reason",
                          reduce_model="strong/reduce")
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     estimate_cost_mr=spy), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, "key-abcdef123456", "https://x", {})
        text = out.getvalue()
        self.assertIn("map=cheap/reason", text)
        self.assertIn("reduce=strong/reduce", text)
        self.assertEqual(len(recorded), 1)
        dry_a, dry_k = recorded[0]

        # The LIVE gate must compute the same figure from the same inputs.
        gated = {}

        def rec(expected, a, conf, bound=None):
            gated["expected"], gated["bound"] = expected, bound

        live_args = audit_args(src, model="cheap/reason",
                               reduce_model="strong/reduce")
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     _gate_amount=rec,
                     run_map_reduce=lambda *a, **k: ("ok", False, ""),
                     log_usage=lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(live_args, "key-abcdef123456", "https://x", {})
        dry_expected, dry_bound, _ = real(*dry_a, **dry_k)
        self.assertAlmostEqual(gated["expected"], dry_expected, places=9)
        self.assertAlmostEqual(gated["bound"], dry_bound, places=9)

    def test_audit_dry_run_structured_prices_map_lane_only(self):
        catalog = fix_catalog()
        src = self._big_src()
        recorded = []
        real = amb.estimate_cost_mr

        def spy(*a, **k):
            recorded.append((a, k))
            return real(*a, **k)

        args = audit_args(src, dry_run=True, format="json",
                          model="cheap/reason",
                          reduce_model="strong/reduce")
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     estimate_cost_mr=spy), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, "key-abcdef123456", "https://x", {})
        self.assertEqual(len(recorded), 1)
        _a, k = recorded[0]
        self.assertFalse(k.get("synthesis", True))

    def test_build_dry_run_shows_split_and_shares_helper(self):
        catalog = fix_catalog()
        root = tempfile.mkdtemp()
        ctxf = os.path.join(root, "ctx.py")
        with open(ctxf, "w", encoding="utf-8") as fh:
            fh.write("y = 2\n" * 10_000)  # ~60k chars > single//2
        recorded = []
        real = amb.estimate_cost_mr

        def spy(*a, **k):
            recorded.append((a, k))
            return real(*a, **k)

        out = io.StringIO()
        args = build_args(root, dry_run=True, context=[ctxf],
                          model="cheap/reason",
                          reduce_model="strong/reduce")
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     estimate_cost_mr=spy), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_build(args, "key-abcdef123456", "https://x", {})
        text = out.getvalue()
        self.assertIn("map=cheap/reason", text)
        self.assertIn("reduce=strong/reduce", text)
        self.assertEqual(len(recorded), 1)
        a, k = recorded[0]
        # same inputs the live distillation gate uses: (catalog, model,
        # reduce_model, len(context), n_chunks) with the +1 generation call.
        self.assertEqual(a[1], "cheap/reason")
        self.assertEqual(a[2], "strong/reduce")
        self.assertEqual(k.get("extra_calls", 0), 1)

    def test_build_gate_is_fallback_aware(self):
        # the build generation gate + dry-run must price the
        # generation calls via the fallback-aware helper so a --fallback swap to
        # a pricier candidate is reserved up front (byte-identical when off).
        catalog = fix_catalog()
        root = tempfile.mkdtemp()
        seen = []
        real_fb = amb.estimate_cost_fb

        def spy(*a, **k):
            seen.append((a, k))
            return real_fb(*a, **k)

        args = build_args(root, dry_run=True, model="cheap/reason")
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     estimate_cost_fb=spy), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_build(args, "key-abcdef123456", "https://x", {})
        self.assertTrue(
            seen, "build must price generation via the fallback-aware helper")
        # per-call sizing must be threaded: each
        # generation call re-sends the full prompt, so the fallback candidate is
        # sized per-call, never the input_chars/n_calls average.
        self.assertTrue(
            any(k.get("per_call_chars") for _a, k in seen),
            "build must thread per_call_chars so a big prompt's pricier "
            "large-context fallback is reserved")

    def test_dry_run_without_reduce_model_shows_no_reduce_line(self):
        catalog = fix_catalog()
        src = self._big_src()
        out = io.StringIO()
        args = audit_args(src, dry_run=True, model="cheap/reason")
        with patched(amb, safe_catalog=lambda *a, **k: catalog), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, "key-abcdef123456", "https://x", {})
        self.assertNotIn("reduce=", out.getvalue())


if __name__ == "__main__":
    unittest.main()
