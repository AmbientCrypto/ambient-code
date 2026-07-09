"""Hermetic tests: advisory routing. The -m auto pseudo-model
(explicit delegation, printed pick), the pre-flight readiness/price ADVISORY
hint (never swaps, never blocks), --reduce-model (cheap-map/strong-reduce),
the AMBIENT_MODEL_MAP per-phase config, and the fit-then-cheapest fallback
ranking. The SACRED invariant is asserted throughout: an explicit concrete
-m model is NEVER changed by any of this. No network, no live API."""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v3route", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v3route", loader)
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


def routing_catalog():
    """Mixed catalog: ready/cold, cheap/pricey, big/tiny context, one
    non-text model — the shapes advisory routing has to reason about."""
    return [
        {"id": "cheap/ready", "context_length": 131072,
         "max_output_length": 32768, "is_ready": True,
         "supported_features": ["reasoning"], "output_modalities": ["text"],
         "pricing": {"input": 0.2, "output": 0.8}},
        {"id": "big/ready", "context_length": 262144,
         "max_output_length": 65536, "is_ready": True,
         "supported_features": ["reasoning"], "output_modalities": ["text"],
         "pricing": {"input": 1.0, "output": 4.0}},
        {"id": "cold/cheapest", "context_length": 200000,
         "max_output_length": 65536, "is_ready": False,
         "supported_features": ["reasoning"], "output_modalities": ["text"],
         "pricing": {"input": 0.05, "output": 0.1}},
        {"id": "tiny/ready", "context_length": 8000,
         "max_output_length": 4000, "is_ready": True,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 0.01, "output": 0.05}},
        {"id": "embed/ready", "context_length": 8192,
         "max_output_length": 8192, "is_ready": True,
         "supported_features": [], "output_modalities": ["embedding"],
         "pricing": {"input": 0.01, "output": 0.01}},
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
    base = dict(max_tokens=8000, temperature=0.1, timeout=30, raw=False,
                fallback=False, allow_partial=False, allow_cost=True,
                yes=True, no_cache=True, cache_ttl=None, model=None,
                parallel=None, escalation_ceiling=30000, _auto_budget=True,
                reduce_model=None)
    base.update(kw)
    return argparse.Namespace(**base)


class TestAutoPseudoModel(unittest.TestCase):
    """3a: -m auto resolves at call time to a REAL model and PRINTS the pick."""

    def test_bare_auto_picks_cheapest_ready_fit_and_prints(self):
        # tiny/ready excluded: it's cheapest but cannot fit this input in one
        # pass; cold/cheapest excluded: no live workers. cheap/ready wins.
        seen = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            seen.append(model)
            return ("answer", None, {"finish_reason": "stop"})

        args = ask_args(model="auto", prompt=["x" * 30_000])
        err = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: routing_catalog(),
                     complete=fake_complete,
                     log_usage=lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            amb.cmd_ask(args, "key-abcdef123456", "https://x", {})
        self.assertEqual(seen, ["cheap/ready"])
        text = err.getvalue()
        self.assertIn("-m auto -> cheap/ready", text)
        self.assertIn("cheapest READY", text)  # the pick reason (no price shown)

    def test_auto_cheapest_and_largest_pick_differently(self):
        cat = routing_catalog()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cheapest = amb.resolve_auto_model(
                "auto:cheapest", cat, {}, input_chars=1000)
            largest = amb.resolve_auto_model(
                "auto:largest", cat, {}, input_chars=1000)
        self.assertEqual(cheapest, "tiny/ready")   # 0.05/M out, fits 1k chars
        self.assertEqual(largest, "big/ready")     # 262k context
        self.assertNotEqual(cheapest, largest)

    def test_auto_never_picks_cold_hidden_or_non_text(self):
        cat = routing_catalog()
        conf = {"AMBIENT_MODELS_HIDE": "tiny/*"}
        with contextlib.redirect_stderr(io.StringIO()):
            pick = amb.resolve_auto_model("auto", cat, conf, input_chars=1000)
        # cold/cheapest (not ready), embed/ready (no text), tiny (hidden by
        # the user's curation) are all skipped.
        self.assertEqual(pick, "cheap/ready")

    def test_auto_nothing_ready_fails_model_category(self):
        cat = [dict(m, is_ready=False) for m in routing_catalog()]
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.resolve_auto_model("auto", cat, {}, input_chars=100)
        self.assertTrue(str(cm.exception.code).startswith("ambient [model]:"))

    def test_auto_nothing_fits_fails_and_lists_ready(self):
        cat = routing_catalog()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.resolve_auto_model("auto", cat, {},
                                       input_chars=10_000_000)
        msg = str(cm.exception.code)
        self.assertTrue(msg.startswith("ambient [model]:"))
        self.assertIn("cheap/ready", msg)  # what IS ready is named

    def test_use_accepts_auto_as_sticky_default(self):
        stored = {}

        def fake_save(updates):
            stored.update(updates)

        args = argparse.Namespace(model_id="auto:cheapest", chat=False,
                                  code=False, all=False)
        with patched(amb, fetch_models=lambda *a: routing_catalog(),
                     save_config_values=fake_save), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_use(args, "key-abcdef123456", "https://x", {})
        self.assertEqual(stored.get("AMBIENT_MODEL"), "auto:cheapest")
        self.assertEqual(stored.get("AMBIENT_CODE_MODEL"), "auto:cheapest")

    def test_sticky_auto_re_resolves_each_call(self):
        # A saved "auto" default (no -m at all) resolves against the LIVE
        # catalog on every call — it stores the literal spec, not a model.
        args = ask_args(model=None)
        conf = {"AMBIENT_MODEL": "auto"}
        with contextlib.redirect_stderr(io.StringIO()):
            model = amb.route_model(args, conf, "chat", routing_catalog(),
                                    input_chars=1000)
        self.assertEqual(model, "tiny/ready")  # cheapest READY that fits


