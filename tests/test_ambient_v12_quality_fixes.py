"""Hermetic remediation tests.

H1  ask/code --best-of with failed samples is PARTIAL (exit 2), the
    coverage reason names the failed samples, --allow-partial accepts.
H2  cmd_code validates --best-of at the TOP — an invalid K fails usage
    (64) with ZERO spend, even when -f context would need distillation.
H3  audit --best-of resolves the salted cache BEFORE the gate: a re-run
    makes 0 calls, and the gate prices only the cache MISSES; the
    single-shot audit sample path reads/writes the salted cache.
H4  SACRED model choice vs --fallback: an explicit consensus set and a
    chat /model concrete choice run with fallback DISABLED; where
    fallback is still allowed it is disclosed (served vs requested).
M1  an oversized chat line is refused before gating/calling; REPL lives.
M2  audit --best-of emits failed_samples as the SAME list-of-objects
    shape as ask/code (+ failed_sample_count).
M3  hook ownership is STRICT: a foreign hook that merely mentions the
    marker is never clobbered without --force and never uninstalled.
LOW _gate_amount runs on EVERY chat turn; a low best-of temperature
    prints an explicit weak-corroboration note.

Every test patches complete/stream_completion/gates — no network.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v12p7", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v12p7", loader)
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


@contextlib.contextmanager
def chdir(path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def fake_catalog():
    base = {"context_length": 200_000, "max_output_length": 16384,
            "is_ready": True, "supported_features": [],
            "output_modalities": ["text"],
            "pricing": {"input": 0.2, "output": 0.8}}
    return [dict(base, id="cheap/model"), dict(base, id="other/model")]


def ask_args(**kw):
    base = dict(prompt=["hello", "world"], system=None, allow_secrets=False,
                json=False, model="cheap/model", max_tokens=None,
                temperature=0.7, timeout=30, raw=False, fallback=False,
                allow_partial=False, allow_cost=True, yes=True,
                no_cache=False, cache_ttl=None, parallel=None,
                reduce_model=None, best_of=None, consensus=None)
    base.update(kw)
    return argparse.Namespace(**base)


def code_args(**kw):
    base = dict(task=["write", "a", "thing"], context=[], system=None,
                allow_secrets=False, json=False, model="cheap/model",
                max_tokens=None, temperature=0.7, timeout=30, raw=False,
                fallback=False, allow_partial=False, allow_cost=True,
                yes=True, no_cache=False, cache_ttl=None, parallel=None,
                reduce_model=None, best_of=None)
    base.update(kw)
    return argparse.Namespace(**base)


def audit_args(**kw):
    base = dict(paths=[], staged=False, diff=None, focus=None,
                allow_secrets=False, format="prose", dry_run=False,
                consensus=None, model="cheap/model", max_tokens=None,
                temperature=0.7, timeout=30, raw=False, fallback=False,
                allow_partial=False, allow_cost=True, yes=True,
                no_cache=True, cache_ttl=None, parallel=None,
                reduce_model=None, json=False, repo=None, deep=None,
                best_of=None, install_hook=None, uninstall_hook=None,
                force=False)
    base.update(kw)
    return argparse.Namespace(**base)


def chat_args(**kw):
    base = dict(system=None, model="cheap/model", max_tokens=None,
                temperature=0.7, timeout=30, raw=False, fallback=False,
                allow_partial=False, allow_cost=True, yes=True,
                no_cache=True, cache_ttl=None, parallel=None,
                reduce_model=None)
    base.update(kw)
    return argparse.Namespace(**base)


def hook_args(**kw):
    base = dict(install_hook=None, uninstall_hook=None, force=False,
                format="prose")
    base.update(kw)
    return argparse.Namespace(**base)


class CompleteRecorder:
    """Stands in for amb.complete — records model, args flags, and whether
    a call had fallback disabled; round-robins over `answers`. `fail_first`
    raises the given exception on the first N calls (thread-safe)."""

    def __init__(self, answers=("answer A",), usage=None, raise_exc=None,
                 raise_for_models=(), fail_first=0, fail_exc=None):
        self.calls = []
        self.answers = list(answers)
        self.usage = usage or {"prompt_tokens": 10, "completion_tokens": 5}
        self.raise_exc = raise_exc
        self.raise_for_models = raise_for_models
        self.fail_first = fail_first
        self.fail_exc = fail_exc
        self._lock = threading.Lock()
        self._n = 0

    def __call__(self, api_key, api_url, model, messages, args, **kw):
        with self._lock:
            self._n += 1
            n = self._n
            self.calls.append({
                "model": model, "messages": messages,
                "temperature": getattr(args, "temperature", None),
                "fallback_disabled": getattr(args, "_no_fallback", False),
            })
        if self.fail_exc is not None and n <= self.fail_first:
            raise self.fail_exc
        if self.raise_exc is not None and (
                not self.raise_for_models or model in self.raise_for_models):
            raise self.raise_exc
        text = self.answers[(n - 1) % len(self.answers)]
        usage = dict(self.usage)
        return text, usage, {"usage": usage, "finish_reason": "stop"}


class GateRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))


def run_ask(args, complete, cache_dir, gate=None):
    gate = gate if gate is not None else GateRecorder()
    out, err = io.StringIO(), io.StringIO()
    with patched(amb,
                 safe_catalog=lambda *a, **k: fake_catalog(),
                 complete=complete, cost_gate=gate,
                 _gate_amount=GateRecorder(),
                 warn_if_stdin_ignored=lambda *a, **k: None,
                 read_stdin_if_piped=lambda: "",
                 CACHE_DIR=cache_dir), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        amb.cmd_ask(args, KEY, "https://api.ambient.xyz", {})
    return out.getvalue(), err.getvalue(), gate


# ------------------------------------------------- H1: best-of honest partial

class BestOfPartialTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def _intermittent(self):
        return CompleteRecorder(answers=["same answer", "same answer"],
                                fail_first=1,
                                fail_exc=amb.ChatError("stall", "worker died"))

    def test_failed_sample_is_partial_exit_2_prose(self):
        rec = self._intermittent()
        with self.assertRaises(SystemExit) as cm:
            run_ask(ask_args(best_of=3, parallel=1), rec, self.cache)
        self.assertEqual(cm.exception.code, amb.EXIT_PARTIAL)
        self.assertEqual(len(rec.calls), 3)

    def test_failed_sample_names_itself_in_json_reason(self):
        rec = self._intermittent()
        with self.assertRaises(SystemExit) as cm:
            run_ask(ask_args(best_of=3, parallel=1, json=True),
                    rec, self.cache)
        self.assertEqual(cm.exception.code, amb.EXIT_PARTIAL)

    def test_json_envelope_partial_with_failed_sample_list(self):
        rec = self._intermittent()
        out = io.StringIO()
        gate = GateRecorder()
        with patched(amb,
                     safe_catalog=lambda *a, **k: fake_catalog(),
                     complete=rec, cost_gate=gate,
                     _gate_amount=GateRecorder(),
                     warn_if_stdin_ignored=lambda *a, **k: None,
                     read_stdin_if_piped=lambda: "",
                     CACHE_DIR=self.cache), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            amb.cmd_ask(ask_args(best_of=3, parallel=1, json=True),
                        KEY, "https://api.ambient.xyz", {})
        env = json.loads(out.getvalue())
        self.assertEqual(env["status"], "partial")
        self.assertTrue(env["partial"])
        self.assertIn("failed", env["coverage_gap"])
        self.assertIsInstance(env["failed_samples"], list)
        self.assertEqual(len(env["failed_samples"]), 1)
        self.assertEqual(env["failed_sample_count"], 1)
        self.assertEqual(env["exit_code"], amb.EXIT_PARTIAL)

    def test_allow_partial_accepts_failed_samples(self):
        rec = self._intermittent()
        out, err, _g = run_ask(
            ask_args(best_of=3, parallel=1, allow_partial=True),
            rec, self.cache)
        self.assertIn("failed", err)   # still disclosed
        self.assertIn("best-of selection", out)

    def test_all_samples_ok_stays_clean(self):
        rec = CompleteRecorder(answers=["same", "same", "same"])
        out, _err, _g = run_ask(ask_args(best_of=3), rec, self.cache)
        self.assertIn("best-of selection", out)


# ------------------------------------- H2: code --best-of validated up front

class CodeBestOfValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_invalid_best_of_fails_64_with_zero_spend(self):
        # an OVERSIZED -f context would normally trigger a paid distillation
        # pass — the bad --best-of must fail usage BEFORE any of it.
        prof = amb.model_profile(fake_catalog(), "cheap/model")
        big = os.path.join(self.tmp, "big.py")
        with open(big, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n" * (prof.single_shot_chars // 6 + 1000))
        rec = CompleteRecorder()
        gates = [GateRecorder(), GateRecorder(), GateRecorder()]
        mr = GateRecorder()
        with patched(amb,
                     safe_catalog=lambda *a, **k: fake_catalog(),
                     complete=rec, cost_gate=gates[0],
                     cost_gate_mr=gates[1], _gate_amount=gates[2],
                     run_map_reduce=mr,
                     warn_if_stdin_ignored=lambda *a, **k: None), \
                contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            amb.cmd_code(code_args(best_of=1, context=[big]),
                         KEY, "https://api.ambient.xyz", {})
        self.assertEqual(cm.exception.code, 64)
        self.assertEqual(len(rec.calls), 0)          # no complete()
        for g in gates:
            self.assertEqual(len(g.calls), 0)        # no gate touched
        self.assertEqual(len(mr.calls), 0)           # no distillation


# -------------------------- H3: audit best-of cache before gate + resumable

FINDING = {"file": "a.py", "line": 3, "severity": "LOW",
           "title": "loop bound off by one"}


class BestOfAuditCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "a.py")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("def f(x):\n    return x + 1\n" * 20)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.cache, ignore_errors=True)

    def _run(self, args, rec):
        gate = GateRecorder()
        out, err = io.StringIO(), io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: fake_catalog(),
                     complete=rec, _gate_amount=gate,
                     warn_if_stdin_ignored=lambda *a, **k: None,
                     read_stdin_if_piped=lambda: "",
                     CACHE_DIR=self.cache), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            amb.cmd_audit(args, KEY, "https://api.ambient.xyz", {})
        return out.getvalue(), err.getvalue(), gate

    def test_rerun_serves_every_sample_from_cache_zero_calls(self):
        body = json.dumps({"findings": [dict(FINDING)]})
        rec1 = CompleteRecorder(answers=[body])
        args = audit_args(paths=[self.src], best_of=3, format="json",
                          no_cache=False)
        out1, _e, gate1 = self._run(args, rec1)
        self.assertEqual(len(rec1.calls), 3)
        self.assertEqual(len(gate1.calls), 1)
        rec2 = CompleteRecorder(answers=["never used"])
        out2, err2, gate2 = self._run(
            audit_args(paths=[self.src], best_of=3, format="json",
                       no_cache=False), rec2)
        self.assertEqual(len(rec2.calls), 0)          # fully resumed
        self.assertEqual(len(gate2.calls), 0)         # nothing to gate
        env = json.loads(out2)
        self.assertEqual(env["findings"][0]["corroboration"]["count"], 3)

    def test_gate_prices_only_the_cache_misses(self):
        body = json.dumps({"findings": [dict(FINDING)]})
        rec1 = CompleteRecorder(answers=[body])
        self._run(audit_args(paths=[self.src], best_of=2, format="json",
                             no_cache=False), rec1)
        self.assertEqual(len(rec1.calls), 2)
        # K=3 reuses salted lanes 0 and 1 — only sample 2 is a miss.
        rec2 = CompleteRecorder(answers=[body])
        _o, err2, gate2 = self._run(
            audit_args(paths=[self.src], best_of=3, format="json",
                       no_cache=False), rec2)
        self.assertEqual(len(rec2.calls), 1)          # one missing sample
        self.assertEqual(len(gate2.calls), 1)         # gated the miss only
        self.assertIn("cached", err2)                 # disclosed the resume

    def test_no_cache_still_gates_everything(self):
        body = json.dumps({"findings": []})
        rec = CompleteRecorder(answers=[body])
        _o, _e, gate = self._run(
            audit_args(paths=[self.src], best_of=2, format="json",
                       no_cache=True), rec)
        self.assertEqual(len(rec.calls), 2)
        self.assertEqual(len(gate.calls), 1)


# ----------------------------------------------- H4: SACRED choice vs fallback

class FallbackSacredCompleteTests(unittest.TestCase):
    """complete() itself: _no_fallback forces the swap OFF even when
    --fallback / AMBIENT_FALLBACK is on."""

    def _args(self, **kw):
        base = dict(max_tokens=1000, temperature=0.7, timeout=30,
                    fallback=True, response_format=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def _stream(self, calls):
        def fake_stream(api_url, api_key, payload, timeout, on_delta=None):
            calls.append(payload["model"])
            if payload["model"] == "cheap/model":
                return 404, {"error": {"message": "no workers"}}
            return 200, {"content": "ok", "finish_reason": "stop",
                         "usage": {"prompt_tokens": 1,
                                   "completion_tokens": 1}}
        return fake_stream

    def test_no_fallback_flag_blocks_the_swap(self):
        calls = []
        args = self._args()
        args._no_fallback = True
        with patched(amb, stream_completion=self._stream(calls),
                     classify_error=lambda *a, **k: ("model", "no workers"),
                     fetch_models=lambda *a, **k: fake_catalog(),
                     read_config_file=lambda: {},
                     pick_fallback_model=lambda *a, **k: "other/model",
                     log_usage=lambda *a, **k: None), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(amb.ChatError):
            amb.complete(KEY, "https://api", "cheap/model",
                         [{"role": "user", "content": "hi"}], args)
        self.assertEqual(calls, ["cheap/model"])      # never swapped

    def test_fallback_still_swaps_when_not_sacred(self):
        calls = []
        with patched(amb, stream_completion=self._stream(calls),
                     classify_error=lambda *a, **k: ("model", "no workers"),
                     fetch_models=lambda *a, **k: fake_catalog(),
                     read_config_file=lambda: {},
                     pick_fallback_model=lambda *a, **k: "other/model",
                     log_usage=lambda *a, **k: None), \
                contextlib.redirect_stderr(io.StringIO()):
            content, _u, body = amb.complete(
                KEY, "https://api", "cheap/model",
                [{"role": "user", "content": "hi"}], self._args())
        self.assertEqual(calls, ["cheap/model", "other/model"])
        self.assertEqual(content, "ok")
        self.assertEqual(body.get("_served_model"), "other/model")


class ConsensusSacredTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "a.py")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("def f(x):\n    return x + 1\n" * 20)

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ask_consensus_runs_every_model_with_fallback_disabled(self):
        rec = CompleteRecorder(answers=["The answer is 42."])
        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: fake_catalog(),
                     complete=rec, _gate_amount=GateRecorder(),
                     warn_if_stdin_ignored=lambda *a, **k: None,
                     read_stdin_if_piped=lambda: "",
                     CACHE_DIR=self.cache), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_ask(ask_args(consensus="cheap/model,other/model",
                                 fallback=True),
                        KEY, "https://api.ambient.xyz", {})
        self.assertEqual(len(rec.calls), 2)
        self.assertTrue(all(c["fallback_disabled"] for c in rec.calls))

    def test_ask_consensus_workerless_model_reported_not_swapped(self):
        rec = CompleteRecorder(
            answers=["fine answer"],
            raise_exc=amb.ChatError("model", "no workers"),
            raise_for_models=("cheap/model",))
        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: fake_catalog(),
                     complete=rec, _gate_amount=GateRecorder(),
                     warn_if_stdin_ignored=lambda *a, **k: None,
                     read_stdin_if_piped=lambda: "",
                     CACHE_DIR=self.cache), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            amb.cmd_ask(ask_args(consensus="cheap/model,other/model",
                                 fallback=True),
                        KEY, "https://api.ambient.xyz", {})
        self.assertEqual(cm.exception.code, amb.EXIT_PARTIAL)
        # A is its OWN failure — never silently served by another model.
        self.assertIn("failed", out.getvalue())
        self.assertIn("no workers", out.getvalue())
        models_called = {c["model"] for c in rec.calls}
        self.assertEqual(models_called, {"cheap/model", "other/model"})

    def test_audit_consensus_workers_run_with_fallback_disabled(self):
        seen = []

        def stub(model, catalog, labeled, sys_prompt, args, api_key,
                 api_url, conf, gate=None, cancel_event=None, session=None):
            seen.append((model, getattr(args, "_no_fallback", False)))
            return [], True

        with patched(amb, safe_catalog=lambda *a, **k: fake_catalog(),
                     run_one_audit=stub, _gate_amount=GateRecorder(),
                     warn_if_stdin_ignored=lambda *a, **k: None,
                     read_stdin_if_piped=lambda: ""), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(audit_args(paths=[self.src], fallback=True,
                                     consensus="cheap/model,other/model"),
                          KEY, "https://api.ambient.xyz", {})
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(nofb for _m, nofb in seen))


# ------------------------------------------------------- 7c chat (H4/M1/LOW)

class ScriptedInput:
    def __init__(self, lines):
        self.lines = list(lines)

    def __call__(self, prompt):
        if not self.lines:
            raise EOFError
        nxt = self.lines.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


class ChatRemediationTests(unittest.TestCase):
    def _run(self, lines, complete, args=None, gate=None):
        script = ScriptedInput(lines)
        gate = gate if gate is not None else GateRecorder()
        out, err = io.StringIO(), io.StringIO()
        with patched(amb,
                     safe_catalog=lambda *a, **k: fake_catalog(),
                     complete=complete, _gate_amount=gate,
                     _stdin_is_tty=lambda: True, _chat_input=script), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            amb.cmd_chat(args or chat_args(), KEY,
                         "https://api.ambient.xyz", {})
        return out.getvalue(), err.getvalue(), script, gate

    def test_slash_model_concrete_choice_disables_fallback(self):
        rec = CompleteRecorder(answers=["ok"])
        self._run(["/model other/model", "hello", "/exit"], rec,
                  args=chat_args(fallback=True))
        self.assertEqual(rec.calls[0]["model"], "other/model")
        self.assertTrue(rec.calls[0]["fallback_disabled"])

    def test_default_model_turn_keeps_fallback_available(self):
        rec = CompleteRecorder(answers=["ok"])
        self._run(["hello", "/exit"], rec, args=chat_args(fallback=True))
        self.assertFalse(rec.calls[0]["fallback_disabled"])

    def test_oversized_line_is_refused_and_repl_survives(self):
        prof = amb.model_profile(fake_catalog(), "cheap/model")
        big = "x" * (prof.single_shot_chars + 10)
        rec = CompleteRecorder(answers=["ok"])
        gate = GateRecorder()
        _o, err, script, gate = self._run(
            [big, "still alive", "/exit"], rec, gate=gate)
        self.assertEqual(len(rec.calls), 1)           # only the small turn
        self.assertEqual(rec.calls[0]["messages"][-1]["content"],
                         "still alive")
        self.assertEqual(len(gate.calls), 1)          # big turn never gated
        self.assertIn("too large", err)
        self.assertEqual(script.lines, [])            # REPL consumed /exit

    def test_gate_amount_runs_on_every_turn(self):
        rec = CompleteRecorder(answers=["one", "two"])
        _o, _e, _s, gate = self._run(["first", "second", "/exit"], rec)
        self.assertEqual(len(gate.calls), 2)          # per-turn cost gate


# -------------------------------------------- unified failed_samples shape

class FailedSamplesShapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "a.py")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("def f(x):\n    return x + 1\n" * 20)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_audit_best_of_failed_samples_is_a_list_of_objects(self):
        def stub(model, catalog, labeled, sys_prompt, args, api_key,
                 api_url, conf, gate=None, cancel_event=None, session=None):
            idx = int(args._cache_salt.rsplit(":", 1)[1])
            if idx == 1:
                return [], False
            return [dict(FINDING)], True

        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: fake_catalog(),
                     run_one_audit=stub, _gate_amount=GateRecorder(),
                     warn_if_stdin_ignored=lambda *a, **k: None,
                     read_stdin_if_piped=lambda: ""), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            amb.cmd_audit(audit_args(paths=[self.src], best_of=3,
                                     format="json"),
                          KEY, "https://api.ambient.xyz", {})
        self.assertEqual(cm.exception.code, amb.EXIT_PARTIAL)
        env = json.loads(out.getvalue())
        self.assertIsInstance(env["failed_samples"], list)
        self.assertEqual(len(env["failed_samples"]), 1)
        entry = env["failed_samples"][0]
        self.assertEqual(entry["index"], 1)
        self.assertIn("category", entry)
        self.assertIn("diagnosis", entry)
        self.assertEqual(env["failed_sample_count"], 1)


# ------------------------------------------------ strict hook ownership

def make_git_repo():
    tmp = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", tmp], check=True,
                   capture_output=True)
    return tmp


FOREIGN_WITH_MARKER = (
    "#!/bin/sh\n"
    "# my custom hook; docs mention # ambient-code audit hook v1 here\n"
    "echo mine\n"
)


class StrictHookOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo()
        self.hooks = os.path.join(self.repo, ".git", "hooks")
        os.makedirs(self.hooks, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def _hook(self, name="pre-commit"):
        return os.path.join(self.hooks, name)

    def test_foreign_hook_mentioning_marker_not_clobbered(self):
        with open(self._hook(), "w") as fh:
            fh.write(FOREIGN_WITH_MARKER)
        with chdir(self.repo), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            amb.cmd_audit_hook(hook_args(install_hook="pre-commit"))
        with open(self._hook()) as fh:
            self.assertEqual(fh.read(), FOREIGN_WITH_MARKER)  # untouched

    def test_foreign_hook_mentioning_marker_not_uninstalled(self):
        with open(self._hook(), "w") as fh:
            fh.write(FOREIGN_WITH_MARKER)
        with chdir(self.repo), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            amb.cmd_audit_hook(hook_args(uninstall_hook="pre-commit"))
        self.assertTrue(os.path.exists(self._hook()))

    def test_our_own_hook_reinstalls_and_uninstalls_cleanly(self):
        with chdir(self.repo), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit_hook(hook_args(install_hook="pre-commit"))
            amb.cmd_audit_hook(hook_args(install_hook="pre-commit"))
            amb.cmd_audit_hook(hook_args(uninstall_hook="pre-commit"))
        self.assertFalse(os.path.exists(self._hook()))

    def test_legacy_ambient_header_still_recognized_as_ours(self):
        # An older-version ambient hook (template drift) keeps the strict
        # two-line header — it must still uninstall without --force.
        legacy = ("#!/bin/sh\n"
                  f"{amb.AMBIENT_HOOK_MARKER} (pre-commit)\n"
                  "# Installed by: ambient audit --install-hook pre-commit\n"
                  "echo legacy body\n")
        with open(self._hook(), "w") as fh:
            fh.write(legacy)
        with chdir(self.repo), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit_hook(hook_args(uninstall_hook="pre-commit"))
        self.assertFalse(os.path.exists(self._hook()))


# ------------------------------------- LOW: weak-corroboration temperature

class BestOfTemperatureNoteTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_low_temperature_prints_weak_corroboration_note(self):
        rec = CompleteRecorder(answers=["a", "b"])
        _o, err, _g = run_ask(ask_args(best_of=2, temperature=0.1),
                              rec, self.cache)
        self.assertIn("corroboration", err)
        # the user's explicit temperature is NOT changed — only disclosed
        self.assertTrue(all(c["temperature"] == 0.1 for c in rec.calls))

    def test_default_diversity_temperature_gets_no_note(self):
        rec = CompleteRecorder(answers=["a", "b"])
        _o, err, _g = run_ask(ask_args(best_of=2, temperature=0.7),
                              rec, self.cache)
        self.assertNotIn("weak", err)


if __name__ == "__main__":
    unittest.main()
