"""P4/F03 — the stdin '-' sentinel must work in ANY argument order, including
the natural `ask "prompt" -m MODEL -` that argparse orphaned into
'unrecognized arguments: -'. See docs/plans/2026-07-06-stress-test-remediation.md."""
import importlib.machinery
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "bin", "ambient")


def _load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_dash", _BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_dash", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = _load_module()


class StdinDashSentinelTests(unittest.TestCase):
    def setUp(self):
        # monkeypatch.setattr(amb.sys, "argv", ...) equivalent: save and restore.
        self._orig_argv = amb.sys.argv
        self.addCleanup(setattr, amb.sys, "argv", self._orig_argv)

    def _parse(self, argv):
        amb.sys.argv = ["ambient"] + argv
        return amb._parse_args_with_stdin_dash(amb.build_parser())

    def test_dash_sentinel_reaches_prompt_in_every_order(self):
        for argv in [
            ["ask", "-m", "z-ai/glm-5.2", "list keys", "-"],   # flag before prompt
            ["ask", "list keys", "-"],                          # documented shape
            ["ask", "list keys", "-m", "z-ai/glm-5.2", "-"],    # THE bug: flag then dash
        ]:
            with self.subTest(argv=argv):
                args = self._parse(argv)
                self.assertIn("-", args.prompt)

    def test_no_dash_means_no_sentinel(self):
        args = self._parse(["ask", "list keys", "-m", "z-ai/glm-5.2"])
        self.assertNotIn("-", args.prompt)

    def test_real_unrecognized_argument_still_errors(self):
        with self.assertRaises(SystemExit):
            self._parse(["ask", "hi", "--totally-bogus-flag"])

    def test_trailing_dash_dropped_not_errored_on_other_commands(self):
        # Codex round 2: the natural order must not exit 64 on code/audit/map either.
        for argv in [
            ["audit", "file.py", "-m", "z-ai/glm-5.2", "-"],   # audit auto-reads stdin
            ["code", "build a thing", "-m", "z-ai/glm-5.2", "-"],  # code takes no stdin
            ["map", "summarize", "a.py", "-m", "z-ai/glm-5.2", "-"],
        ]:
            with self.subTest(argv=argv):
                args = self._parse(argv)  # must NOT raise SystemExit
                self.assertIsNotNone(args)

    def test_dash_handler_semantics_unchanged(self):
        # cmd_ask's want_stdin logic (words filter + '-' detection) must read the
        # injected sentinel the same as a normally-parsed one.
        args = self._parse(["ask", "summarize", "-m", "z-ai/glm-5.2", "-"])
        words = [w for w in args.prompt if w != "-"]
        want_stdin = "-" in args.prompt or not words
        self.assertIs(want_stdin, True)
        self.assertEqual(words, ["summarize"])
