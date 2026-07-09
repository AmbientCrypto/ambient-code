"""Hermetic REMEDIATION tests:
the global fan-out gate (consensus width² → one shared cap), consensus
fail-fast on fatal errors / Ctrl-C via cancel_event, the machine-readable
--json error envelope from INSIDE the real task handlers (ask/code/audit/
build), and per-model consensus budgets. No network, no live API — only
complete()/run_one_audit are patched, never the handlers under test."""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v3rem", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v3rem", loader)
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


KEY = "sk-test-key-abcdef1234567890"


def _fake_catalog(*ids):
    return [
        {"id": mid, "context_length": 200000, "max_output_length": 200000,
         "is_ready": True, "supported_features": ["reasoning"],
         "output_modalities": ["text"],
         "pricing": {"input": 1.0, "output": 4.0}}
        for mid in ids
    ]


def _base_ns(**over):
    ns = argparse.Namespace(
        model="fake/model-a", max_tokens=None, temperature=0.1, timeout=30,
        raw=False, json=True, fallback=False, allow_partial=False,
        allow_secrets=False, allow_cost=True, yes=True, no_cache=True,
        cache_ttl=None, parallel=None, system=None, response_format=None)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _mr_args(**over):
    ns = argparse.Namespace(
        max_tokens=1000, temperature=0.1, timeout=30, parallel=2,
        no_cache=True, cache_ttl=None, response_format=None)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


