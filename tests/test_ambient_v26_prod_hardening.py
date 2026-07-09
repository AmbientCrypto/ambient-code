"""v1.1.x production hardening — regression tests for the 8 fixes from the
Codex + team-share Workflow audits (2026-07-08). Pure stdlib unittest (the
canonical CI runner has no pytest). See
docs/plans/2026-07-08-production-hardening-and-features.md."""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "bin", "ambient")


def _load():
    loader = importlib.machinery.SourceFileLoader("amb_v26", _BIN)
    mod = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("amb_v26", loader))
    loader.exec_module(mod)
    return mod


amb = _load()


class InsecureUrlTests(unittest.TestCase):
    """#1 AMBIENT_ALLOW_INSECURE must relax HTTPS ONLY for a local host."""

    def setUp(self):
        self._env = dict(os.environ)
        for k in ("AMBIENT_API_URL", "AMBIENT_ALLOW_INSECURE", "AMBIENT_ALLOW_URL"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def _resolve(self, conf=None):
        with contextlib.redirect_stderr(io.StringIO()):
            return amb.resolve_api_url(conf or {})

    def test_insecure_flag_does_not_allow_http_to_real_host(self):
        os.environ["AMBIENT_API_URL"] = "http://api.ambient.xyz"
        os.environ["AMBIENT_ALLOW_INSECURE"] = "1"
        with self.assertRaises(SystemExit):
            self._resolve()

    def test_insecure_flag_allows_http_localhost(self):
        os.environ["AMBIENT_API_URL"] = "http://127.0.0.1:8080"
        os.environ["AMBIENT_ALLOW_INSECURE"] = "1"
        self.assertEqual(self._resolve(), "http://127.0.0.1:8080")

    def test_https_real_host_is_fine(self):
        os.environ["AMBIENT_API_URL"] = "https://api.ambient.xyz"
        self.assertEqual(self._resolve(), "https://api.ambient.xyz")

    def test_plain_http_real_host_without_flag_refused(self):
        os.environ["AMBIENT_API_URL"] = "http://api.ambient.xyz"
        with self.assertRaises(SystemExit):
            self._resolve()


class SystemPromptTripwireTests(unittest.TestCase):
    """#2 a secret in --system must be caught (it is sent to the network)."""

    def test_ask_refuses_secret_in_system(self):
        args = argparse.Namespace(
            prompt=["hello"], allow_secrets=False, model=None,
            system="AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_ask(args, "sk-key", "https://api.ambient.xyz", {})
        self.assertNotEqual(cm.exception.code, 0)   # refused before any network call

    def test_benign_system_is_not_refused_at_scan(self):
        # A benign --system must pass the scan (it then proceeds to the network,
        # which we cut off by pointing at an unroutable host so no real request
        # is made — the point is it did NOT refuse at the tripwire).
        seen = {}
        real = amb.refuse_if_secrets

        def spy(chunks, allow):
            seen["labels"] = [c[0] for c in chunks]
            return real(chunks, allow)
        args = argparse.Namespace(prompt=["hi"], allow_secrets=False, model=None,
                                  system="You are a careful analyst.")
        with mock.patch.object(amb, "refuse_if_secrets", spy), \
                mock.patch.object(amb, "Session", side_effect=SystemExit(0)), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                amb.cmd_ask(args, "sk-key", "https://api.ambient.xyz", {})
        self.assertIn("system", seen.get("labels", []))


class PartialAuditTests(unittest.TestCase):
    """#3 the partial flag must make coverage non-clean (SHIP can't survive)."""

    def _render(self, raw, partial):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            amb.render_findings(raw, "json", api_key="", partial=partial, model="m")
        return json.loads(buf.getvalue())

    def test_partial_flag_forces_non_clean(self):
        env = self._render(json.dumps({"findings": [], "verdict": "SHIP"}), True)
        self.assertFalse(env["coverage_complete"])
        self.assertNotEqual(env["verdict"], "SHIP")

    def test_not_partial_stays_clean(self):
        env = self._render(json.dumps({"findings": [], "verdict": "SHIP"}), False)
        self.assertTrue(env["coverage_complete"])
        self.assertEqual(env["verdict"], "SHIP")


class StreamLineCapTests(unittest.TestCase):
    """#4 the streamed SSE line is bounded so a no-newline stream can't buffer
    without bound / starve the hard-wall + no-progress checks."""

    def test_stream_line_cap_is_bounded_and_generous(self):
        self.assertTrue(hasattr(amb, "STREAM_LINE_MAX"))
        self.assertGreaterEqual(amb.STREAM_LINE_MAX, 1 << 20)     # >=1MB: real lines fit
        self.assertLessEqual(amb.STREAM_LINE_MAX, 64 << 20)       # bounded, not unbounded

    def test_stream_actually_passes_the_cap_to_readline(self):
        # Non-vacuous: records the size arg readline() receives. If the fix were
        # reverted to a bare resp.readline(), the size would be -1 and this fails.
        seen = []
        lines = [
            b"data: " + json.dumps(
                {"choices": [{"delta": {"content": "hi"}}]}).encode() + b"\n",
            b"\n", b"data: [DONE]\n", b"\n",
        ]

        class RecordingSSE:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def __init__(self):
                self._i = 0

            def readline(self, size=-1):
                seen.append(size)
                if self._i < len(lines):
                    v = lines[self._i]
                    self._i += 1
                    return v
                return b""

            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(amb.urllib.request, "urlopen",
                               lambda req, timeout=None: RecordingSSE()):
            amb.stream_completion("https://x", "k", {"model": "m"}, 30)
        self.assertTrue(seen, "readline was never called")
        self.assertTrue(all(s == amb.STREAM_LINE_MAX for s in seen),
                        f"readline must be capped at STREAM_LINE_MAX, got {set(seen)}")

    def test_no_newline_dribble_aborts_on_schedule_not_hang(self):
        # The real fix: a peer that keeps the socket open but never delivers a
        # parseable line must trip the no-progress stall ON SCHEDULE via the
        # reader-thread + wall-clock loop — not hang because a blocking readline
        # owns the clock. (Reverting to an inline readline would hang here.)
        class SilentSSE:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def readline(self, size=-1):
                time.sleep(60)   # a dribble that never completes a line
                return b""

            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(amb.urllib.request, "urlopen",
                               lambda req, timeout=None: SilentSSE()), \
                mock.patch.object(amb, "NOPROGRESS_S", 0.5), \
                contextlib.redirect_stderr(io.StringIO()):
            t0 = time.monotonic()
            with self.assertRaises(amb.StallError):
                amb.stream_completion("https://x", "k", {"model": "m"}, 30)
            self.assertLess(time.monotonic() - t0, 6)   # aborted promptly, no hang


class BoundedStdinTests(unittest.TestCase):
    """#5 when select() is unavailable, the read must NOT hang forever."""

    def test_bounded_read_gives_up_instead_of_hanging(self):
        def _blocked():
            time.sleep(30)          # simulate a held-open, data-less stdin read
            return "late"
        with mock.patch.object(amb, "_stdin_read_and_decode", _blocked):
            t0 = time.monotonic()
            with contextlib.redirect_stderr(io.StringIO()):
                out = amb._read_stdin_bounded(0.2, "no data within 0.2s")
            self.assertEqual(out, "")
            self.assertLess(time.monotonic() - t0, 5)   # returned promptly, no hang

    def test_bounded_read_returns_data_when_fast(self):
        with mock.patch.object(amb, "_stdin_read_and_decode", lambda: "piped!"):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(amb._read_stdin_bounded(5, "x"), "piped!")

    def test_read_stdin_if_piped_does_not_hang_when_select_unavailable(self):
        # Integration: read_stdin_if_piped() must route to the bounded read when
        # select() raises (Windows pipes / IDE-CI wrappers). If the fix were
        # reverted (except: pass → fall through to a blocking read), the mocked
        # 30s-blocking read would make this hang instead of returning promptly.
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = False

        def _blocked():
            time.sleep(30)
            return "late"

        with mock.patch.object(amb.sys, "stdin", fake_stdin), \
                mock.patch.object(amb.select, "select",
                                  side_effect=OSError("select() unsupported on this fd")), \
                mock.patch.object(amb, "_stdin_read_and_decode", _blocked), \
                mock.patch.dict(amb.os.environ, {"AMBIENT_STDIN_WAIT": "0.2"}), \
                contextlib.redirect_stderr(io.StringIO()):
            t0 = time.monotonic()
            out = amb.read_stdin_if_piped()
            self.assertEqual(out, "")
            self.assertLess(time.monotonic() - t0, 5)   # bounded — did not hang


class WindowsPermHealTests(unittest.TestCase):
    """#7 the 0600 self-heal must be POSIX-only (else a false 'tightened 666->600'
    on every Windows command)."""

    def _read_with_mode(self, force_nt):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        cfg = os.path.join(d, "env")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("AMBIENT_API_KEY=abc\n")
        os.chmod(cfg, 0o666)
        buf = io.StringIO()
        ctx = [mock.patch.object(amb, "CONFIG_PATH", cfg),
               contextlib.redirect_stderr(buf)]
        if force_nt:
            ctx.append(mock.patch.object(amb.os, "name", "nt"))
        with contextlib.ExitStack() as stack:
            for c in ctx:
                stack.enter_context(c)
            amb.read_config_file()
        return buf.getvalue()

    @unittest.skipUnless(os.name == "posix", "needs POSIX chmod to set 0o666")
    def test_windows_does_not_print_spurious_tightened(self):
        self.assertNotIn("tightened", self._read_with_mode(force_nt=True))

    @unittest.skipUnless(os.name == "posix", "POSIX heal path")
    def test_posix_still_heals_and_reports(self):
        self.assertIn("tightened", self._read_with_mode(force_nt=False))


class WindowsShimTests(unittest.TestCase):
    """#8 _shim_is_ours matches the new sys.executable form AND the legacy
    @python form (so an old shim stays removable) and rejects foreign shims."""

    def _write(self, body):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, "ambient.cmd")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        return p

    def test_new_interp_form_is_ours(self):
        p = self._write(r'@"C:\Python\python.exe" "C:\x\ambient-code\bin\ambient" %*')
        self.assertTrue(amb._shim_is_ours(p))

    def test_legacy_python_form_still_ours(self):
        p = self._write(r'@python "C:\x\ambient-code\bin\ambient" %*')
        self.assertTrue(amb._shim_is_ours(p))

    def test_foreign_shim_rejected(self):
        p = self._write(r'@python "C:\other\tool\bin\other" %*')
        self.assertFalse(amb._shim_is_ours(p))


class WindowsAgentTests(unittest.TestCase):
    """#6 on Windows, `ambient agent` must run opencode as a CHILD (subprocess),
    not os.execvpe (which spawns-then-exits and can't exec opencode.cmd)."""

    def test_agent_uses_subprocess_on_windows(self):
        calls = {"exec": 0, "run": 0}

        def fake_run(cmd, **kw):
            calls["run"] += 1
            return argparse.Namespace(returncode=0)

        def fake_exec(*a, **k):
            calls["exec"] += 1
            raise AssertionError("execvpe must not be used on Windows")

        args = argparse.Namespace(model=None, agent_args=[])
        with mock.patch.object(amb.os, "name", "nt"), \
                mock.patch.object(amb.shutil, "which", lambda _n: r"C:\opencode.cmd"), \
                mock.patch.object(amb, "resolve_model", lambda *a, **k: "z-ai/glm-5.2"), \
                mock.patch.object(amb, "is_auto_model", lambda _m: False), \
                mock.patch.object(amb, "ensure_opencode_config", lambda *a, **k: None), \
                mock.patch.object(amb.subprocess, "run", fake_run), \
                mock.patch.object(amb.os, "execvpe", fake_exec), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_agent(args, "sk-key", "https://api.ambient.xyz", {})
        self.assertEqual(calls["run"], 1)
        self.assertEqual(calls["exec"], 0)
        self.assertEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
