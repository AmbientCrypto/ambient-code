"""Regression tests for the CRIT/HIGH audit-remediation batch (2026-07-06).

Each test locks in a fix for a finding CONFIRMED by independent verification of
the best-of-3 self-audit. Phases are appended as they land.

No network, no live API. Run: python3 -m pytest tests/test_audit_crithigh_fixes.py
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "ambient")


@contextlib.contextmanager
def _patch_attr(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_crithigh", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_crithigh", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = load_module()


# ---------------------------------------------------------- Phase 1: security

class TestA3StripsC1Controls(unittest.TestCase):
    """A3: the terminal-injection filter must strip C1 controls (0x80-0x9f)
    too — some terminals treat 0x9b/0x9d as CSI/OSC introducers."""

    def test_c1_controls_stripped(self):
        for c in ("\x80", "\x9b", "\x9d", "\x9f"):
            self.assertNotIn(c, amb.redact(f"a{c}b", ""))

    def test_tab_and_newline_still_kept(self):
        self.assertEqual(amb.redact("a\tb\nc", ""), "a\tb\nc")

    def test_cr_still_stripped(self):  # from the earlier F1 fix
        self.assertNotIn("\r", amb.redact("a\rb", ""))


class TestA12SanitizesCatalogStrings(unittest.TestCase):
    """A12: network-derived catalog model IDs / names must be sanitized before
    they reach the terminal."""

    def test_sanitize_strips_escapes(self):
        self.assertEqual(amb.sanitize("a\x1b[31m\x9bred"), "ared")

    def test_sanitize_handles_non_str(self):
        self.assertEqual(amb.sanitize(12345), "12345")
        self.assertIsNone(amb.sanitize(None))

    def test_format_model_line_has_no_escapes_for_malicious_id(self):
        m = {"id": "evil\x1b[2J\x9dhttp://x", "name": "n\x1b[31m",
             "context_length": 1000, "is_ready": True}
        line = amb.format_model_line(m, "chat/x", "code/x", note="no\x1bte")
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\x9d", line)


class TestA2InsecureBypassLoopbackOnly(unittest.TestCase):
    """A2: AMBIENT_ALLOW_INSECURE may bypass host-pinning ONLY for a loopback
    host — never a private-LAN/link-local/public host."""

    def test_loopback_allowed(self):
        for h in ("127.0.0.1", "localhost", "::1", "dev.localhost", "127.5.5.5"):
            self.assertTrue(amb._is_local_host(h), h)

    def test_non_loopback_rejected(self):
        for h in ("192.168.1.5", "10.0.0.1", "169.254.1.1", "0.0.0.0",
                  "172.16.0.1", "evil.com", "api.ambient.xyz.evil.com", ""):
            self.assertFalse(amb._is_local_host(h), h)


# ------------------------------------------------------- Phase 2: spend gate

class TestA8SingleCallGateIsFallbackAware(unittest.TestCase):
    """A8: _single_call_gate must price through the FALLBACK-AWARE estimator
    (estimate_cost_fb), not the plain estimate_cost, so a --fallback swap is
    reserved up front like the batch gates."""

    def test_single_call_gate_uses_estimate_cost_fb(self):
        called = {"fb": False, "plain": False}

        # spy_fb returns a stub WITHOUT delegating to the real estimate_cost_fb
        # (which itself calls estimate_cost internally) — so `plain` is set only
        # if _single_call_gate calls the plain estimator DIRECTLY, which is the
        # bug A8 fixes.
        def spy_fb(*a, **k):
            called["fb"] = True
            return (0.0, 0.0, False)

        def spy_plain(*a, **k):
            called["plain"] = True
            return (0.0, 0.0, False)

        cat = [{"id": "m/x", "is_ready": True, "context_length": 128000,
                "max_output_length": 32000, "supported_features": [],
                "output_modalities": ["text"],
                "pricing": {"input": 0.2, "output": 0.8}}]
        args = argparse.Namespace(max_tokens=256, temperature=0.1, timeout=30,
                                  fallback=False, allow_cost=True, yes=True)
        with _patch_attr(amb, "estimate_cost_fb", spy_fb), \
                _patch_attr(amb, "estimate_cost", spy_plain):
            amb._single_call_gate(cat, "m/x", 4000, args, {})
        self.assertTrue(called["fb"],
                        "_single_call_gate must call estimate_cost_fb")
        self.assertFalse(called["plain"],
                         "_single_call_gate must NOT call plain estimate_cost")


# ------------------------------------------------------ Phase 3: audit/build

class TestA1ReadFilesMultibyte(unittest.TestCase):
    """A1 (CRITICAL): a multibyte file within the CHAR budget must be read in
    FULL — sizing the read by the char count in BYTES truncated UTF-8 files
    mid-content (N bytes decode to fewer than N chars, so the cap never tripped
    and the tail was silently dropped)."""

    def test_multibyte_file_read_in_full_within_budget(self):
        content = "配" * 50   # 50 chars, 150 UTF-8 bytes
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "cjk.txt")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(content)
            # cap = 100 chars: 50 < 100 so the file FITS; the old byte-sized
            # read(100+1)=101 bytes would truncate it to ~33 chars.
            with _patch_attr(amb, "ABS_MAX_CHARS", 100):
                chunks = amb.read_files([p])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][1], content)   # all 50 chars, not truncated

    def test_genuinely_over_cap_still_fails_loud(self):
        content = "配" * 200   # 200 chars > cap 100
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "big.txt")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(content)
            with _patch_attr(amb, "ABS_MAX_CHARS", 100):
                with self.assertRaises(SystemExit):
                    amb.read_files([p])


class TestA9GitDiffFromSubdirectory(unittest.TestCase):
    """A9: `git diff` audit must read changed-file CONTENT even when invoked
    from a subdirectory — git emits repo-root-relative paths, so CWD-relative
    resolution silently dropped every file."""

    def _git(self, *a, cwd):
        subprocess.run(["git", *a], cwd=cwd, check=True,
                       capture_output=True, text=True)

    def test_staged_diff_from_subdir_includes_file_content(self):
        with tempfile.TemporaryDirectory() as repo:
            self._git("init", cwd=repo)
            self._git("config", "user.email", "t@t.t", cwd=repo)
            self._git("config", "user.name", "t", cwd=repo)
            sub = os.path.join(repo, "src")
            os.makedirs(sub)
            fpath = os.path.join(sub, "main.py")
            with open(fpath, "w", encoding="utf-8") as fh:
                fh.write("def f():\n    return 1  # UNIQUEMARKER\n")
            self._git("add", "src/main.py", cwd=repo)
            # run git_diff_inputs from the SUBDIR
            cwd0 = os.getcwd()
            try:
                os.chdir(sub)
                labeled = amb.git_diff_inputs(staged=True, ref=None)
            finally:
                os.chdir(cwd0)
        blob = "\n".join(text for _lbl, text in labeled)
        self.assertIn("UNIQUEMARKER", blob)   # full file content present
        # and the changed file is a labeled input, not just the diff text
        self.assertTrue(any("main.py" in lbl for lbl, _t in labeled))


# --------------------------------------------------- Phase 4: config/onboard

class TestA6OpencodeConfigTmpRace(unittest.TestCase):
    """A6: ensure_opencode_config must use a pid-unique tmp (so concurrent
    writers can't share one .tmp inode) AND preserve a 0600 config's mode."""

    def test_write_preserves_600_and_leaves_no_stray_tmp(self):
        import io
        import stat
        import sys
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "opencode.json")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write('{"provider": {"openai": {"options": '
                         '{"apiKey": "sk-secret"}}}}')
            os.chmod(cfg, 0o600)
            with _patch_attr(amb, "OPENCODE_CONFIG_PATH", cfg), \
                    _patch_attr(sys, "stderr", io.StringIO()):
                amb.ensure_opencode_config("https://api.example", "some/model")
            if os.name != "nt":   # Windows chmod can't express 0o600
                mode = stat.S_IMODE(os.stat(cfg).st_mode)
                self.assertEqual(mode, 0o600, f"mode widened to {oct(mode)}")
            strays = [f for f in os.listdir(d) if ".tmp" in f]
            self.assertEqual(strays, [], f"stray tmp left: {strays}")

    def test_tmp_name_is_pid_unique(self):
        # the tmp path embeds the pid so two processes never collide
        self.assertIn(str(os.getpid()),
                      amb.OPENCODE_CONFIG_PATH + f".tmp-{os.getpid()}-1")


class TestA13OnboardingNotBlockedByUrlCheck(unittest.TestCase):
    """A13: a first-run user with a custom AMBIENT_API_URL but NO key must
    reach onboarding (exit UNCONFIGURED), not be blocked by the key-exfil URL
    refusal for a key that does not exist yet."""

    def test_keyless_custom_url_reaches_unconfigured_not_url_refusal(self):
        import io
        import sys
        env = {k: v for k, v in os.environ.items()}
        env["AMBIENT_API_URL"] = "https://not-ambient.example.com"
        env["AMBIENT_NO_ONBOARD"] = "1"   # force non-interactive path
        with _patch_attr(amb, "read_config_file", lambda: {}), \
                _patch_attr(amb, "resolve_key_and_backend",
                            lambda conf: (None, None)), \
                _patch_attr(amb.os, "environ", env), \
                _patch_attr(sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.load_config()
        # EXIT_UNCONFIGURED (onboarding path), NOT the URL-refusal exit
        self.assertEqual(cm.exception.code, amb.EXIT_UNCONFIGURED)


if __name__ == "__main__":
    unittest.main()
