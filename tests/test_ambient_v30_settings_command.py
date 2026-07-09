"""v1.3.x — the first-class `ambient settings` command and the build_phase
milestone helper (2026-07-08). Pure stdlib unittest (the canonical CI runner has
no pytest).

WHY these two live together: both are about the CLI being legible to a human
driving it. `settings` exists because nobody hunting for a toggle guesses
`config`; it MUST be a byte-identical alias, not a near-copy, or the two
surfaces drift. build_phase exists because a reasoning model's planning pass
emits no content for minutes — without a phase line a working build is
indistinguishable from a hang, and the bug that motivated it was that the line
was tty-gated, so a headless tool harness (streaming "on") saw nothing.

No network, no live API, tempdirs/isolated HOME only.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "bin", "ambient")


def _load():
    loader = importlib.machinery.SourceFileLoader("amb_v30", _BIN)
    mod = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("amb_v30", loader))
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


def _run(*argv):
    """Invoke bin/ambient exactly as the user would, in an isolated empty HOME —
    so the run is provably keyless and can never read the developer's real
    config file. Returns the CompletedProcess with RAW BYTES (byte-identity is
    the contract under test)."""
    with tempfile.TemporaryDirectory() as home:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("AMBIENT_")}
        env["HOME"] = home
        env.pop("XDG_CONFIG_HOME", None)   # CONFIG_PATH is ~/.config, HOME-rooted
        env.pop("CLAUDE_PLUGIN_ROOT", None)   # skip the launcher self-heal block
        return subprocess.run([sys.executable, _BIN, *argv],
                              env=env, capture_output=True, timeout=60)


# ---------------------------------------------------------------------------
# registry + parser wiring (hermetic, in-process)
# ---------------------------------------------------------------------------

class RegistryTests(unittest.TestCase):
    def test_settings_registered_keyless_to_cmd_config(self):
        spec = next(s for s in amb.COMMANDS if s["name"] == "settings")
        self.assertIs(spec["needs_key"], False)
        self.assertEqual(spec["handler"], "cmd_config")
        self.assertTrue(callable(amb._registry_handler("cmd_config")))

    def test_settings_shares_the_config_handler(self):
        # An alias, not a fork: both entries resolve to the SAME function, so the
        # two surfaces can never diverge.
        cfg = next(s for s in amb.COMMANDS if s["name"] == "config")
        setg = next(s for s in amb.COMMANDS if s["name"] == "settings")
        self.assertEqual(setg["handler"], cfg["handler"])
        self.assertIs(amb._registry_handler(setg["handler"]),
                      amb._registry_handler(cfg["handler"]))

    def test_parser_parses_settings_forms(self):
        p = amb.build_parser()
        self.assertEqual(p.parse_args(["settings"]).verb, "status")
        a = p.parse_args(["settings", "set", "streaming", "off"])
        self.assertEqual((a.verb, a.name, a.value), ("set", "streaming", "off"))
        a = p.parse_args(["settings", "unset", "spend-cap"])
        self.assertEqual((a.verb, a.name), ("unset", "spend-cap"))

    def test_bad_settings_verb_rejected(self):
        with self.assertRaises(SystemExit):
            amb.build_parser().parse_args(["settings", "frobnicate"])


# ---------------------------------------------------------------------------
# `ambient settings` == `ambient config` (end-to-end, real dispatch)
# ---------------------------------------------------------------------------

class AliasOutputTests(unittest.TestCase):
    def test_settings_and_config_stdout_are_byte_identical(self):
        cfg, setg = _run("config"), _run("settings")
        self.assertEqual(cfg.returncode, 0, cfg.stderr)
        self.assertEqual(setg.returncode, 0, setg.stderr)
        # The contract: identical bytes, not merely similar text.
        self.assertEqual(cfg.stdout, setg.stdout)

    def test_settings_is_keyless(self):
        # An empty HOME has NO stored key; a needs_key command would exit 3.
        # `settings` renders the status and exits 0 — proof it is keyless.
        r = _run("settings")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Ambient settings", r.stdout.decode())

    def test_status_hint_column_and_footer_say_settings(self):
        out = _run("settings").stdout.decode()
        # The set-hint column points at the discoverable command name…
        self.assertIn("ambient settings set streaming on|off", out)
        # …and the reset footer does too (not the old `config` verb).
        self.assertIn("ambient settings unset <name>", out)


# ---------------------------------------------------------------------------
# build_phase — the greppable milestone line
# ---------------------------------------------------------------------------

class BuildPhaseTests(unittest.TestCase):
    """build_phase writes one milestone to stderr, gated ONLY by the progress
    toggle — never by whether stderr is a tty."""

    def _emit(self, msg="x", env_progress=None, tty=None):
        # Resolved=None so progress_display_enabled() honors AMBIENT_PROGRESS,
        # independent of any main() run that mutated the shared dict.
        env = {k: v for k, v in os.environ.items() if k != "AMBIENT_PROGRESS"}
        if env_progress is not None:
            env["AMBIENT_PROGRESS"] = env_progress
        over = {"_PROGRESS_DISPLAY": {"resolved": None}}
        if tty is not None:
            over["_stderr_is_tty"] = lambda: tty
        err = io.StringIO()
        with patched(amb, **over), patched(amb.os, environ=env), \
                contextlib.redirect_stderr(err):
            amb.build_phase(msg)
        return err.getvalue()

    def test_writes_milestone_when_progress_enabled(self):
        self.assertEqual(self._emit("planning"), "ambient: build — planning\n")

    def test_silent_when_progress_off(self):
        self.assertEqual(self._emit(env_progress="off"), "")

    def test_writes_even_when_stderr_is_not_a_tty(self):
        # THE regression guard for "streaming is on but nothing shows": phase
        # lines are deliberately NOT tty-gated, so a headless harness still sees
        # them. If build_phase ever gains an `and _stderr_is_tty()` gate, this
        # fails.
        self.assertEqual(self._emit("generating 1-3 of 9", tty=False),
                         "ambient: build — generating 1-3 of 9\n")


if __name__ == "__main__":
    unittest.main()
