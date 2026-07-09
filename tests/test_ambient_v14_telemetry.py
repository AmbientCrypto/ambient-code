"""Hermetic tests: (1) the declarative command registry that
replaces main()'s dual dispatch (pre-credential if-chain + post-credential
handlers dict) with ONE table, and (2) telemetry-EWMA routing — per-model
observed chars-per-token from the usage ledger blended into model_profile /
estimate_cost, byte-identical to the static constants when there is no
history. No network, no live API, no writes outside tempdirs."""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_p8a", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_p8a", loader)
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


class NotATTY(io.StringIO):
    def isatty(self):
        return False


def _poisoned_load_config():
    raise AssertionError("load_config must NOT be called for a key-less command")


CATALOG = [
    {"id": "m-reason", "context_length": 202752, "max_output_length": 131072,
     "supported_features": ["reasoning"],
     "pricing": {"prompt": 0.2, "completion": 1.0}},
    {"id": "m-plain", "context_length": 32768, "max_output_length": 8192,
     "supported_features": [],
     "pricing": {"prompt": 0.1, "completion": 0.5}},
    # Output-capped reasoner: single_shot is bound by max_output (not the
    # global cost knob), so an observed chars/token shift is visible.
    {"id": "m-mid", "context_length": 60000, "max_output_length": 16384,
     "supported_features": ["reasoning"],
     "pricing": {"prompt": 0.2, "completion": 1.0}},
]


@contextlib.contextmanager
def temp_ledger(records=None):
    """Point USAGE_PATH at a temp file seeded with `records`, reset the
    per-process telemetry memo around the block, and explicitly ENABLE
    telemetry (tests/__init__.py defaults the suite to AMBIENT_TELEMETRY=off
    so exact-value tests can never depend on the developer's real ledger)."""
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "usage.jsonl")
        if records is not None:
            with open(path, "w", encoding="utf-8") as fh:
                for rec in records:
                    fh.write(json.dumps(rec) + "\n")
        with patched(amb, USAGE_PATH=path, _TELEMETRY_CPT_CACHE=None), \
                patched(amb.os, environ={**os.environ,
                                         "AMBIENT_TELEMETRY": "on"}):
            yield path
        amb._TELEMETRY_CPT_CACHE = None


def rec(model="m-reason", chars=3200, in_tok=1000, **kw):
    r = {"ts": 1, "model": model, "in": in_tok, "out": 50, "chars": chars}
    r.update(kw)
    return r


# ---------------------------------------------------------------------------
# 8a-1: declarative command registry
# ---------------------------------------------------------------------------

ALL_COMMANDS = {"version", "models", "curate", "setup", "link", "cache",
                "trust-url", "usage", "mode", "config", "settings", "doctor",
                "use", "ask", "audit", "map", "code", "chat", "build", "agent",
                "codex"}
KEYED = {"use", "ask", "audit", "map", "code", "chat", "build", "agent"}