class TestGlobalGate(unittest.TestCase):
    """one shared Semaphore must cap TOTAL concurrent complete() calls
    across NESTED fan-outs (outer consensus models × inner chunk pools)."""

    def _fan_out(self, gate, outer=4, chunks=3, hold_s=0.05):
        lock = threading.Lock()
        cur, mx = [0], [0]

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            with lock:
                cur[0] += 1
                mx[0] = max(mx[0], cur[0])
            time.sleep(hold_s)
            with lock:
                cur[0] -= 1
            return "PART", {}, {"finish_reason": "stop"}

        def one_model():
            amb.run_map_reduce(
                KEY, "https://x", "fake/model-a", "map",
                [f"chunk {i}" for i in range(chunks)], _mr_args(),
                "synth", 100_000, reducer=lambda ts: "MERGED", gate=gate)

        with patched(amb, complete=fake_complete), \
                contextlib.redirect_stderr(io.StringIO()):
            threads = [threading.Thread(target=one_model)
                       for _ in range(outer)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        return mx[0]

    def test_shared_gate_caps_total_concurrency_at_width(self):
        # 4 outer "models" × inner width 2 would be 8 concurrent without the
        # gate; a shared Semaphore(2) must hold the observed max at ≤ 2.
        self.assertLessEqual(self._fan_out(threading.Semaphore(2)), 2)

    def test_without_gate_nested_fanout_really_exceeds_width(self):
        # Sensitivity check: the same rig with gate=None must observe MORE
        # than the width-2 cap, proving the gated assertion above has teeth.
        self.assertGreaterEqual(self._fan_out(None, hold_s=0.25), 3)


class TestCancelEvent(unittest.TestCase):
    """a set cancel_event must stop chunks from STARTING — zero new
    billed calls once the caller is unwinding."""

    def test_set_event_prevents_any_complete_call(self):
        calls = []

        def fake_complete(*a, **kw):
            calls.append(1)
            return "x", {}, {"finish_reason": "stop"}

        ev = threading.Event()
        ev.set()
        with patched(amb, complete=fake_complete), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(amb.ChatError):
                amb.run_map_reduce(
                    KEY, "https://x", "fake/model-a", "map",
                    ["c1", "c2"], _mr_args(), "synth", 100_000,
                    reducer=lambda ts: "MERGED", cancel_event=ev)
        self.assertEqual(calls, [])


def _consensus_args(src, models="fake/model-a,fake/model-b", **over):
    ns = argparse.Namespace(
        paths=[src], staged=False, diff=None, focus=None, allow_secrets=False,
        format="json", dry_run=False, consensus=models, model=None,
        max_tokens=None, temperature=0.1, timeout=30, raw=False,
        fallback=False, allow_partial=False, allow_cost=True, yes=True,
        no_cache=True, cache_ttl=None, parallel=None)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _src_file(body="print('hi')\n"):
    d = tempfile.mkdtemp()
    src = os.path.join(d, "x.py")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(body)
    return src


def _drain_worker_threads(timeout=10.0):
    """Wait for lingering ThreadPoolExecutor workers to finish. The fatal/Ctrl-C
    tests below use shutdown(wait=False) + a MOCKED os._exit, so their workers
    survive the test (in PRODUCTION the real os._exit kills them). If one is
    still alive when the NEXT test re-patches the module-global run_one_audit,
    it resolves that global at call-time and appends to the WRONG test's list —
    the cross-test pollution that made this suite flaky on slow CI runners.
    Bounded join: a signalled worker returns promptly; we never hang."""
    deadline = time.monotonic() + timeout
    for t in list(threading.enumerate()):
        if (t is threading.main_thread() or not t.is_alive()
                or not t.name.startswith("ThreadPoolExecutor")):
            continue
        t.join(timeout=max(0.0, deadline - time.monotonic()))


class TestConsensusFailFast(unittest.TestCase):
    """the FIRST fatal-category worker error (key/funds/auth) must cancel
    the queued siblings instead of billing them to completion."""

    def tearDown(self):
        _drain_worker_threads()

    def test_fatal_error_cancels_queued_models(self):
        # --parallel 1 → one worker: a fatal from model A must CANCEL the
        # still-queued B and C. A queued sibling can race past cancel_futures
        # into the worker before the event flips, so a raced-in model WAITS
        # for the cancel signal: fail-fast means it sees the event set almost
        # immediately; the old drain-everything behavior would leave it
        # timing out with the event still clear.
        ran = []
        seen_kwargs = {}
        sibling_saw_cancel = []

        def fake_audit(model, *a, **k):
            ran.append(model)
            seen_kwargs.update(k)
            if model == "fake/model-a":
                raise amb.ChatError("funds", "balance empty")
            k["cancel_event"].wait(timeout=5)
            sibling_saw_cancel.append(k["cancel_event"].is_set())
            return [], True

        models = "fake/model-a,fake/model-b,fake/model-c"
        args = _consensus_args(_src_file(), models=models, parallel=1)
        start = time.monotonic()
        with patched(amb,
                     safe_catalog=lambda *a, **k: _fake_catalog(
                         "fake/model-a", "fake/model-b", "fake/model-c"),
                     _gate_amount=lambda *a, **k: None,
                     run_one_audit=fake_audit), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(amb.ChatError) as cm:
                amb.cmd_audit(args, KEY, "https://x", {})
        self.assertEqual(cm.exception.category, "funds")
        # fail-fast raised WITHOUT waiting out any sibling's 5s timeout
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(ran[0], "fake/model-a")
        # any sibling that raced into the worker observed the cancel signal
        self.assertTrue(all(sibling_saw_cancel))
        # R1/one SHARED gate + cancel_event reached the workers, and the
        # fail-fast flipped the event so fan-outs stop starting chunks.
        self.assertTrue(hasattr(seen_kwargs.get("gate"), "acquire"))
        self.assertIsInstance(seen_kwargs.get("cancel_event"),
                              threading.Event)
        self.assertTrue(seen_kwargs["cancel_event"].is_set())

    def test_keyboard_interrupt_cancels_queued_models(self):
        # F6: Ctrl-C during a consensus run must end the PROCESS promptly via
        # os._exit(130) — non-daemon pool workers are joined by
        # concurrent.futures' atexit, so merely re-raising would stall exit for
        # up to --timeout if a sibling is mid-call. Mirrors cmd_map's contract.
        ran = []
        sibling_saw_cancel = []
        exit_codes = []

        def fake_exit(code):
            # Halt cmd_audit exactly where a real os._exit would (nothing after
            # may run), but keep the test process alive.
            exit_codes.append(code)
            raise SystemExit(code)

        def fake_audit(model, *a, **k):
            ran.append(model)
            if model == "fake/model-a":
                raise KeyboardInterrupt
            k["cancel_event"].wait(timeout=5)
            sibling_saw_cancel.append(k["cancel_event"].is_set())
            return [], True

        args = _consensus_args(_src_file(), parallel=1)
        start = time.monotonic()
        with patched(amb,
                     safe_catalog=lambda *a, **k: _fake_catalog(
                         "fake/model-a", "fake/model-b"),
                     _gate_amount=lambda *a, **k: None,
                     run_one_audit=fake_audit), \
                patched(amb.os, _exit=fake_exit), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_audit(args, KEY, "https://x", {})
        self.assertEqual(exit_codes, [130],
                         "Ctrl-C must os._exit(130) so non-daemon pool "
                         "threads cannot stall process exit")
        self.assertEqual(cm.exception.code, 130)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(ran[0], "fake/model-a")
        self.assertTrue(all(sibling_saw_cancel))

    def test_non_fatal_error_still_drains_all_models(self):
        # A transient per-model failure is NOT fatal: every model still runs,
        # and the error re-raises in model order afterwards (old semantics).
        ran = []

        def fake_audit(model, *a, **k):
            ran.append(model)
            if model == "fake/model-a":
                raise amb.ChatError("stall", "went quiet")
            return [], True

        args = _consensus_args(_src_file(), parallel=1)
        with patched(amb,
                     safe_catalog=lambda *a, **k: _fake_catalog(
                         "fake/model-a", "fake/model-b"),
                     _gate_amount=lambda *a, **k: None,
                     run_one_audit=fake_audit), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(amb.ChatError) as cm:
                amb.cmd_audit(args, KEY, "https://x", {})
        self.assertEqual(cm.exception.category, "stall")
        self.assertEqual(ran, ["fake/model-a", "fake/model-b"])


class TestConsensusPerModelBudget(unittest.TestCase):
    """consensus workers must get a PROFILE-derived budget per model —
    not the default model's resolved number — unless --max-tokens was
    explicitly set by the user."""

    def _run(self, max_tokens):
        budgets = {}

        def fake_audit(model, catalog, labeled, sys_prompt, args, *a, **k):
            budgets[model] = args.max_tokens
            return [], True

        args = _consensus_args(_src_file(), max_tokens=max_tokens)
        with patched(amb,
                     safe_catalog=lambda *a, **k: _fake_catalog(
                         "fake/model-a", "fake/model-b"),
                     _gate_amount=lambda *a, **k: None,
                     run_one_audit=fake_audit), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, KEY, "https://x", {})
        return budgets

    def test_auto_budget_is_not_inherited_from_default_model(self):
        budgets = self._run(max_tokens=None)
        self.assertEqual(budgets, {"fake/model-a": None, "fake/model-b": None})

    def test_explicit_max_tokens_is_respected_for_every_model(self):
        budgets = self._run(max_tokens=4096)
        self.assertEqual(budgets,
                         {"fake/model-a": 4096, "fake/model-b": 4096})


