"""tests: the frozen RequestSpec engine request-knob carrier.

Hermetic — every test fakes the transport; no network, no live API. Focus:
(1) RequestSpec is frozen and from_args captures EVERY engine knob with a
default that matches the engine's old getattr(args, X, D) read exactly
(default parity is the #1 regression risk of); (2) every request
variant — retry budget shrink/escalation, --fallback re-derivation, SACRED
_no_fallback workers, best-of cache salts — is a dataclasses.replace that
returns a NEW spec and never mutates the original; (3) the engine runs on a
frozen spec end-to-end, so an in-place attribute write anywhere inside it
would raise FrozenInstanceError instead of silently mutating shared state."""
import argparse
import contextlib
import dataclasses
import importlib.machinery
import importlib.util
import io
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v16", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v16", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = load_module()


def rich_catalog():
    return [
        {"id": "z-ai/glm-5.2", "context_length": 202752,
         "max_output_length": 202752, "is_ready": True,
         "supported_features": ["reasoning", "structured_outputs"],
         "output_modalities": ["text"],
         "pricing": {"input": 0.9, "output": 3.6}},
        {"id": "alt/cheap", "context_length": 300000,
         "max_output_length": 60000, "is_ready": True,
         "supported_features": ["reasoning", "json_mode"],
         "output_modalities": ["text"],
         "pricing": {"input": 0.1, "output": 0.4}},
    ]


def ns(**kw):
    base = dict(max_tokens=8000, temperature=0.1, timeout=30, raw=False,
                fallback=False, allow_partial=False, allow_cost=True, yes=True,
                no_cache=True, cache_ttl=None, model=None, parallel=1,
                escalation_ceiling=30000, _auto_budget=True)
    base.update(kw)
    return argparse.Namespace(**base)


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


def stream_seq(*results):
    calls, deltas = [], []

    def fake(api_url, api_key, payload, timeout, on_delta=None):
        calls.append(payload)
        deltas.append(on_delta)
        r = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    return fake, calls, deltas


def ok_body(content="ok", finish="stop", reasoning=""):
    return (200, {"content": content, "reasoning": reasoning, "usage": None,
                  "finish_reason": finish})


# The default-parity contract: every engine knob and the EXACT default the
# engine used when reading it off a raw Namespace (getattr(args, X, D) → D;
# argparse-owned flags → their add_common_flags default). A mismatch here is
# a silent behavior change — this table is asserted field-for-field below.
EXPECTED_DEFAULTS = {
    "max_tokens": None,             # argparse default (auto-sized later)
    "temperature": 0.1,             # argparse default
    "timeout": 300,                 # argparse default (DEFAULT_TIMEOUT_S)
    "response_format": None,        # getattr(..., None)
    "system": None,                 # getattr(..., None)
    "raw": False,                   # argparse store_true
    "json": False,                  # getattr(..., False)
    "format": None,                 # getattr(..., None) (_json_mode)
    "allow_partial": False,         # getattr(..., False)
    "allow_cost": False,            # getattr(..., False)
    "yes": False,                   # getattr(..., False)
    "parallel": None,               # getattr(..., None)
    "no_cache": False,              # getattr(..., False)
    "cache_ttl": None,              # getattr(..., None)
    "fallback": False,              # getattr(..., False)
    "consensus": None,              # argparse default (ask --consensus)
    "_no_fallback": False,          # getattr(..., False) — SACRED guard
    "_auto_budget": False,          # getattr(..., False)
    "_cache_salt": None,            # getattr(..., None)
    "escalation_ceiling": 65536,    # getattr(..., MAX_AUTO_BUDGET_TOKENS)
    "gate_fallback": True,          # getattr(..., True) — single-call lanes
                                    # re-gate a live --fallback swap; fan-out
                                    # workers set False (batch reserved it)
}