class TestCommandRegistry(unittest.TestCase):
    def test_registry_covers_every_command_once(self):
        names = [s["name"] for s in amb.COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), ALL_COMMANDS)

    def test_registry_entries_are_complete(self):
        for spec in amb.COMMANDS:
            self.assertIn("needs_key", spec, spec["name"])
            self.assertTrue(callable(spec["configure"]), spec["name"])
            self.assertIsInstance(spec["handler"], str, spec["name"])
            self.assertTrue(hasattr(amb, spec["handler"]), spec["name"])

    def test_key_gating_matches_the_contract(self):
        gating = {s["name"]: s["needs_key"] for s in amb.COMMANDS}
        for name in ALL_COMMANDS:
            self.assertEqual(gating[name], name in KEYED, name)

    def test_every_keyed_command_dispatches_with_credentials(self):
        for name, argv in [
            ("ask", ["ask", "hi"]),
            ("audit", ["audit", "x.py"]),
            ("map", ["map", "p", "f.py"]),
            ("code", ["code", "t"]),
            ("chat", ["chat"]),
            ("build", ["build", "t"]),
            ("agent", ["agent"]),
            ("use", ["use", "m-x"]),
        ]:
            seen = {}

            def stub(args, api_key, api_url, conf, _n=name, _s=seen):
                _s["cmd"] = _n
                _s["key"] = api_key

            with patched(amb, **{
                    "load_config": lambda: (KEY, "https://x", {}),
                    "cmd_" + name: stub}), \
                    patched(sys, argv=["ambient"] + argv), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                amb.main()
            self.assertEqual(seen.get("cmd"), name)
            self.assertEqual(seen.get("key"), KEY)

    def test_keyless_commands_never_touch_load_config(self):
        cases = [
            ("mode", ["mode"], "cmd_mode"),
            ("doctor", ["doctor"], "cmd_doctor"),
            ("usage", ["usage"], "cmd_usage"),
            ("link", ["link"], "cmd_link"),
            ("cache", ["cache"], "cmd_cache"),
            ("curate", ["curate"], "cmd_curate"),
            ("setup", ["setup"], "cmd_setup"),
            # `settings` is a keyless alias of `config` (same cmd_config handler);
            # dispatch must never reach load_config for it either.
            ("settings", ["settings"], "cmd_config"),
        ]
        for name, argv, fn in cases:
            seen = {}
            with patched(amb, **{
                    "load_config": _poisoned_load_config,
                    fn: lambda args, _s=seen, _n=name: _s.update(cmd=_n)}), \
                    patched(sys, argv=["ambient"] + argv), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                amb.main()
            self.assertEqual(seen.get("cmd"), name)

    def test_codex_is_keyless_and_keeps_its_signature(self):
        seen = {}
        with patched(amb,
                     load_config=_poisoned_load_config,
                     cmd_codex=lambda args, k, u, c, _s=seen:
                     _s.update(key=k, url=u, conf=c)), \
                patched(sys, argv=["ambient", "codex"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.main()
        self.assertEqual(seen, {"key": None, "url": None, "conf": None})

    def test_models_is_keyless_with_optional_key(self):
        seen = {}
        with patched(amb,
                     load_config=_poisoned_load_config,
                     read_config_file=lambda: {},
                     resolve_key_and_backend=lambda conf: (None, "none"),
                     resolve_api_url=lambda conf: "https://api.x",
                     cmd_models=lambda args, k, u, c, _s=seen:
                     _s.update(key=k, url=u)), \
                patched(sys, argv=["ambient", "models"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.main()
        self.assertEqual(seen.get("key"), "none")
        self.assertEqual(seen.get("url"), "https://api.x")

    def test_trust_url_dispatch_reset_url_and_usage_error(self):
        seen = {}
        with patched(amb, load_config=_poisoned_load_config,
                     cmd_trust_url_reset=lambda _s=seen: _s.update(r=True)), \
                patched(sys, argv=["ambient", "trust-url", "--reset"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.main()
        self.assertTrue(seen.get("r"))
        seen = {}
        with patched(amb, load_config=_poisoned_load_config,
                     cmd_trust_url=lambda args, _s=seen:
                     _s.update(url=args.url)), \
                patched(sys, argv=["ambient", "trust-url", "https://g.x"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.main()
        self.assertEqual(seen.get("url"), "https://g.x")
        with patched(amb, load_config=_poisoned_load_config), \
                patched(sys, argv=["ambient", "trust-url"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.main()
        self.assertEqual(cm.exception.code, amb.EXIT_USAGE)

    def test_audit_hook_management_needs_no_key(self):
        seen = {}
        with patched(amb, load_config=_poisoned_load_config,
                     cmd_audit_hook=lambda args, _s=seen:
                     _s.update(hook=args.install_hook)), \
                patched(sys, argv=["ambient", "audit", "--install-hook"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.main()
        self.assertEqual(seen.get("hook"), "pre-commit")

    def test_needs_key_command_exits_3_unconfigured(self):
        with tempfile.TemporaryDirectory() as td:
            with patched(amb,
                         CONFIG_PATH=os.path.join(td, "env"),
                         resolve_key_and_backend=lambda conf: (None, "none"),
                         cmd_ask=lambda *a: (_ for _ in ()).throw(
                             AssertionError("handler ran without a key"))), \
                    patched(amb.sys, stdin=NotATTY(), stdout=NotATTY(),
                            stderr=NotATTY()), \
                    patched(sys, argv=["ambient", "ask", "hi"]):
                with self.assertRaises(SystemExit) as cm:
                    amb.main()
        self.assertEqual(cm.exception.code, 3)

    def test_typo_gets_did_you_mean_and_exit_64(self):
        err = io.StringIO()
        with patched(sys, argv=["ambient", "asx", "hi"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                amb.main()
        self.assertEqual(cm.exception.code, amb.EXIT_USAGE)
        self.assertIn("did you mean: ask", err.getvalue())

    def test_unknown_command_exits_64(self):
        with patched(sys, argv=["ambient", "zzznothing"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.main()
        self.assertEqual(cm.exception.code, amb.EXIT_USAGE)

    def test_help_lists_every_command_and_exits_0(self):
        out = io.StringIO()
        with patched(sys, argv=["ambient", "--help"]), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                amb.main()
        self.assertEqual(cm.exception.code, 0)
        text = out.getvalue()
        for name in ALL_COMMANDS:
            self.assertIn(name, text)

    def test_subcommand_help_exits_0(self):
        for name in ("ask", "audit", "map", "usage"):
            out = io.StringIO()
            with patched(sys, argv=["ambient", name, "--help"]), \
                    contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as cm:
                    amb.main()
            self.assertEqual(cm.exception.code, 0, name)
            self.assertIn(name, out.getvalue())

    def test_bare_ambient_prints_banner(self):
        out = io.StringIO()
        with patched(amb, build_banner=lambda: "BANNER-P8A"), \
                patched(sys, argv=["ambient"]), \
                contextlib.redirect_stdout(out):
            amb.main()
        self.assertIn("BANNER-P8A", out.getvalue())

    def test_version_runs_before_the_home_check(self):
        # `ambient version` must keep working even when HOME is unresolvable
        # (CONFIG_PATH still contains '~'), exactly as before the registry.
        out = io.StringIO()
        with patched(amb, CONFIG_PATH="~/unresolved/env"), \
                patched(sys, argv=["ambient", "version"]), \
                contextlib.redirect_stdout(out):
            amb.main()
        self.assertIn(amb.__version__, out.getvalue())
        # ...while a normal command still gets the config failure.
        with patched(amb, CONFIG_PATH="~/unresolved/env",
                     load_config=_poisoned_load_config), \
                patched(sys, argv=["ambient", "doctor"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                amb.main()


# ---------------------------------------------------------------------------
# 8a-2: telemetry-EWMA routing
# ---------------------------------------------------------------------------

class TestObservedCpt(unittest.TestCase):
    def test_none_without_ledger(self):
        with temp_ledger(records=None):
            self.assertIsNone(amb.observed_cpt("m-reason"))

    def test_none_for_model_without_history(self):
        with temp_ledger([rec(model="m-reason")]):
            self.assertIsNone(amb.observed_cpt("m-plain"))

    def test_observed_ratio_returned(self):
        with temp_ledger([rec(chars=6400, in_tok=1000)] * 3):
            v = amb.observed_cpt("m-reason")
            self.assertAlmostEqual(v, 6.4, places=3)

    def test_ewma_is_recent_weighted(self):
        old = [rec(chars=2000, in_tok=1000)] * 5   # ratio 2.0, older
        new = [rec(chars=4000, in_tok=1000)] * 5   # ratio 4.0, newer
        with temp_ledger(old + new):
            recent_high = amb.observed_cpt("m-reason")
        with temp_ledger(new + old):
            recent_low = amb.observed_cpt("m-reason")
        self.assertGreater(recent_high, 3.0)
        self.assertLess(recent_low, 3.0)
        self.assertGreater(recent_high, recent_low)

    def test_ratio_clamped_to_sane_range(self):
        with temp_ledger([rec(chars=500_000, in_tok=1000)]):
            self.assertLessEqual(amb.observed_cpt("m-reason"),
                                 amb.TELEMETRY_CPT_MAX)
        with temp_ledger([rec(chars=50, in_tok=1000)]):
            self.assertGreaterEqual(amb.observed_cpt("m-reason"),
                                    amb.TELEMETRY_CPT_MIN)

    def test_corrupt_and_estimated_records_ignored(self):
        rows = [rec(est=True),                      # estimated → not real usage
                {"model": "m-reason", "in": 0, "chars": 100},   # zero tokens
                {"model": "m-reason", "in": 100},               # no chars
                "not-a-dict"]
        with temp_ledger(rows) as path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("{corrupt json\n")
            self.assertIsNone(amb.observed_cpt("m-reason"))

    def test_telemetry_off_ignores_history(self):
        with temp_ledger([rec(chars=6400, in_tok=1000)] * 3):
            with patched(amb.os, environ={**os.environ,
                                          "AMBIENT_TELEMETRY": "off"}):
                self.assertIsNone(amb.observed_cpt("m-reason"))

    def test_memoized_no_reread_per_call(self):
        with temp_ledger([rec(chars=6400, in_tok=1000)]) as path:
            first = amb.observed_cpt("m-reason")
            os.unlink(path)  # a re-read would now find nothing
            self.assertEqual(amb.observed_cpt("m-reason"), first)
            amb._TELEMETRY_CPT_CACHE = None  # explicit reset → fresh read
            self.assertIsNone(amb.observed_cpt("m-reason"))


class TestTelemetryBlending(unittest.TestCase):
    def test_estimate_cost_byte_identical_with_no_history(self):
        with temp_ledger(records=None):
            got = amb.estimate_cost(CATALOG, "m-reason", 123457, 3, 20000)
        # The exact pre-telemetry formula, from the static constant:
        price = amb.model_pricing(CATALOG, "m-reason") or (
            amb.ASSUMED_MAX_INPUT_PRICE, amb.ASSUMED_MAX_OUTPUT_PRICE)
        in_tok = 123457 / amb.CHARS_PER_TOKEN
        input_cost = in_tok * 1.3 * price[0]
        bound = (input_cost + 3 * 20000 * price[1]) / 1e6
        expected_out = min(20000, amb.ANSWER_TOKENS_RESERVE)
        expected = (input_cost + 3 * expected_out * price[1]) / 1e6
        self.assertEqual(got[0], expected)
        self.assertEqual(got[1], bound)

    def test_model_profile_byte_identical_with_no_history(self):
        with temp_ledger(records=None):
            fresh = amb.model_profile(CATALOG, "m-mid")
        with temp_ledger([rec(model="m-mid", chars=6400, in_tok=1000)] * 3):
            with patched(amb.os, environ={**os.environ,
                                          "AMBIENT_TELEMETRY": "off"}):
                off = amb.model_profile(CATALOG, "m-mid")
        self.assertEqual(tuple(fresh), tuple(off))

    def test_seeded_history_only_tightens_the_cost_gate(self):
        # Telemetry may make the COST estimate MORE conservative, never less: a
        # higher observed cpt (fewer tokens/char) must NOT lower the gate, or a
        # run the static gate would block could slip through.
        args = (CATALOG, "m-reason", 200_000, 3, 20000)
        with temp_ledger(records=None):
            base = amb.estimate_cost(*args)
        # Observed ~6.4 chars/token is CLAMPED to the default for cost — the
        # estimate does NOT drop below the static baseline.
        with temp_ledger([rec(chars=6400, in_tok=1000)] * 3):
            high_cpt = amb.estimate_cost(*args)
        self.assertEqual(high_cpt[0], base[0])
        # Observed ~2.0 chars/token (a model that really uses MORE tokens per
        # char) RAISES the estimate — telemetry still tightens the gate.
        with temp_ledger([rec(chars=2000, in_tok=1000)] * 3):
            low_cpt = amb.estimate_cost(*args)
        self.assertGreater(low_cpt[0], base[0])
        # ...and a model with NO history is untouched.
        with temp_ledger([rec(chars=2000, in_tok=1000)] * 3):
            other = amb.estimate_cost(CATALOG, "m-plain", 200_000, 3, 8000)
        with temp_ledger(records=None):
            other_base = amb.estimate_cost(CATALOG, "m-plain", 200_000, 3, 8000)
        self.assertEqual(other, other_base)

    def test_seeded_history_shifts_that_models_profile(self):
        with temp_ledger(records=None):
            base = amb.model_profile(CATALOG, "m-mid")
        with temp_ledger([rec(model="m-mid", chars=6400, in_tok=1000)] * 3):
            tuned = amb.model_profile(CATALOG, "m-mid")
        # More observed chars/token → the same token window holds MORE chars.
        self.assertGreater(tuned.single_shot_chars, base.single_shot_chars)

    def test_telemetry_off_keeps_static_estimates(self):
        args = (CATALOG, "m-reason", 200_000, 3, 20000)
        with temp_ledger(records=None):
            base = amb.estimate_cost(*args)
        with temp_ledger([rec(chars=6400, in_tok=1000)] * 3):
            with patched(amb.os, environ={**os.environ,
                                          "AMBIENT_TELEMETRY": "off"}):
                off = amb.estimate_cost(*args)
        self.assertEqual(base, off)

    def test_cost_cpt_clamped_to_default_while_sizing_cpt_is_not(self):
        # The spend-gate cpt can only tighten (<= default); the SIZING cpt is
        # free to grow so budgets right-size (gate must never
        # under-price even when a model observes fewer tokens per char).
        with temp_ledger([rec(model="m-reason", chars=6400, in_tok=1000)] * 3):
            self.assertLessEqual(amb._cost_cpt("m-reason"), amb.CHARS_PER_TOKEN)
            self.assertGreater(amb._effective_cpt("m-reason"),
                               amb.CHARS_PER_TOKEN)
        # A model that really uses MORE tokens per char tightens BOTH.
        with temp_ledger([rec(model="m-reason", chars=2000, in_tok=1000)] * 3):
            self.assertLess(amb._cost_cpt("m-reason"), amb.CHARS_PER_TOKEN)

    def test_estimate_cost_mr_stays_identical_to_gate_when_models_match(self):
        with temp_ledger([rec(chars=6400, in_tok=1000)] * 3):
            mr = amb.estimate_cost_mr(CATALOG, "m-reason", "m-reason",
                                      99991, 4, 16000)
            classic = amb.estimate_cost(CATALOG, "m-reason", 99991,
                                        4 * 2, 16000)
        self.assertEqual(mr, classic)


class TestLedgerCharsRecording(unittest.TestCase):
    def test_log_usage_records_chars_for_real_usage(self):
        with temp_ledger(records=None) as path:
            amb.log_usage("m-x", {"prompt_tokens": 100,
                                  "completion_tokens": 5}, input_chars=333)
            with open(path, encoding="utf-8") as fh:
                stored = json.loads(fh.readlines()[-1])
        self.assertEqual(stored["chars"], 333)
        self.assertEqual(stored["in"], 100)

    def test_log_usage_skips_chars_for_estimated_usage(self):
        with temp_ledger(records=None) as path:
            amb.log_usage("m-x", {"prompt_tokens": 100, "completion_tokens": 5,
                                  "_estimated": True}, input_chars=333)
            with open(path, encoding="utf-8") as fh:
                stored = json.loads(fh.readlines()[-1])
        self.assertNotIn("chars", stored)

    def test_complete_feeds_observed_chars_to_the_ledger(self):
        seen = {}
        msgs = [{"role": "system", "content": "s" * 40},
                {"role": "user", "content": "u" * 60}]
        body = (200, {"content": "answer", "reasoning": "",
                      "usage": {"prompt_tokens": 25, "completion_tokens": 3},
                      "finish_reason": "stop"})
        ns = argparse.Namespace(
            max_tokens=8000, temperature=0.1, timeout=30, raw=False,
            fallback=False, allow_partial=False, allow_cost=True, yes=True,
            no_cache=True, cache_ttl=None, model=None,
            escalation_ceiling=30000, _auto_budget=True)
        with patched(amb,
                     stream_completion=lambda *a, **k: body,
                     log_usage=lambda model, usage, input_chars=None,
                     _s=seen: _s.update(chars=input_chars)):
            amb.complete(KEY, "https://x", "m-x", msgs, ns)
        self.assertEqual(seen.get("chars"), 100)


if __name__ == "__main__":
    unittest.main()