class TestJsonFailFromHandlers(unittest.TestCase):
    """a ChatError caught INSIDE the real task handlers must still produce
    the machine envelope under --json — parseable, status:"error", exit 1,
    key never on stdout. Only the engine (complete) is patched."""

    def _boom(self, category="stall"):
        def fake_complete(api_key, api_url, model, messages, args, **kw):
            raise amb.ChatError(category, f"engine fell over near {KEY}")
        return fake_complete

    def _assert_error_envelope(self, out, kind, category):
        env = json.loads(out)
        self.assertEqual(env["schema_version"], 1)
        self.assertEqual(env["kind"], kind)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["category"], category)
        self.assertEqual(env["exit_code"], 1)
        self.assertIn("engine fell over", env["diagnosis"])
        self.assertNotIn(KEY, out)

    def _catalog(self):
        return lambda *a, **k: _fake_catalog("fake/model-a")

    def test_ask_json_chat_error_emits_envelope(self):
        args = _base_ns(prompt=["what", "is", "this"])
        buf = io.StringIO()
        with patched(amb, safe_catalog=self._catalog(),
                     complete=self._boom()), \
                contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_ask(args, KEY, "https://x", {})
        self.assertEqual(cm.exception.code, 1)
        self._assert_error_envelope(buf.getvalue(), "ask", "stall")

    def test_code_json_chat_error_emits_envelope(self):
        args = _base_ns(task=["write", "a", "thing"], context=None)
        buf = io.StringIO()
        with patched(amb, safe_catalog=self._catalog(),
                     complete=self._boom("funds")), \
                contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_code(args, KEY, "https://x", {})
        self.assertEqual(cm.exception.code, 1)
        self._assert_error_envelope(buf.getvalue(), "code", "funds")

    def test_audit_json_single_shot_chat_error_emits_envelope(self):
        args = _consensus_args(_src_file(), consensus=None,
                               model="fake/model-a")
        buf = io.StringIO()
        with patched(amb, safe_catalog=self._catalog(),
                     complete=self._boom("rate")), \
                contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_audit(args, KEY, "https://x", {})
        self.assertEqual(cm.exception.code, 1)
        self._assert_error_envelope(buf.getvalue(), "audit", "rate")

    def test_build_json_planning_chat_error_emits_envelope(self):
        d = tempfile.mkdtemp()
        args = _base_ns(task=["a", "tiny", "tool"], context=None, dir=d,
                        apply=False, dry_run=False, plan_only=False,
                        no_resume=True, max_files=20,
                        max_file_bytes=200_000)
        buf = io.StringIO()
        with patched(amb, safe_catalog=self._catalog(),
                     cost_gate=lambda *a, **k: None,
                     complete=self._boom("funds")), \
                contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_build(args, KEY, "https://x", {})
        self.assertEqual(cm.exception.code, 1)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["kind"], "build")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["category"], "funds")
        self.assertIn("planning failed", env["diagnosis"])
        self.assertNotIn(KEY, buf.getvalue())

    def test_non_json_prose_path_is_unchanged(self):
        args = _base_ns(prompt=["hello"], json=False)
        buf = io.StringIO()
        with patched(amb, safe_catalog=self._catalog(),
                     complete=self._boom("funds")), \
                contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_ask(args, KEY, "https://x", {})
        self.assertIsInstance(cm.exception.code, str)
        self.assertTrue(cm.exception.code.startswith("ambient [funds]:"))
        self.assertEqual(buf.getvalue(), "")


