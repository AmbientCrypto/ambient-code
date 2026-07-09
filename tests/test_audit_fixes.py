"""Regression tests for an earlier remediation batch.

Each test locks in a fix for a finding that was CONFIRMED by independent
verification against the source (an internal review):

  F1  redact() strips carriage returns (terminal line-overwrite spoofing)
  F5  _finding_sig keys on the full path, not the basename (dedupe collision)
  F9  ensure_opencode_config preserves a config's restrictive mode
  F15 cmd_usage skips well-formed non-object JSON lines instead of crashing
  F24 the setup key-leak guard also inspects `--key=<token>` style args

No network, no live API. Run: python3 -m pytest tests/test_audit_verified_fixes.py
"""
import importlib.machinery
import importlib.util
import io
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_fixes", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_fixes", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = load_module()


class TestF1CarriageReturnStripped(unittest.TestCase):
    """F1 (CRITICAL): a bare CR in model output must not survive redact() —
    otherwise it moves the cursor to column 0 and lets untrusted output
    overwrite the current terminal line (spoofing a verdict/banner/receipt)."""

    def test_cr_is_stripped(self):
        poisoned = "PARTIAL — review needed\rOK — no issues found"
        cleaned = amb.redact(poisoned, "")
        self.assertNotIn("\r", cleaned)
        # the visible text of both segments survives; only the CR is gone
        self.assertIn("PARTIAL", cleaned)
        self.assertIn("OK", cleaned)

    def test_newline_and_tab_preserved(self):
        # \n and \t are legitimate and must be kept
        self.assertEqual(amb.redact("a\nb\tc", ""), "a\nb\tc")

    def test_other_c0_controls_still_stripped(self):
        # sanity: the regex still removes NUL/BEL/ESC-less C0 controls
        self.assertEqual(amb.redact("a\x00\x07b", ""), "ab")


class TestF5FindingSignatureFullPath(unittest.TestCase):
    """F5 (HIGH): findings in different directories that share a filename must
    not dedupe into one (which silently dropped the other)."""

    def test_same_basename_different_dir_are_distinct(self):
        a = {"file": "src/utils.py", "line": 42, "title": "missing null check",
             "severity": "HIGH", "scenario": "x"}
        b = {"file": "tests/utils.py", "line": 42, "title": "missing null check",
             "severity": "HIGH", "scenario": "y"}
        kept = amb.dedupe_findings([a, b])
        self.assertEqual(len(kept), 2, "different dirs must stay separate")

    def test_identical_path_still_dedupes(self):
        a = {"file": "src/utils.py", "line": 42, "title": "missing null check",
             "severity": "HIGH", "scenario": "short"}
        b = {"file": "./src/utils.py", "line": 43, "title": "missing null check",
             "severity": "CRITICAL", "scenario": "a longer scenario string"}
        kept = amb.dedupe_findings([a, b])
        self.assertEqual(len(kept), 1, "same file+title+near-line must merge")
        # highest severity + longest scenario win
        self.assertEqual(kept[0].get("severity"), "CRITICAL")


