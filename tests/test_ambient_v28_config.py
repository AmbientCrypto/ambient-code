"""v1.3.0 — `ambient config` settings surface (2026-07-08). Pure stdlib unittest
(the canonical CI runner has no pytest). See
docs/plans/2026-07-08-production-hardening-and-features.md.

Covers: set/unset round-trips (each knob's real resolver honors the write), boolean
normalization, validation rejection (exit 64, file untouched), unknown/key-ish name
rejection, unset→default, the status view (never prints the key VALUE), the
env-override annotation, parser + registry wiring, and the whitelist NO-CLOBBER
guarantee. Each test fails if the feature were reverted. No network, tempdirs only.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "bin", "ambient")


def _load():
    loader = importlib.machinery.SourceFileLoader("amb_v28", _BIN)
    mod = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("amb_v28", loader))
    loader.exec_module(mod)
    return mod


amb = _load()


@contextlib.contextmanager
def patched(obj, **attrs):
    missing = object()
    old = {k: getattr(obj, k, missing) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v) if v is not missing else delattr(obj, k)


class _ConfigCase(unittest.TestCase):
    """Isolated env + a per-test config file at a patched CONFIG_PATH."""

    def setUp(self):
        self._env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith("AMBIENT_"):
                os.environ.pop(k, None)
        self._td = tempfile.mkdtemp()
        self._cfg = os.path.join(self._td, "env")
        self._orig_cfg = amb.CONFIG_PATH
        amb.CONFIG_PATH = self._cfg
        # Isolate from the real OS keychain so status tests don't depend on
        # whether THIS machine has a key stored (the status view reads it only to
        # show configured/MISSING, never the value).
        self._orig_kc = amb.keychain_read
        amb.keychain_read = lambda: None

    def tearDown(self):
        amb.CONFIG_PATH = self._orig_cfg
        amb.keychain_read = self._orig_kc
        shutil.rmtree(self._td, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self._env)

    def config(self, *a):
        """Run cmd_config with a Namespace mirroring _configure_config; return
        (stdout, stderr). Raises SystemExit for usage errors (assert on it)."""
        ns = argparse.Namespace(
            verb=a[0] if a else "status",
            name=a[1] if len(a) > 1 else None,
            value=a[2] if len(a) > 2 else None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            amb.cmd_config(ns)
        return out.getvalue(), err.getvalue()

    def conf(self):
        return amb.read_config_file()


class RoundTripTests(_ConfigCase):
    """`config set` → the SAME resolver the CLI uses honors it (no reader change)."""

    def test_streaming_off(self):
        self.config("set", "streaming", "off")
        self.assertEqual(self.conf().get("AMBIENT_PROGRESS"), "off")
        self.assertFalse(amb._progress_from_env_or_conf(self.conf()))

    def test_spend_cap(self):
        self.config("set", "spend-cap", "12")
        self.assertEqual(amb._ceiling(self.conf()), (12.0, True))

    def test_spend_cap_lenient_dollar_and_comma(self):
        self.config("set", "spend-cap", "$1,234")
        self.assertEqual(amb._ceiling(self.conf()), (1234.0, True))

    def test_fleet_budget_off(self):
        self.config("set", "fleet-budget", "off")
        self.assertFalse(amb._fleet_enabled(self.conf()))

    def test_fallback_on(self):
        self.config("set", "fallback", "on")
        self.assertEqual(self.conf().get("AMBIENT_FALLBACK"), "on")
        self.assertEqual(amb._CONFIG_BY_NAME["fallback"]["current"](self.conf()), "on")

    def test_reference_price(self):
        self.config("set", "reference-price", "5/20")
        self.assertEqual(amb.resolve_reference_price(self.conf()), (5.0, 20.0))

    def test_confirmation_line_echoed(self):
        out, _ = self.config("set", "streaming", "off")
        self.assertIn("streaming = off", out)


class BoolNormalizationTests(_ConfigCase):
    def test_truthy_spellings_store_on(self):
        for v in ("on", "TRUE", "1", "yes", "On"):
            self.config("set", "streaming", v)
            self.assertEqual(self.conf().get("AMBIENT_PROGRESS"), "on", v)

    def test_falsey_spellings_store_off(self):
        for v in ("off", "false", "0", "no", "OFF"):
            self.config("set", "streaming", v)
            self.assertEqual(self.conf().get("AMBIENT_PROGRESS"), "off", v)


class ValidationRejectTests(_ConfigCase):
    def _rejects(self, *a):
        with self.assertRaises(SystemExit) as cm:
            self.config(*a)
        self.assertEqual(cm.exception.code, 64)
        self.assertFalse(os.path.exists(self._cfg), "a rejected set must not write")

    def test_spend_cap_bad(self):
        for v in ("abc", "-1", "0", "nan", "inf"):
            self._rejects("set", "spend-cap", v)

    def test_reference_price_bad(self):
        for v in ("abc", "0", "1/2/3", "-3/15"):
            self._rejects("set", "reference-price", v)

    def test_streaming_bad(self):
        self._rejects("set", "streaming", "maybe")

    def test_set_without_value(self):
        self._rejects("set", "streaming")

    def test_set_without_name(self):
        self._rejects("set")


class UnknownAndKeyNameTests(_ConfigCase):
    def test_unknown_name_rejected_no_write(self):
        with self.assertRaises(SystemExit) as cm:
            self.config("set", "bogus", "on")
        self.assertEqual(cm.exception.code, 64)
        self.assertFalse(os.path.exists(self._cfg))

    def test_unset_unknown_rejected(self):
        with self.assertRaises(SystemExit):
            self.config("unset", "bogus")

    def test_key_names_point_to_setup_and_never_write(self):
        for n in ("key", "api-key", "api_key", "apikey", "API-KEY"):
            with self.assertRaises(SystemExit) as cm:
                self.config("set", n, "sk-should-never-be-stored")
            self.assertEqual(cm.exception.code, 64)
        self.assertFalse(os.path.exists(self._cfg))

    def test_unknown_message_lists_names_and_pointers(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            amb.cmd_config(argparse.Namespace(verb="set", name="bogus", value="on"))
        msg = err.getvalue()
        self.assertIn("streaming", msg)          # lists valid names
        self.assertIn("ambient use", msg)        # points to the model command
        self.assertIn("ambient setup", msg)      # points key to setup

    def test_key_refusal_does_not_echo_the_value(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            amb.cmd_config(argparse.Namespace(
                verb="set", name="key", value="sk-topsecret-xyz"))
        self.assertNotIn("sk-topsecret-xyz", err.getvalue())

    def test_key_equals_value_form_refused_without_echo(self):
        # `config set key=<SECRET>` must be read as the name `key` (value dropped),
        # refused with the setup pointer, and NEVER echo the secret (Codex A #1).
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            amb.cmd_config(argparse.Namespace(
                verb="set", name="key=sk-topsecret-abcdef123456", value=None))
        self.assertEqual(cm.exception.code, 64)
        self.assertNotIn("sk-topsecret-abcdef123456", err.getvalue())
        self.assertIn("ambient setup", err.getvalue())
        self.assertFalse(os.path.exists(self._cfg))

    def test_secret_shaped_unknown_name_is_redacted(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            amb.cmd_config(argparse.Namespace(
                verb="set", name="sk-live-SECRETSECRET012345", value="on"))
        self.assertNotIn("SECRETSECRET012345", err.getvalue())
        self.assertIn("<unrecognized>", err.getvalue())

    def test_spaces_and_unicode_names_never_echoed(self):
        # A whitelist of safe slug SHAPE — anything with spaces/unicode is a
        # placeholder, so a pasted secret of any shape can't be reflected.
        for weird in ("pa55 phrase with spaces", "päss-wörd", "sk secret one",
                      "AKIA IOSFODNN7 EXAMPLE"):
            err = io.StringIO()
            with self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(io.StringIO()):
                amb.cmd_config(argparse.Namespace(verb="set", name=weird, value="on"))
            out = err.getvalue()
            self.assertIn("<unrecognized>", out, weird)
            self.assertNotIn(weird, out, weird)

    def test_plain_typo_is_still_echoed_helpfully(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            amb.cmd_config(argparse.Namespace(verb="set", name="streming", value="on"))
        self.assertIn("streming", err.getvalue())   # a clean slug typo is echoed


class UnsetRejectsValueTests(_ConfigCase):
    def test_unset_with_extra_value_is_rejected_and_not_destructive(self):
        # `config unset streaming off` must NOT silently delete AMBIENT_PROGRESS
        # while ignoring the stray value (Codex A #2).
        self.config("set", "streaming", "off")
        with self.assertRaises(SystemExit) as cm:
            self.config("unset", "streaming", "off")
        self.assertEqual(cm.exception.code, 64)
        self.assertEqual(self.conf().get("AMBIENT_PROGRESS"), "off")  # not deleted


class ArgvSecretGuardTests(_ConfigCase):
    """The pre-argparse guard in main() also covers `config`, so a key-shaped token
    is refused BEFORE argparse (or any handler) can echo it (Codex A #1, --flag form)."""

    def _guard(self, argv):
        with patched(amb.sys, argv=argv):
            with self.assertRaises(SystemExit) as cm:
                amb.main()
        return str(cm.exception.code or "")

    def test_positional_secret_token_refused(self):
        msg = self._guard(["ambient", "config", "set", "spend-cap",
                           "sk-live-SECRETSECRET012345"])
        self.assertNotIn("SECRETSECRET012345", msg)
        self.assertIn("shell history", msg)

    def test_key_flag_form_refused(self):
        msg = self._guard(["ambient", "config", "--key=sk-live-SECRETSECRET012345"])
        self.assertNotIn("SECRETSECRET012345", msg)
        self.assertIn("shell history", msg)

    def test_short_key_assignment_refused_regardless_of_shape(self):
        # A SHORT key value must still be refused before argparse can echo it —
        # a `key=`/`--api-key=` assignment is never valid here (Codex re-audit).
        for argv in (["ambient", "config", "--key=short"],
                     ["ambient", "config", "set", "key=short"],
                     ["ambient", "config", "--api-key=abc123"],
                     ["ambient", "config", "set", "AMBIENT_API_KEY=xy"]):
            msg = self._guard(argv)
            self.assertIn("shell history", msg)
            self.assertNotIn("short", msg)
            self.assertNotIn("abc123", msg)


class UnsetTests(_ConfigCase):
    def test_unset_removes_key_and_restores_default(self):
        self.config("set", "spend-cap", "12")
        self.assertEqual(amb._ceiling(self.conf()), (12.0, True))
        out, _ = self.config("unset", "spend-cap")
        self.assertNotIn("AMBIENT_MAX_SPEND", self.conf())
        self.assertEqual(amb._ceiling(self.conf()), (5.0, False))
        self.assertIn("back to default", out)


class StatusRenderTests(_ConfigCase):
    def test_status_shows_settings_and_pointers(self):
        out, _ = self.config()   # bare = status
        for token in ("Ambient settings", "API key", "streaming", "fallback",
                      "spend-cap", "reference-price", "ambient use", "ambient mode",
                      "ambient curate"):
            self.assertIn(token, out, token)

    def test_status_reports_missing_key_when_unset(self):
        out, _ = self.config()
        self.assertIn("MISSING", out)

    def test_status_never_prints_the_key_value(self):
        secret = "sk-SUPERSECRETVALUE0001"
        with patched(amb, resolve_key_and_backend=lambda conf: (secret, "keychain")):
            out, _ = self.config()
        self.assertNotIn(secret, out)
        self.assertIn("configured (keychain)", out)


class EnvOverrideTests(_ConfigCase):
    def test_status_notes_env_override(self):
        os.environ["AMBIENT_PROGRESS"] = "off"
        out, _ = self.config()
        self.assertIn("(env override)", out)

    def test_set_prints_shadow_note_on_stderr(self):
        os.environ["AMBIENT_PROGRESS"] = "off"
        out, err = self.config("set", "streaming", "on")
        self.assertIn("overrides the config", err)
        # still writes the file so it takes effect once the env var is unset
        self.assertEqual(self.conf().get("AMBIENT_PROGRESS"), "on")


class ParserWiringTests(_ConfigCase):
    def test_parser_parses_config_forms(self):
        p = amb.build_parser()
        self.assertEqual(p.parse_args(["config"]).verb, "status")
        a = p.parse_args(["config", "set", "streaming", "off"])
        self.assertEqual((a.verb, a.name, a.value), ("set", "streaming", "off"))
        a = p.parse_args(["config", "unset", "spend-cap"])
        self.assertEqual((a.verb, a.name), ("unset", "spend-cap"))

    def test_bad_verb_rejected(self):
        with self.assertRaises(SystemExit):
            amb.build_parser().parse_args(["config", "frobnicate"])


class RegistryTests(_ConfigCase):
    def test_config_registered_keyless(self):
        spec = next(s for s in amb.COMMANDS if s["name"] == "config")
        self.assertIs(spec["needs_key"], False)
        self.assertTrue(callable(amb._registry_handler("cmd_config")))


class NoClobberTests(_ConfigCase):
    def test_set_preserves_other_commands_keys(self):
        # Keys owned by use/mode/curate must survive a config write — proves the
        # CONFIG_SETTINGS whitelist is the only thing set/unset touches.
        amb.save_config_values({"AMBIENT_DELEGATE": "takeover",
                                "AMBIENT_MODEL": "z-ai/glm-5.2",
                                "AMBIENT_MODELS_HIDE": "qwen/*"})
        self.config("set", "streaming", "off")
        c = self.conf()
        self.assertEqual(c.get("AMBIENT_DELEGATE"), "takeover")
        self.assertEqual(c.get("AMBIENT_MODEL"), "z-ai/glm-5.2")
        self.assertEqual(c.get("AMBIENT_MODELS_HIDE"), "qwen/*")
        self.assertEqual(c.get("AMBIENT_PROGRESS"), "off")

    def test_unset_only_removes_its_own_key(self):
        amb.save_config_values({"AMBIENT_DELEGATE": "on"})
        self.config("set", "spend-cap", "9")
        self.config("unset", "spend-cap")
        self.assertEqual(self.conf().get("AMBIENT_DELEGATE"), "on")


if __name__ == "__main__":
    unittest.main()
