"""v1.2.0 feature regressions — Ambient Takeover mode, the env-tunable smart
timeout, and the user-toggleable streamed progress display (2026-07-08). Pure
stdlib unittest (the canonical CI runner has no pytest). See
docs/plans/2026-07-08-production-hardening-and-features.md.

Every test is written to FAIL if the feature were reverted:
  * TakeoverModeTests      — `ambient mode takeover` persists + surfaces as TAKEOVER.
  * EnvPosIntTests         — HARD_WALL_S / NOPROGRESS_S env override + floors.
  * ProgressResolverTests  — flag > env > config > on precedence, purely.
  * ProgressWiringTests    — the toggle actually gates the heartbeat (stream) and
                             the build "generating" line, WITHOUT touching the
                             hard-wall / no-progress / stall guards.
No network, no live API, no writes outside tempdirs.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_BIN = os.path.join(_ROOT, "bin", "ambient")
_HOOK = os.path.join(_ROOT, "hooks", "session-start.sh")


def _load():
    loader = importlib.machinery.SourceFileLoader("amb_v27", _BIN)
    mod = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("amb_v27", loader))
    loader.exec_module(mod)
    return mod


amb = _load()


@contextlib.contextmanager
def patched(obj, **attrs):
    """Temporarily set attributes on a module/object, restoring exactly."""
    missing = object()
    old = {k: getattr(obj, k, missing) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is missing:
                delattr(obj, k)
            else:
                setattr(obj, k, v)


class _EnvIsolated(unittest.TestCase):
    """Snapshot/restore the whole environment + the resolved-progress holder so
    tests never leak AMBIENT_* state into each other."""

    def setUp(self):
        self._env = dict(os.environ)
        for k in ("AMBIENT_PROGRESS", "AMBIENT_HARD_WALL_S", "AMBIENT_NOPROGRESS_S"):
            os.environ.pop(k, None)
        self._prog = dict(amb._PROGRESS_DISPLAY)
        amb._PROGRESS_DISPLAY["resolved"] = None   # unresolved → env fallback

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        amb._PROGRESS_DISPLAY.clear()
        amb._PROGRESS_DISPLAY.update(self._prog)


class TakeoverModeTests(_EnvIsolated):
    """C3: `ambient mode takeover` is a 3rd ordered level (off < on < takeover),
    persists to config, and surfaces as TAKEOVER; `off` clears it."""

    def _mode(self, state):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_mode(argparse.Namespace(state=state, model=None))
        return out.getvalue()

    def test_takeover_persists_and_announces_off_switch(self):
        with tempfile.TemporaryDirectory() as td:
            with patched(amb, CONFIG_PATH=os.path.join(td, "env")):
                out = self._mode("takeover")
                self.assertEqual(
                    amb.read_config_file().get("AMBIENT_DELEGATE"), "takeover")
                low = out.lower()
                self.assertIn("takeover", low)
                # The exit path must always be visible when turning it on.
                self.assertIn("ambient mode off", low)

    def test_off_clears_takeover(self):
        with tempfile.TemporaryDirectory() as td:
            with patched(amb, CONFIG_PATH=os.path.join(td, "env")):
                self._mode("takeover")
                self._mode("off")
                self.assertEqual(
                    amb.read_config_file().get("AMBIENT_DELEGATE"), "off")

    def test_status_line_reports_takeover(self):
        with tempfile.TemporaryDirectory() as td:
            with patched(amb, CONFIG_PATH=os.path.join(td, "env")):
                amb.save_config_values({"AMBIENT_DELEGATE": "takeover"})
                out = self._mode(None)      # bare `ambient mode` = status
                self.assertIn("delegate=takeover", out)

    def test_takeover_is_ordered_above_on(self):
        # The design is an ORDERED level, not a parallel flag: takeover implies
        # delegate. The mode arg accepts exactly off/on/takeover.
        p = amb.build_parser()
        for state in ("off", "on", "takeover"):
            a = p.parse_args(["mode", state])
            self.assertEqual(a.state, state)
        with self.assertRaises(SystemExit):
            p.parse_args(["mode", "bogus"])

    def test_bare_banner_renders_TAKEOVER(self):
        # The onboarding banner's status footer maps delegate=takeover → the
        # uppercase TAKEOVER badge (vs "delegate on/off"), key present.
        with patched(amb,
                     read_config_file=lambda: {"AMBIENT_DELEGATE": "takeover"},
                     resolve_key_and_backend=lambda conf: ("k", "keychain"),
                     resolve_model=lambda *a, **k: "moonshotai/kimi-k2.7-code"):
            self.assertIn("TAKEOVER", amb.build_banner())

    def test_bare_banner_on_is_not_takeover(self):
        with patched(amb,
                     read_config_file=lambda: {"AMBIENT_DELEGATE": "on"},
                     resolve_key_and_backend=lambda conf: ("k", "keychain"),
                     resolve_model=lambda *a, **k: "moonshotai/kimi-k2.7-code"):
            banner = amb.build_banner()
            self.assertNotIn("TAKEOVER", banner)
            self.assertIn("delegate on", banner)


class EnvPosIntTests(_EnvIsolated):
    """C2: HARD_WALL_S / NOPROGRESS_S are env-tunable via _env_pos_int, with a
    floor so a fat-fingered 0 can never DISABLE the safety guard."""

    def test_missing_keeps_default(self):
        self.assertEqual(amb._env_pos_int("AMBIENT_NOPROGRESS_S", 150, floor=10), 150)

    def test_valid_override_applied(self):
        os.environ["AMBIENT_HARD_WALL_S"] = "9000"
        self.assertEqual(amb._env_pos_int("AMBIENT_HARD_WALL_S", 5400, floor=60), 9000)

    def test_floor_clamps_too_small(self):
        os.environ["AMBIENT_NOPROGRESS_S"] = "7"           # below the floor
        self.assertEqual(amb._env_pos_int("AMBIENT_NOPROGRESS_S", 150, floor=10), 10)
        os.environ["AMBIENT_NOPROGRESS_S"] = "0"           # cannot disable
        self.assertEqual(amb._env_pos_int("AMBIENT_NOPROGRESS_S", 150, floor=10), 10)

    def test_invalid_keeps_default(self):
        os.environ["AMBIENT_HARD_WALL_S"] = "not-a-number"
        self.assertEqual(amb._env_pos_int("AMBIENT_HARD_WALL_S", 5400, floor=60), 5400)

    def test_module_constants_are_positive_ints(self):
        self.assertIsInstance(amb.HARD_WALL_S, int)
        self.assertIsInstance(amb.NOPROGRESS_S, int)
        self.assertGreaterEqual(amb.HARD_WALL_S, 60)
        self.assertGreaterEqual(amb.NOPROGRESS_S, 10)


class ProgressResolverTests(_EnvIsolated):
    """C1c: precedence for the progress-display toggle — flag > env > config > on."""

    def _r(self, flag=None, env=None, conf=None):
        if env is not None:
            os.environ["AMBIENT_PROGRESS"] = env
        args = argparse.Namespace() if flag is None else argparse.Namespace(progress=flag)
        return amb._resolve_progress_display(args, conf or {})

    def test_default_is_on(self):
        self.assertTrue(self._r())

    def test_config_off(self):
        self.assertFalse(self._r(conf={"AMBIENT_PROGRESS": "off"}))

    def test_env_beats_config(self):
        self.assertTrue(self._r(env="on", conf={"AMBIENT_PROGRESS": "off"}))
        os.environ.pop("AMBIENT_PROGRESS", None)
        self.assertFalse(self._r(env="off", conf={"AMBIENT_PROGRESS": "on"}))

    def test_flag_beats_env(self):
        self.assertTrue(self._r(flag=True, env="off"))
        self.assertFalse(self._r(flag=False, env="on"))

    def test_all_falsey_spellings(self):
        for word in ("off", "0", "false", "no", "OFF", "No"):
            self.assertFalse(self._r(conf={"AMBIENT_PROGRESS": word}), word)

    def test_enabled_helper_honors_env_when_unresolved(self):
        amb._PROGRESS_DISPLAY["resolved"] = None
        os.environ["AMBIENT_PROGRESS"] = "off"
        self.assertFalse(amb.progress_display_enabled())
        os.environ["AMBIENT_PROGRESS"] = "on"
        self.assertTrue(amb.progress_display_enabled())

    def test_enabled_helper_prefers_resolved_over_env(self):
        amb._PROGRESS_DISPLAY["resolved"] = True
        os.environ["AMBIENT_PROGRESS"] = "off"   # main() already decided; env ignored
        self.assertTrue(amb.progress_display_enabled())

    def test_argparse_absent_when_neither_flag_given(self):
        p = amb.build_parser()
        a = p.parse_args(["build", "x", "--dir", "/tmp"])
        self.assertIsNone(getattr(a, "progress", None))
        a = p.parse_args(["build", "x", "--dir", "/tmp", "--no-progress"])
        self.assertFalse(a.progress)
        a = p.parse_args(["ask", "hi", "--progress"])
        self.assertTrue(a.progress)

    def test_flag_present_on_every_streaming_command(self):
        p = amb.build_parser()
        for cmd, tail in (("ask", ["hi"]), ("audit", ["f.py"]), ("map", ["p", "f"]),
                          ("code", ["t"]), ("chat", []), ("build", ["t", "--dir", "/tmp"])):
            a = p.parse_args([cmd, *tail, "--no-progress"])
            self.assertFalse(a.progress, cmd)


# ---- streaming heartbeat harness (mirrors v26's RecordingSSE) ---------------
def _sse(lines):
    class FakeSSE:
        status = 200
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self):
            self._i = 0

        def readline(self, size=-1):
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

    return FakeSSE


_ONE_DELTA = [
    b"data: " + json.dumps({"choices": [{"delta": {"content": "hi"}}]}).encode() + b"\n",
    b"\n", b"data: [DONE]\n", b"\n",
]


class ProgressWiringTests(_EnvIsolated):
    """C1/C1c: the toggle actually SILENCES the heartbeat + build phase line, and
    doing so NEVER weakens the smart-timeout guards."""

    def _stream(self, sse_factory):
        err = io.StringIO()
        with patched(amb.urllib.request, urlopen=lambda req, timeout=None: sse_factory()), \
                patched(amb, HEARTBEAT_S=0, _stderr_is_tty=lambda: False), \
                contextlib.redirect_stderr(err):
            status, result = amb.stream_completion("https://x", "k", {"model": "m"}, 30)
        return status, result, err.getvalue()

    def test_heartbeat_shown_when_progress_on(self):
        os.environ["AMBIENT_PROGRESS"] = "on"
        status, result, err = self._stream(_sse(_ONE_DELTA))
        self.assertEqual(status, 200)
        self.assertEqual(result["content"], "hi")
        self.assertIn("chars,", err)          # the heartbeat line fired

    def test_heartbeat_silenced_when_progress_off(self):
        os.environ["AMBIENT_PROGRESS"] = "off"
        status, result, err = self._stream(_sse(_ONE_DELTA))
        self.assertEqual(result["content"], "hi")   # content still parsed …
        self.assertNotIn("chars,", err)             # … but no heartbeat noise
        self.assertNotIn("…", err)

    def test_stall_guard_still_fires_with_progress_off(self):
        # The critical safety property: silencing the display must NOT silence the
        # no-progress stall guard. A socket that dribbles nothing must still abort.
        os.environ["AMBIENT_PROGRESS"] = "off"

        class SilentSSE:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def readline(self, size=-1):
                time.sleep(4)          # never completes a line
                return b""

            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        t0 = time.time()
        with patched(amb.urllib.request, urlopen=lambda req, timeout=None: SilentSSE()), \
                patched(amb, NOPROGRESS_S=1, HARD_WALL_S=3600,
                        _stderr_is_tty=lambda: False), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(amb.StallError):
                amb.stream_completion("https://x", "k", {"model": "m"}, 30)
        self.assertLess(time.time() - t0, 15, "stall must trip promptly, not hang")


# ---- build "generating" line harness (mirrors test_ambient_features) --------
def _build_ns(root, **kw):
    base = dict(task=["make", "a", "thing"], dir=root, context=None,
                apply=False, force=False, plan_only=False, dry_run=False,
                max_files=32, max_file_bytes=200_000, no_resume=False,
                json=True, allow_secrets=False, model=None, max_tokens=None,
                temperature=0.1, timeout=30, raw=False, fallback=False,
                allow_partial=True, allow_cost=True, yes=True, no_cache=True,
                cache_ttl=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _fake_build_complete(plan_files, gen_batches):
    batches = list(gen_batches)

    def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
        if not getattr(fake, "_seen_plan", False):
            fake._seen_plan = True
            return (json.dumps({"plan": plan_files, "notes": "n",
                                "advisory_steps": ["run tests"]}),
                    {}, {"finish_reason": "stop"})
        resp = batches.pop(0)
        return (json.dumps({"files": resp["files"]}), {},
                {"finish_reason": resp.get("finish_reason", "stop")})

    fake._seen_plan = False
    return fake


_CAT = [{"id": "moonshotai/kimi-k2.7-code", "context_length": 262144,
         "max_output_length": 262144, "is_ready": True,
         "supported_features": ["reasoning", "structured_outputs"],
         "output_modalities": ["text"],
         "pricing": {"input": 1.0, "output": 3.83}}]


class BuildProgressLineTests(_EnvIsolated):
    """C1a/C1c: cmd_build prints a per-batch `generating … [X/Y]` line — shown by
    default, silenced by AMBIENT_PROGRESS=off."""

    def _run_build(self):
        root = tempfile.mkdtemp()
        plan = [{"path": "a.py", "purpose": "p", "est_lines": 3},
                {"path": "b.py", "purpose": "p", "est_lines": 3}]
        fake = _fake_build_complete(plan, [
            {"files": [{"path": "a.py", "content": "A\n"},
                       {"path": "b.py", "content": "B\n"}]}])
        out, err = io.StringIO(), io.StringIO()
        with patched(amb, complete=fake, safe_catalog=lambda *a: _CAT,
                     cost_gate=lambda *a, **k: None,
                     warn_if_stdin_ignored=lambda *a: None):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                amb.cmd_build(_build_ns(root), "k", "https://x", {})
        return json.loads(out.getvalue()), err.getvalue()

    def test_generating_line_shown_by_default(self):
        env, err = self._run_build()
        self.assertEqual(env["status"], "ok")
        self.assertIn("ambient: generating", err)
        self.assertIn("of the plan done", err)

    def test_generating_line_silenced_when_progress_off(self):
        os.environ["AMBIENT_PROGRESS"] = "off"
        env, err = self._run_build()
        self.assertEqual(env["status"], "ok")           # build still succeeds …
        self.assertNotIn("ambient: generating", err)    # … just no progress noise


@unittest.skipIf(os.name == "nt",
                 "SessionStart contract hook is POSIX sh; Windows uses the "
                 "skill's own SessionStart reminder path")
class HookTakeoverContractTests(unittest.TestCase):
    """C3 + audit fixes: the SessionStart hook injects the right contract per
    AMBIENT_DELEGATE level, detects a whitespace-formatted config exactly like the
    CLI parser (Codex A), and emits the banner on ONE line (Codex B)."""

    def _run(self, cfg_text):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".config", "ambient"))
            with open(os.path.join(home, ".config", "ambient", "env"),
                      "w", encoding="utf-8") as fh:
                fh.write(cfg_text)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("AMBIENT_")}
            env["HOME"] = home
            env.pop("CLAUDE_PLUGIN_ROOT", None)   # skip the launcher self-heal block
            return subprocess.run(["sh", _HOOK], env=env, capture_output=True,
                                  text=True, timeout=30)

    def test_takeover_contract_emitted(self):
        r = self._run("AMBIENT_DELEGATE=takeover\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("TAKEOVER", r.stdout)
        self.assertIn("/ambient off", r.stdout)

    def test_whitespace_and_duplicate_config_detected(self):
        # Codex A: the hook's sed must trim + take last-wins like the CLI parser,
        # so a hand-spaced later assignment is honored.
        r = self._run("AMBIENT_DELEGATE=off\n AMBIENT_DELEGATE = takeover \n")
        self.assertIn("TAKEOVER", r.stdout)

    def test_delegate_on_contract(self):
        r = self._run("AMBIENT_DELEGATE=on\n")
        self.assertIn("delegate mode is ON", r.stdout)
        self.assertNotIn("TAKEOVER", r.stdout)

    def test_off_is_silent(self):
        r = self._run("AMBIENT_DELEGATE=off\n")
        self.assertEqual(r.stdout.strip(), "")

    def test_mandatory_banner_is_one_line(self):
        # Codex B: the banner Claude must echo has to be a SINGLE line so the hook
        # never teaches it to emit a line-broken banner.
        r = self._run("AMBIENT_DELEGATE=takeover\n")
        banner = [ln for ln in r.stdout.splitlines() if "Ambient Takeover ON" in ln]
        self.assertEqual(len(banner), 1)
        self.assertIn("/ambient off to stop", banner[0])


if __name__ == "__main__":
    unittest.main()