class TestUsageErrorJsonEnvelope(unittest.TestCase):
    """usage errors under --json must also be machine-readable
    (exit 64), and the non-json prose path must stay byte-identical."""

    def test_usage_exit_emits_envelope_under_json(self):
        out = io.StringIO()
        with patched(amb.sys, argv=["ambient", "ask", "--json"]):
            with contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as cm:
                    amb.usage_exit("nothing to ask")
        self.assertEqual(cm.exception.code, amb.EXIT_USAGE)
        env = json.loads(out.getvalue())
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["category"], "usage")
        self.assertEqual(env["exit_code"], amb.EXIT_USAGE)

    def test_usage_exit_stderr_path_unchanged_without_json(self):
        out, err = io.StringIO(), io.StringIO()
        with patched(amb.sys, argv=["ambient", "ask"]):
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    amb.usage_exit("nothing to ask")
        self.assertEqual(cm.exception.code, amb.EXIT_USAGE)
        self.assertEqual(out.getvalue(), "")             # no envelope on stdout
        self.assertIn("nothing to ask", err.getvalue())  # prose on stderr

    def test_json_in_argv_detection(self):
        cases = [
            (["ambient", "ask", "--json"], True),
            (["ambient", "audit", "--format", "json"], True),
            (["ambient", "audit", "--format=json"], True),
            (["ambient", "ask"], False),
            (["ambient", "audit", "--format", "report"], False),
        ]
        for argv, want in cases:
            with patched(amb.sys, argv=argv):
                self.assertEqual(amb._json_in_argv(), want, argv)


def _tiny_profile(model="fake/model-a"):
    """A profile with a TINY single-shot window (50 chars) so a small test
    context reliably triggers the distillation lane without megabyte inputs."""
    return amb.ModelProfile(model, False, 1000, 1000, 500, 50, 40, 1000, [])