class TestF9OpencodeConfigPreservesMode(unittest.TestCase):
    """F9 (HIGH): rewriting opencode.json must not widen a 0600 config that may
    hold other providers' API keys to a umask-default 0644."""

    def test_existing_600_mode_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            cfgpath = os.path.join(d, "opencode.json")
            # Seed a pre-existing config at 0600 with a foreign provider key.
            with open(cfgpath, "w", encoding="utf-8") as fh:
                fh.write('{"provider": {"openai": {"options": '
                         '{"apiKey": "sk-secret"}}}}')
            os.chmod(cfgpath, 0o600)
            with patch.object(amb, "OPENCODE_CONFIG_PATH", cfgpath), \
                    io.StringIO() as _s, \
                    patch.object(sys, "stderr", _s):
                amb.ensure_opencode_config("https://api.example", "some/model")
            # Unix mode preservation; on Windows os.chmod only toggles the
            # read-only bit, so 0o600 isn't representable — chmod is best-effort.
            if os.name != "nt":
                mode = stat.S_IMODE(os.stat(cfgpath).st_mode)
                self.assertEqual(mode, 0o600,
                                 f"mode widened to {oct(mode)} — key exposure")

    def test_new_file_is_created_restrictively(self):
        with tempfile.TemporaryDirectory() as d:
            cfgpath = os.path.join(d, "sub", "opencode.json")
            with patch.object(amb, "OPENCODE_CONFIG_PATH", cfgpath), \
                    io.StringIO() as _s, \
                    patch.object(sys, "stderr", _s):
                amb.ensure_opencode_config("https://api.example", "some/model")
            self.assertTrue(os.path.exists(cfgpath))
            if os.name != "nt":   # Windows chmod can't express 0o600
                mode = stat.S_IMODE(os.stat(cfgpath).st_mode)
                self.assertEqual(mode, 0o600,
                                 f"new config created at {oct(mode)}, want 0600")


class TestF15UsageSkipsNonDictLines(unittest.TestCase):
    """F15 (MED): a well-formed non-object JSON line (42, "x", [..]) must be
    skipped as corrupt, not crash cmd_usage with AttributeError."""

    def _run_usage(self, usage_body):
        with tempfile.TemporaryDirectory() as d:
            upath = os.path.join(d, "usage.jsonl")
            with open(upath, "w", encoding="utf-8") as fh:
                fh.write(usage_body)
            args = type("A", (), {"json": False, "days": 30})()
            out = io.StringIO()
            with patch.object(amb, "USAGE_PATH", upath), \
                    patch.object(amb, "read_config_file", lambda: {}), \
                    patch.object(sys, "stdout", out):
                amb.cmd_usage(args)
            return out.getvalue()

    def test_non_dict_lines_do_not_crash(self):
        body = ('42\n'
                '"garbage"\n'
                '[1, 2, 3]\n'
                '{"ts": 1783357893, "model": "m", "in": 10, "out": 20, '
                '"cost": 0.001, "ref": [3.0, 15.0]}\n')
        try:
            printed = self._run_usage(body)
        except AttributeError as err:  # the pre-fix failure mode
            self.fail(f"cmd_usage crashed on a non-dict line: {err}")
        # the one valid record still summarized
        self.assertIn("m", printed)


class TestF24SetupKeyLeakGuardEqualsForm(unittest.TestCase):
    """F24 (LOW/security): `ambient setup --key=sk-...` smuggles the secret
    inside a `-`-prefixed token; the guard must inspect the value after '='
    and refuse before argparse echoes it in an error."""

    def test_key_equals_form_is_refused(self):
        fake_key = "sk-" + "A1b2C3d4E5" * 3  # >=20 chars, key-shaped
        argv = ["ambient", "setup", f"--key={fake_key}"]
        with patch.object(sys, "argv", argv), \
                io.StringIO() as err, patch.object(sys, "stderr", err):
            with self.assertRaises(SystemExit) as cm:
                amb.main()
            msg = str(cm.exception)
        self.assertIn("shell history", msg,
                      "guard did not refuse the --key=<token> form")
        # the secret itself must NOT be echoed back to the user
        self.assertNotIn(fake_key, msg)

    def test_plain_flag_without_value_is_ignored(self):
        # a bare flag (no '=') is not a secret and must not trip the guard
        argv = ["ambient", "setup", "--force"]
        with patch.object(sys, "argv", argv), \
                io.StringIO() as err, patch.object(sys, "stderr", err):
            # --force is a real setup flag; guard must let it through to argparse
            try:
                amb.main()
            except SystemExit as exc:
                # if it exits, it must NOT be the key-leak refusal
                self.assertNotIn("shell history", str(exc or ""))


if __name__ == "__main__":
    unittest.main()