class RequestSpecContractTests(unittest.TestCase):
    """The frozen carrier itself: field set, defaults, from_args."""

    def test_spec_is_frozen(self):
        spec = amb.RequestSpec()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.max_tokens = 123
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec._no_fallback = True

    def test_every_engine_knob_has_the_old_getattr_default(self):
        fields = {f.name: f.default
                  for f in dataclasses.fields(amb.RequestSpec)}
        self.assertEqual(fields, EXPECTED_DEFAULTS)
        # anchor the two constant-backed defaults to the module constants
        self.assertEqual(fields["timeout"], amb.DEFAULT_TIMEOUT_S)
        self.assertEqual(fields["escalation_ceiling"],
                         amb.MAX_AUTO_BUDGET_TOKENS)

    def test_from_args_on_a_bare_namespace_matches_the_getattr_defaults(self):
        # A Namespace with NO knobs must resolve exactly like the engine's
        # old getattr reads did — every field lands on its documented default.
        spec = amb.RequestSpec.from_args(argparse.Namespace())
        for name, want in EXPECTED_DEFAULTS.items():
            self.assertEqual(getattr(spec, name), want, name)

    def test_from_args_captures_every_knob_set_on_the_namespace(self):
        values = dict(
            max_tokens=1234, temperature=0.9, timeout=42,
            response_format={"type": "json_object"}, system="sys",
            raw=True, json=True, format="json", allow_partial=True,
            allow_cost=True, yes=True, parallel=7, no_cache=True,
            cache_ttl=99, fallback=True, consensus="a,b",
            _no_fallback=True, _auto_budget=True, _cache_salt="best-of:3",
            escalation_ceiling=777)
        spec = amb.RequestSpec.from_args(argparse.Namespace(**values))
        for name, want in values.items():
            self.assertEqual(getattr(spec, name), want, name)

    def test_from_args_passes_an_existing_spec_through_untouched(self):
        spec = amb.RequestSpec(max_tokens=55)
        self.assertIs(amb.RequestSpec.from_args(spec), spec)

    def test_replace_returns_a_new_spec_and_leaves_the_original(self):
        spec = amb.RequestSpec.from_args(ns())
        nxt = dataclasses.replace(spec, max_tokens=1, _no_fallback=True)
        self.assertIsNot(nxt, spec)
        self.assertEqual(spec.max_tokens, 8000)
        self.assertFalse(spec._no_fallback)
        self.assertEqual(nxt.max_tokens, 1)
        self.assertTrue(nxt._no_fallback)


class OutputBudgetParityTests(unittest.TestCase):
    """with_output_budget (frozen) must derive EXACTLY what
    apply_output_budget (Namespace, cmd_* boundary) derives — one shared
    core, no drift."""

    def _profile(self):
        return amb.model_profile(rich_catalog(), "z-ai/glm-5.2")

    def test_auto_budget_matches_apply_output_budget(self):
        profile = self._profile()
        for input_chars in (None, 500, 120_000):
            mutable = ns(max_tokens=None)
            amb.apply_output_budget(mutable, profile, input_chars)
            spec = amb.RequestSpec.from_args(
                ns(max_tokens=None)).with_output_budget(profile, input_chars)
            self.assertEqual(spec.max_tokens, mutable.max_tokens, input_chars)
            self.assertEqual(spec._auto_budget, mutable._auto_budget)
            self.assertEqual(spec.escalation_ceiling,
                             mutable.escalation_ceiling)
            self.assertTrue(spec._auto_budget)

    def test_explicit_budget_matches_apply_output_budget(self):
        profile = self._profile()
        for explicit in (500, 9000, 10_000_000):
            mutable = ns(max_tokens=explicit)
            with contextlib.redirect_stderr(io.StringIO()) as err_a:
                amb.apply_output_budget(mutable, profile, 1000)
            base = amb.RequestSpec.from_args(ns(max_tokens=explicit))
            with contextlib.redirect_stderr(io.StringIO()) as err_b:
                spec = base.with_output_budget(profile, 1000)
            self.assertEqual(spec.max_tokens, mutable.max_tokens, explicit)
            self.assertFalse(spec._auto_budget)
            self.assertEqual(spec._auto_budget, mutable._auto_budget)
            self.assertEqual(err_a.getvalue(), err_b.getvalue())
            # the original spec is untouched — a NEW spec came back
            self.assertEqual(base.max_tokens, explicit)

    def test_with_output_budget_never_mutates_the_receiver(self):
        profile = self._profile()
        spec = amb.RequestSpec.from_args(ns(max_tokens=None))
        out = spec.with_output_budget(profile, 1000)
        self.assertIsNot(out, spec)
        self.assertIsNone(spec.max_tokens)