class TestTotalJsonFailureContract(unittest.TestCase):
    """the --json failure contract is TOTAL — every
    exit path reachable from ask/code/audit/build emits the machine envelope
    under --json (exit 1 runtime / 64 usage), while the NON-json prose +
    exit codes stay byte-identical."""

    def _envelope(self, out, kind, category, exit_code):
        env = json.loads(out)
        self.assertEqual(env["schema_version"], 1)
        self.assertEqual(env["kind"], kind)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["category"], category)
        self.assertEqual(env["exit_code"], exit_code)
        return env

    # ---- argparse: _Parser.error() ------------------------------------

    def test_argparse_error_under_json_emits_usage_envelope(self):
        out, err = io.StringIO(), io.StringIO()
        with patched(amb.sys, argv=["ambient", "audit", "--badflag", "--json"]), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                amb.main()
        self.assertEqual(cm.exception.code, amb.EXIT_USAGE)
        env = self._envelope(out.getvalue(), "audit", "usage", amb.EXIT_USAGE)
        self.assertIn("unrecognized arguments: --badflag", env["diagnosis"])
        self.assertEqual(err.getvalue(), "")  # envelope only, no prose mix

    def test_argparse_error_without_json_keeps_prose_and_exit_64(self):
        out, err = io.StringIO(), io.StringIO()
        with patched(amb.sys, argv=["ambient", "audit", "--badflag"]), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                amb.main()
        self.assertEqual(cm.exception.code, amb.EXIT_USAGE)
        self.assertEqual(out.getvalue(), "")             # nothing on stdout
        self.assertIn("unrecognized arguments: --badflag", err.getvalue())
        self.assertIn("try:", err.getvalue())            # argparse prose intact

    def test_mistyped_subcommand_kind_falls_back_to_usage(self):
        out = io.StringIO()
        with patched(amb.sys, argv=["ambient", "audiit", "x.py", "--json"]), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.main()
        self.assertEqual(cm.exception.code, amb.EXIT_USAGE)
        self._envelope(out.getvalue(), "usage", "usage", amb.EXIT_USAGE)

    # ---- audit with no input ------------------------------------------

    _AUDIT_NO_INPUT_PROSE = (
        "ambient: nothing to audit. Pass file paths and/or pipe a diff:\n"
        "  git diff | ambient audit\n  ambient audit src/foo.py src/bar.py")

    def _audit_no_input(self, fmt):
        args = _consensus_args(_src_file(), consensus=None, paths=[],
                               format=fmt)
        out, err = io.StringIO(), io.StringIO()
        with patched(amb, read_stdin_if_piped=lambda: ""), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_audit(args, KEY, "https://x", {})
        return cm.exception.code, out.getvalue(), err.getvalue()

    def test_audit_json_no_input_emits_usage_envelope(self):
        code, out, _err = self._audit_no_input("json")
        self.assertEqual(code, amb.EXIT_USAGE)
        env = self._envelope(out, "audit", "usage", amb.EXIT_USAGE)
        self.assertIn("nothing to audit", env["diagnosis"])

    def test_audit_no_input_prose_exits_64_with_prose_on_stderr(self):
        # H2: _fail_exit now honors exit_code on the prose path too —
        # a usage error exits EX_USAGE=64 (matching its --json twin), with the
        # byte-identical prose line on stderr instead of a string exit 1.
        code, out, err = self._audit_no_input("prose")
        self.assertEqual(code, amb.EXIT_USAGE)
        self.assertEqual(out, "")
        self.assertIn(self._AUDIT_NO_INPUT_PROSE, err)

    # ---- code: task-brief / context distillation incomplete ------------

    def _run_code(self, json_mode, task, context):
        args = _base_ns(task=task, context=context, json=json_mode)
        out = io.StringIO()
        with patched(amb,
                     safe_catalog=lambda *a, **k: _fake_catalog("fake/model-a"),
                     model_profile=lambda *a, **k: _tiny_profile(),
                     apply_output_budget=lambda *a, **k: None,
                     cost_gate=lambda *a, **k: None,
                     run_map_reduce=lambda *a, **k: ("part", True, "2 chunks failed")), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_code(args, KEY, "https://x", {})
        return cm.exception.code, out.getvalue()

    def test_code_json_task_brief_distillation_partial_emits_envelope(self):
        # task > the 50-char single-shot window → task-brief distillation lane
        code, out = self._run_code(True, ["x" * 80], None)
        self.assertEqual(code, 1)
        env = self._envelope(out, "code", "partial", 1)
        self.assertIn("task-brief distillation was incomplete (2 chunks failed)",
                      env["diagnosis"])
        self.assertNotIn(KEY, out)

    def test_code_json_context_distillation_partial_emits_envelope(self):
        code, out = self._run_code(True, ["tiny", "task"],
                                   [_src_file("x = 1  # pad\n" * 30)])
        self.assertEqual(code, 1)
        env = self._envelope(out, "code", "partial", 1)
        self.assertIn("context distillation was incomplete (2 chunks failed)",
                      env["diagnosis"])

    def test_code_context_distillation_prose_path_is_byte_identical(self):
        code, out = self._run_code(False, ["tiny", "task"],
                                   [_src_file("x = 1  # pad\n" * 30)])
        self.assertEqual(code, (
            "ambient: context distillation was incomplete (2 chunks failed) — "
            "the generated code could miss cross-file details. Re-run, "
            "narrow the context, or pass --allow-partial to proceed anyway."))
        self.assertEqual(out, "")

    # ---- build: context distillation incomplete -------------------------

    def _run_build(self, json_mode):
        d = tempfile.mkdtemp()
        args = _base_ns(task=["a", "tiny", "tool"],
                        context=[_src_file("x = 1  # pad\n" * 30)],
                        dir=d, apply=False, dry_run=False, plan_only=False,
                        no_resume=True, max_files=20, max_file_bytes=200_000,
                        json=json_mode)
        out = io.StringIO()
        with patched(amb,
                     safe_catalog=lambda *a, **k: _fake_catalog("fake/model-a"),
                     model_profile=lambda *a, **k: _tiny_profile(),
                     apply_output_budget=lambda *a, **k: None,
                     cost_gate=lambda *a, **k: None,
                     run_map_reduce=lambda *a, **k: ("part", True, "1 chunk failed")), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_build(args, KEY, "https://x", {})
        return cm.exception.code, out.getvalue()

    def test_build_json_context_distillation_partial_emits_envelope(self):
        code, out = self._run_build(True)
        self.assertEqual(code, 1)
        env = self._envelope(out, "build", "partial", 1)
        self.assertIn("context distillation was incomplete (1 chunk failed)",
                      env["diagnosis"])
        self.assertNotIn(KEY, out)

    def test_build_context_distillation_prose_path_is_byte_identical(self):
        code, out = self._run_build(False)
        self.assertEqual(code, (
            "ambient: context distillation was incomplete (1 chunk failed) — "
            "re-run, narrow -f, or pass --allow-partial."))
        self.assertEqual(out, "")

    # ---- shared helpers reachable from the task commands ----------------

    def test_secrets_refusal_emits_envelope_under_json_argv(self):
        out = io.StringIO()
        with patched(amb.sys, argv=["ambient", "audit", "x.py", "--json"]), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                amb.refuse_if_secrets(
                    [("s.py", 'api_key = "sk_live_abcdef1234567890"')], False)
        self.assertEqual(cm.exception.code, 1)
        env = self._envelope(out.getvalue(), "audit", "secrets", 1)
        self.assertIn("refusing to send", env["diagnosis"])

    def test_secrets_refusal_prose_path_is_byte_identical(self):
        with patched(amb.sys, argv=["ambient", "audit", "x.py"]):
            with self.assertRaises(SystemExit) as cm:
                amb.refuse_if_secrets(
                    [("s.py", 'api_key = "sk_live_abcdef1234567890"')], False)
        self.assertTrue(str(cm.exception).startswith(
            "ambient [secrets]: refusing to send"))

    def test_internal_floor_emits_envelope_under_json_argv(self):
        def boom():
            raise ValueError(f"wires crossed near {KEY}")
        out = io.StringIO()
        with patched(amb, main=boom,
                     resolve_key_and_backend=lambda conf: (KEY, "env")), \
                patched(amb.sys, argv=["ambient", "ask", "hi", "--json"]), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.shielded_main()
        self.assertEqual(cm.exception.code, 1)
        env = self._envelope(out.getvalue(), "ask", "internal", 1)
        self.assertIn("unexpected error", env["diagnosis"])
        self.assertNotIn(KEY, out.getvalue())  # floor still redacts the key

    def test_internal_floor_prose_path_is_byte_identical(self):
        def boom():
            raise ValueError("wires crossed")
        with patched(amb, main=boom,
                     resolve_key_and_backend=lambda conf: ("", "env")), \
                patched(amb.sys, argv=["ambient", "ask", "hi"]):
            with self.assertRaises(SystemExit) as cm:
                amb.shielded_main()
        self.assertEqual(str(cm.exception), (
            "ambient [internal]: unexpected error (ValueError: wires crossed). "
            "Nothing was harmed. Run 'ambient doctor' to check the basics; set "
            "AMBIENT_DEBUG=1 to see full details."))

    def test_spend_ceiling_emits_envelope_under_json_argv(self):
        out = io.StringIO()
        args = _base_ns(allow_cost=False)
        with patched(amb.sys, argv=["ambient", "code", "x", "--json"]), \
                patched(amb.os, environ={**os.environ,
                                         "AMBIENT_MAX_SPEND": "1"}), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb._gate_amount(50.0, args, {})
        self.assertEqual(cm.exception.code, 1)
        env = self._envelope(out.getvalue(), "code", "cost", 1)
        self.assertIn("spend", env["diagnosis"])


if __name__ == "__main__":
    unittest.main()