class TestSacredExplicitModel(unittest.TestCase):
    """The invariant that outranks everything: an explicit concrete -m is
    NEVER changed by auto-routing, hints, or the model map."""

    def test_explicit_cold_model_reaches_complete_unchanged(self):
        seen = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            seen.append(model)
            return ("answer", None, {"finish_reason": "stop"})

        args = ask_args(model="cold/cheapest")
        err = io.StringIO()
        with env_var("AMBIENT_MODEL_MAP", "chat=big/ready"), \
                patched(amb, safe_catalog=lambda *a, **k: routing_catalog(),
                        complete=fake_complete,
                        log_usage=lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            amb.cmd_ask(args, "key-abcdef123456", "https://x", {})
        # the EXACT user model reached complete() — cold, hint printed, but
        # never swapped and never blocked.
        self.assertEqual(seen, ["cold/cheapest"])
        self.assertIn("isn't serving right now", err.getvalue())

    def test_preflight_hint_prints_alternatives_but_returns_nothing(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = amb.preflight_hint("cold/cheapest", routing_catalog(), {},
                                     input_chars=100)
        self.assertIsNone(out)  # information only — nothing to act on
        text = err.getvalue()
        self.assertIn("cold/cheapest", text)
        self.assertIn("isn't serving right now", text)
        self.assertIn("cheap/ready", text)   # READY alternatives named…
        self.assertIn("cheap/ready", text)   # alternatives named (no price)

    def test_preflight_hint_silent_for_healthy_model(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            amb.preflight_hint("cheap/ready", routing_catalog(), {},
                               input_chars=100)
        self.assertEqual(err.getvalue(), "")

    def test_preflight_hint_silent_on_degraded_catalog(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            amb.preflight_hint("cold/cheapest", [], {}, input_chars=100)
        self.assertEqual(err.getvalue(), "")


class TestReduceModel(unittest.TestCase):
    """3c: --reduce-model routes the SYNTHESIS call to a different explicit
    model; the up-front gate prices both."""

    def setUp(self):
        self._logu = amb.log_usage
        amb.log_usage = lambda *a, **k: None

    def tearDown(self):
        amb.log_usage = self._logu

    def test_reduce_model_routes_synthesis_call(self):
        calls = []

        def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
            calls.append((model, messages[0]["content"]))
            return ("part", None, {"finish_reason": "stop"})

        with patched(amb, complete=fake), \
                contextlib.redirect_stderr(io.StringIO()):
            final, partial, _r = amb.run_map_reduce(
                "k", "u", "cheap/map", "map instructions",
                ["chunk one", "chunk two"], mr_args(),
                "SYNTH synthesize", 8000, reduce_model="strong/reduce")
        self.assertFalse(partial)
        map_models = [m for m, s in calls if "map instructions" in s]
        synth_models = [m for m, s in calls if "SYNTH" in s]
        self.assertEqual(map_models, ["cheap/map", "cheap/map"])
        self.assertEqual(synth_models, ["strong/reduce"])

    def test_reduce_defaults_to_the_map_model(self):
        calls = []

        def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
            calls.append(model)
            return ("part", None, {"finish_reason": "stop"})

        with patched(amb, complete=fake), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.run_map_reduce("k", "u", "cheap/map", "map",
                               ["c1", "c2"], mr_args(), "SYNTH", 8000)
        self.assertEqual(set(calls), {"cheap/map"})

    def test_cost_gate_prices_map_and_reduce_models(self):
        cat = routing_catalog()
        gated = {}

        def fake_gate(expected, args, conf, bound=None):
            gated["expected"] = expected
            gated["bound"] = bound

        args = mr_args()
        with patched(amb, _gate_amount=fake_gate), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cost_gate_mr(cat, "cheap/ready", "big/ready", 100_000, 4,
                             args, {})
        # Explicit split formula (the old sum of two 1.3x
        # single-model estimates double-counted the synthesis re-read):
        # map input 1.0x at map prices + synth input 0.3x at reduce prices
        # + each lane's own output calls at its own price.
        in_tok = 100_000 / amb.CHARS_PER_TOKEN
        eo = min(args.max_tokens, amb.ANSWER_TOKENS_RESERVE)
        mp = amb.model_pricing(cat, "cheap/ready")
        rp = amb.model_pricing(cat, "big/ready")
        input_cost = in_tok * 1.0 * mp[0] + in_tok * 0.3 * rp[0]
        expected = (input_cost + 4 * eo * mp[1] + 4 * eo * rp[1]) / 1e6
        bound = (input_cost + 4 * args.max_tokens * mp[1]
                 + 4 * args.max_tokens * rp[1]) / 1e6
        self.assertAlmostEqual(gated["expected"], expected, places=9)
        self.assertAlmostEqual(gated["bound"], bound, places=9)

    def test_cost_gate_same_model_matches_classic_gate(self):
        cat = routing_catalog()
        seen = []

        def fake_gate(expected, args, conf, bound=None):
            seen.append((expected, bound))

        args = mr_args()
        with patched(amb, _gate_amount=fake_gate), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cost_gate_mr(cat, "cheap/ready", None, 100_000, 4, args, {})
            amb.cost_gate(cat, "cheap/ready", 100_000, 8, args, {})
        self.assertEqual(seen[0], seen[1])

    def test_reduce_model_flag_exists_on_task_subparsers(self):
        p = argparse.ArgumentParser()
        amb.add_common_flags(p)
        args = p.parse_args(["--reduce-model", "strong/reduce"])
        self.assertEqual(args.reduce_model, "strong/reduce")


class TestModelMap(unittest.TestCase):
    """3d: AMBIENT_MODEL_MAP is the USER's per-phase routing config."""

    def test_map_sets_phase_default_when_unset(self):
        args = argparse.Namespace(model=None)
        with env_var("AMBIENT_MODEL_MAP",
                     "chat=map/chat-model,code=map/code-model"), \
                env_var("AMBIENT_MODEL", None), \
                env_var("AMBIENT_CODE_MODEL", None):
            self.assertEqual(amb.resolve_model(args, {}, "chat"),
                             "map/chat-model")
            self.assertEqual(amb.resolve_model(args, {}, "code"),
                             "map/code-model")

    def test_explicit_m_always_overrides_the_map(self):
        args = argparse.Namespace(model="explicit/pick")
        with env_var("AMBIENT_MODEL_MAP", "chat=map/chat-model"):
            self.assertEqual(amb.resolve_model(args, {}, "chat"),
                             "explicit/pick")

    def test_unset_map_keeps_todays_defaults(self):
        args = argparse.Namespace(model=None)
        with env_var("AMBIENT_MODEL_MAP", None), \
                env_var("AMBIENT_MODEL", None), \
                env_var("AMBIENT_CODE_MODEL", None):
            self.assertEqual(amb.resolve_model(args, {}, "chat"),
                             amb.DEFAULT_MODEL)
            self.assertEqual(amb.resolve_model(args, {}, "code"),
                             amb.DEFAULT_CODE_MODEL)

    def test_map_phase_used_by_ambient_map_command(self):
        args = argparse.Namespace(model=None)
        with env_var("AMBIENT_MODEL_MAP",
                     "map=bulk/model,chat=chatty/model"), \
                env_var("AMBIENT_MODEL", None):
            self.assertEqual(
                amb.resolve_model(args, {}, "chat", phase="map"),
                "bulk/model")

    def test_reduce_phase_consulted_and_flag_overrides(self):
        conf = {"AMBIENT_MODEL_MAP": "reduce=strong/reduce"}
        # no explicit models: the map's reduce phase applies
        a = argparse.Namespace(model=None, reduce_model=None)
        self.assertEqual(amb.resolve_reduce_model(a, conf, "cheap/map"),
                         "strong/reduce")
        # --reduce-model beats the map
        a = argparse.Namespace(model=None, reduce_model="flag/wins")
        self.assertEqual(amb.resolve_reduce_model(a, conf, "cheap/map"),
                         "flag/wins")
        # an explicit concrete -m pins the WHOLE run (sacred): the map's
        # reduce entry must not reroute the synthesis behind the user's back
        a = argparse.Namespace(model="explicit/pick", reduce_model=None)
        self.assertEqual(amb.resolve_reduce_model(a, conf, "explicit/pick"),
                         "explicit/pick")

    def test_junk_map_never_crashes(self):
        args = argparse.Namespace(model=None)
        with env_var("AMBIENT_MODEL_MAP", ",,=,garbage,x=,=y"), \
                env_var("AMBIENT_MODEL", None):
            self.assertEqual(amb.resolve_model(args, {}, "chat"),
                             amb.DEFAULT_MODEL)


class TestFallbackFitThenCheapest(unittest.TestCase):
    """3e: among FITTING candidates the fallback picks the CHEAPEST, not the
    biggest — only ever inside the opt-in --fallback lane."""

    def _cat(self):
        return [
            {"id": "dead/model", "context_length": 500000,
             "max_output_length": 65536, "is_ready": False,
             "supported_features": ["reasoning"],
             "output_modalities": ["text"],
             "pricing": {"input": 0.1, "output": 0.2}},
            {"id": "huge/pricey", "context_length": 400000,
             "max_output_length": 65536, "is_ready": True,
             "supported_features": ["reasoning"],
             "output_modalities": ["text"],
             "pricing": {"input": 2.0, "output": 8.0}},
            {"id": "fits/cheap", "context_length": 120000,
             "max_output_length": 32768, "is_ready": True,
             "supported_features": ["reasoning"],
             "output_modalities": ["text"],
             "pricing": {"input": 0.2, "output": 0.8}},
        ]

    def test_cheapest_fitting_wins_over_biggest(self):
        pick = amb.pick_fallback_model(self._cat(), "dead/model",
                                       min_context=50_000, conf={})
        self.assertEqual(pick, "fits/cheap")

    def test_too_small_context_still_excluded(self):
        pick = amb.pick_fallback_model(self._cat(), "dead/model",
                                       min_context=200_000, conf={})
        self.assertEqual(pick, "huge/pricey")

    def test_none_when_nothing_fits(self):
        pick = amb.pick_fallback_model(self._cat(), "dead/model",
                                       min_context=999_999_999, conf={})
        self.assertIsNone(pick)


if __name__ == "__main__":
    unittest.main()