class EngineVariantTests(unittest.TestCase):
    """Every engine variant rides dataclasses.replace: the caller's spec is
    reusable after the call and each variant carries EXACTLY its delta."""

    def setUp(self):
        self._logu = amb.log_usage
        amb.log_usage = lambda *a, **k: None

    def tearDown(self):
        amb.log_usage = self._logu

    def test_attempt_state_carries_a_request_spec(self):
        st = amb.AttemptState(model="m", messages=[],
                              spec=amb.RequestSpec.from_args(ns()))
        self.assertIsInstance(st.spec, amb.RequestSpec)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            st.spec = amb.RequestSpec()

    def test_budget_shrink_variant_leaves_the_callers_spec_reusable(self):
        # First run shrinks the budget on a replaced spec; a SECOND run with
        # the SAME frozen spec must start from the ORIGINAL budget — proof
        # the shrink never leaked back into shared state.
        spec = amb.RequestSpec.from_args(ns())
        for _ in range(2):
            fake, calls, _d = stream_seq(
                (400, {"error": {"message": "max_tokens exceeds limit"}}),
                ok_body("fine"))
            with patched(amb, stream_completion=fake), \
                    contextlib.redirect_stderr(io.StringIO()):
                content, _u, _b = amb.complete("k", "u", "m", [], spec)
            self.assertEqual(content, "fine")
            self.assertEqual([p["max_tokens"] for p in calls], [8000, 4000])
        self.assertEqual(spec.max_tokens, 8000)

    def test_escalation_variant_carries_only_the_budget_delta(self):
        spec = amb.RequestSpec.from_args(ns())
        fake, calls, _d = stream_seq(ok_body(content=""), ok_body("done"))
        with patched(amb, stream_completion=fake), \
                contextlib.redirect_stderr(io.StringIO()):
            content, _u, _b = amb.complete(
                "k", "u", "m", [{"role": "user", "content": "x"}], spec)
        self.assertEqual(content, "done")
        self.assertEqual(calls[1]["max_tokens"], 24384)
        self.assertEqual(calls[0]["temperature"], calls[1]["temperature"])
        self.assertEqual(spec.max_tokens, 8000)  # original untouched

    def test_fallback_variant_rederives_budget_ceiling_and_format(self):
        # The swap must replace exactly max_tokens / escalation_ceiling /
        # response_format for the alt profile — and the strict schema is
        # re-gated to json_mode on a model without structured_outputs.
        rf = {"type": "json_schema",
              "json_schema": {"name": "x", "schema": {"type": "object"}}}
        spec = amb.RequestSpec.from_args(
            ns(fallback=True, response_format=rf, max_tokens=90000,
               _auto_budget=False))
        fake, calls, _d = stream_seq(
            (429, {"error": {"message": "No workers available"}}),
            ok_body("swapped"))
        with patched(amb, stream_completion=fake,
                     fetch_models=lambda *a: rich_catalog(),
                     read_config_file=lambda: {}), \
                contextlib.redirect_stderr(io.StringIO()):
            content, _u, body = amb.complete(
                "k", "u", "z-ai/glm-5.2",
                [{"role": "user", "content": "x"}], spec)
        self.assertEqual(content, "swapped")
        self.assertEqual(calls[1]["model"], "alt/cheap")
        self.assertEqual(calls[1]["response_format"],
                         {"type": "json_object"})
        alt_profile = amb.model_profile(rich_catalog(), "alt/cheap")
        self.assertEqual(calls[1]["max_tokens"],
                         min(90000, alt_profile.max_output_length))
        # the caller's spec still names the ORIGINAL request
        self.assertEqual(spec.response_format, rf)
        self.assertEqual(spec.max_tokens, 90000)

    def test_sacred_no_fallback_spec_blocks_the_swap(self):
        spec = dataclasses.replace(
            amb.RequestSpec.from_args(ns(fallback=True)), _no_fallback=True)
        fake, calls, _d = stream_seq(
            (429, {"error": {"message": "No workers available"}}))
        with patched(amb, stream_completion=fake,
                     fetch_models=lambda *a: rich_catalog(),
                     read_config_file=lambda: {}), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(amb.ChatError) as cm:
                amb.complete("k", "u", "z-ai/glm-5.2",
                             [{"role": "user", "content": "x"}], spec)
        self.assertEqual(cm.exception.category, "model")
        self.assertEqual(len(calls), 1)

    def test_consensus_worker_variant_pins_no_fallback_via_replace(self):
        # _ask_consensus workers must carry _no_fallback=True + a re-derived
        # budget — and the caller's args/spec must come back untouched.
        seen = []
        real_complete = amb.complete

        def spy(api_key, api_url, model, messages, a, **kw):
            seen.append((model, getattr(a, "_no_fallback", False),
                         getattr(a, "_auto_budget", None)))
            return real_complete(api_key, api_url, model, messages, a, **kw)

        fake, calls, _d = stream_seq(ok_body("ans"))
        args = ns(json=True, consensus="z-ai/glm-5.2,alt/cheap",
                  system=None, max_tokens=None)
        with patched(amb, stream_completion=fake, complete=spy,
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            amb._ask_consensus(args, "k", "u", {}, rich_catalog(), "q", "")
        self.assertEqual(sorted(m for m, _nf, _ab in seen),
                         ["alt/cheap", "z-ai/glm-5.2"])
        self.assertTrue(all(nf is True for _m, nf, _ab in seen))
        self.assertTrue(all(ab is True for _m, _nf, ab in seen))
        # the shared base was never poisoned by a worker's pin
        self.assertFalse(getattr(args, "_no_fallback", False))
        self.assertIsNone(args.max_tokens)

    def test_audit_sample_prep_returns_a_new_spec_and_leaves_the_input(self):
        base = amb.RequestSpec.from_args(
            ns(max_tokens=None, _cache_salt="best-of:2"))
        a, sp, single, chunk, total = amb._audit_sample_prep(
            "z-ai/glm-5.2", rich_catalog(), [("a.py", "print(1)\n")],
            "SYS", base)
        self.assertIsInstance(a, amb.RequestSpec)
        self.assertIsNot(a, base)
        self.assertIsNotNone(a.max_tokens)        # budget resolved
        self.assertEqual(a._cache_salt, "best-of:2")  # salt survives
        self.assertIsNone(base.max_tokens)        # input spec untouched
        self.assertIsNone(base.response_format)

    def test_best_of_audit_misses_salts_each_sample_via_replace(self):
        # Each sample's keys must ride its OWN salted spec; the caller's
        # frozen spec goes in as-is (an in-place write would raise) and
        # comes back untouched.
        seen_salts = []
        real_prep = amb._audit_sample_prep

        def spy_prep(model, catalog, labeled, sys_prompt, args):
            seen_salts.append(args._cache_salt)
            return real_prep(model, catalog, labeled, sys_prompt, args)

        spec = amb.RequestSpec.from_args(ns(max_tokens=None))
        with patched(amb, _audit_sample_prep=spy_prep,
                     _cache_get=lambda key, ttl: None):
            plans = amb._best_of_audit_misses(
                rich_catalog(), "z-ai/glm-5.2", [("a.py", "print(1)\n")],
                "SYS", spec, 3, False, None)
        self.assertEqual(seen_salts,
                         ["best-of:0", "best-of:1", "best-of:2"])
        self.assertEqual(len(plans), 3)
        self.assertIsNone(spec._cache_salt)  # caller spec untouched

    def test_map_reduce_synth_variant_is_a_replace_not_a_copy(self):
        # reduce-model path: the synthesis call must ride a spec re-derived
        # for the REDUCE model while the map calls keep the original knobs.
        fake, calls, _d = stream_seq(ok_body("part"))
        spec = amb.RequestSpec.from_args(ns(max_tokens=90000, parallel=1))
        with patched(amb, stream_completion=fake), \
                contextlib.redirect_stderr(io.StringIO()):
            final, partial, _r = amb.run_map_reduce(
                "k", "u", "z-ai/glm-5.2", "map",
                ["c1 " + "a" * 3000, "c2 " + "b" * 3000], spec, "synth",
                2000, reduce_model="alt/cheap", catalog=rich_catalog())
        by_model = {}
        for p in calls:
            by_model.setdefault(p["model"], set()).add(p["max_tokens"])
        alt_profile = amb.model_profile(rich_catalog(), "alt/cheap")
        self.assertEqual(by_model["z-ai/glm-5.2"], {90000})
        self.assertEqual(by_model["alt/cheap"],
                         {min(90000, alt_profile.output_budget)})
        self.assertEqual(spec.max_tokens, 90000)  # caller spec untouched

    def test_engine_runs_end_to_end_on_a_frozen_spec(self):
        # run_one_audit → run_map_reduce → complete entirely on ONE frozen
        # spec: any in-place attribute write left in the engine would raise
        # FrozenInstanceError instead of passing.
        finding = ('{"findings": [{"file": "a.py", "line": 1, "severity": '
                   '"HIGH", "title": "t", "scenario": "s"}], '
                   '"verdict": "NEEDS WORK"}')
        fake, calls, _d = stream_seq(ok_body(finding))
        spec = amb.RequestSpec.from_args(ns(max_tokens=None))
        with patched(amb, stream_completion=fake), \
                contextlib.redirect_stderr(io.StringIO()):
            findings, ok = amb.run_one_audit(
                "z-ai/glm-5.2", rich_catalog(), [("a.py", "print(1)\n")],
                "SYS", spec, "k", "u", {})
        self.assertTrue(ok)
        self.assertEqual(len(findings), 1)
        self.assertIsNone(spec.max_tokens)  # untouched, still auto


if __name__ == "__main__":
    unittest.main()
