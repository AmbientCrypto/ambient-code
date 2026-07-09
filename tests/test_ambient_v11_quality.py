"""Hermetic tests — quality-from-cheapness.

7a  --best-of K (ask/code/audit): K independent salted samples over the
    shared pool, ONE up-front cost gate for the K calls, cache-resume via
    per-sample salt, corroboration-ranked audit findings.
7b  ask --consensus A,B: the same ask on N explicit models, one summed
    up-front gate, per-model answers + an agreement/divergence note.
7c  ambient chat: TTY-only readline REPL — /model (SACRED, printed),
    /clear, /exit, streamed replies, per-turn receipt, Ctrl-C interrupts
    only the current turn.
7d  audit --install-hook/--uninstall-hook: a FIXED shell script (never
    model output) in .git/hooks that runs `ambient audit`; refuses to
    clobber a foreign hook without --force; uninstall removes only ours.

Every test patches complete/run_one_audit/safe_catalog/cost gates —
no network, no live API, no writes outside tempdirs.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v11p7", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v11p7", loader)
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
                temperature=0.1, timeout=30, raw=False, fallback=False,
                allow_partial=False, allow_cost=True, yes=True,
                no_cache=False, cache_ttl=None, parallel=None,
                reduce_model=None, best_of=None, consensus=None)
    base.update(kw)
    return argparse.Namespace(**base)


def audit_args(**kw):
    base = dict(paths=[], staged=False, diff=None, focus=None,
                allow_secrets=False, format="prose", dry_run=False,
                consensus=None, model="cheap/model", max_tokens=None,
                temperature=0.1, timeout=30, raw=False, fallback=False,
                allow_partial=False, allow_cost=True, yes=True,
                no_cache=True, cache_ttl=None, parallel=None,
                reduce_model=None, json=False, repo=None, deep=None,
                best_of=None, install_hook=None, uninstall_hook=None,
                force=False)
    base.update(kw)
    return argparse.Namespace(**base)


def chat_args(**kw):
    base = dict(system=None, model="cheap/model", max_tokens=None,
                temperature=0.1, timeout=30, raw=False, fallback=False,
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
    """Stands in for amb.complete — records every call, hands back canned
    answers (round-robin over `answers`)."""

    def __init__(self, answers=("answer A",), usage=None, raise_exc=None,
                 raise_for_models=(), stream=False):
        self.calls = []
        self.answers = list(answers)
        self.usage = usage or {"prompt_tokens": 10, "completion_tokens": 5}
        self.raise_exc = raise_exc
        self.raise_for_models = raise_for_models
        self.stream = stream

    def __call__(self, api_key, api_url, model, messages, args, **kw):
        self.calls.append({"model": model, "messages": messages,
                           "temperature": getattr(args, "temperature", None),
                           "on_delta": kw.get("on_delta")})
        if self.raise_exc is not None and (
                not self.raise_for_models or model in self.raise_for_models):
            raise self.raise_exc
        text = self.answers[(len(self.calls) - 1) % len(self.answers)]
        if self.stream and kw.get("on_delta"):
            kw["on_delta"](text)
        usage = dict(self.usage)
        return text, usage, {"usage": usage, "finish_reason": "stop"}


class GateRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))


def run_ask(args, complete, cache_dir, gate=None, extra=None):
    gate = gate if gate is not None else GateRecorder()
    out, err = io.StringIO(), io.StringIO()
    patches = dict(
        safe_catalog=lambda *a, **k: fake_catalog(),
        complete=complete, cost_gate=gate, _gate_amount=GateRecorder(),
        warn_if_stdin_ignored=lambda *a, **k: None,
        read_stdin_if_piped=lambda: "",
        CACHE_DIR=cache_dir,
    )
    if extra:
        patches.update(extra)
    with patched(amb, **patches), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        amb.cmd_ask(args, KEY, "https://api.ambient.xyz", {})
    return out.getvalue(), err.getvalue(), gate


# ---------------------------------------------------------------- 7a best-of

class BestOfAskTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_k_samples_one_gate_salted_cache(self):
        rec = CompleteRecorder(answers=["yes.", "yes.", "no."])
        out, err, gate = run_ask(ask_args(best_of=3), rec, self.cache)
        self.assertEqual(len(rec.calls), 3)          # K independent samples
        self.assertEqual(len(gate.calls), 1)          # ONE up-front gate
        # gate covered all K calls
        self.assertEqual(gate.calls[0][0][3], 3)      # n_calls arg of cost_gate
        # 3 distinct salted cache entries
        entries = [e for e in os.listdir(self.cache) if e.endswith(".json")]
        self.assertEqual(len(entries), 3)
        self.assertIn("best-of", out)

    def test_resume_serves_cached_samples(self):
        rec = CompleteRecorder(answers=["yes.", "yes.", "no."])
        run_ask(ask_args(best_of=3), rec, self.cache)
        rec2 = CompleteRecorder(answers=["never used"])
        out, err, gate2 = run_ask(ask_args(best_of=3), rec2, self.cache)
        self.assertEqual(len(rec2.calls), 0)          # all K from cache
        self.assertEqual(len(gate2.calls), 0)         # nothing to gate
        self.assertIn("best-of", out)

    def test_majority_selection_reported(self):
        rec = CompleteRecorder(answers=["42", "42", "17"])
        out, _err, _g = run_ask(ask_args(best_of=3, json=True), rec, self.cache)
        env = json.loads(out)
        self.assertEqual(env["schema_version"], 1)
        self.assertEqual(env["best_of"], 3)
        self.assertEqual(len(env["candidates"]), 3)
        self.assertIn("selected_index", env)
        self.assertEqual(env["content"], "42")
        self.assertEqual(env["selection"]["method"], "majority")

    def test_temperature_zero_bumped_for_diversity(self):
        rec = CompleteRecorder(answers=["a", "b"])
        run_ask(ask_args(best_of=2, temperature=0.0), rec, self.cache)
        self.assertTrue(all(c["temperature"] == 0.7 for c in rec.calls))

    def test_temperature_above_zero_kept(self):
        rec = CompleteRecorder(answers=["a", "b"])
        run_ask(ask_args(best_of=2, temperature=0.5), rec, self.cache)
        self.assertTrue(all(c["temperature"] == 0.5 for c in rec.calls))

    def test_k_below_2_is_usage_error(self):
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stderr(io.StringIO()):
            run_ask(ask_args(best_of=1), CompleteRecorder(), self.cache)
        self.assertEqual(cm.exception.code, 64)

    def test_k_clamped_to_max(self):
        rec = CompleteRecorder(answers=["x"])
        with contextlib.redirect_stderr(io.StringIO()):
            run_ask(ask_args(best_of=20), rec, self.cache)
        self.assertEqual(len(rec.calls), amb.BEST_OF_MAX)


class BestOfAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "a.py")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("def f(x):\n    return x + 1\n" * 20)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args, stub):
        gate = GateRecorder()
        out, err = io.StringIO(), io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: fake_catalog(),
                     run_one_audit=stub, _gate_amount=gate,
                     warn_if_stdin_ignored=lambda *a, **k: None,
                     read_stdin_if_piped=lambda: ""), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            amb.cmd_audit(args, KEY, "https://api.ambient.xyz", {})
        return out.getvalue(), err.getvalue(), gate

    def test_corroborated_finding_ranks_first_with_vote_count(self):
        shared = {"file": "a.py", "line": 5, "severity": "MEDIUM",
                  "title": "shared race condition", "confidence": "high"}
        shared2 = {"file": "a.py", "line": 6, "severity": "MEDIUM",
                   "title": "shared race condition here"}
        unique = {"file": "a.py", "line": 40, "severity": "CRITICAL",
                  "title": "unique overflow bug"}
        per_sample = {0: [dict(shared)], 1: [dict(unique)], 2: [dict(shared2)]}

        def stub(model, catalog, labeled, sys_prompt, args, api_key, api_url,
                 conf, gate=None, cancel_event=None, session=None):
            idx = int(args._cache_salt.rsplit(":", 1)[1])
            return per_sample[idx], True

        out, _err, gate = self._run(
            audit_args(paths=[self.src], best_of=3, format="json"), stub)
        env = json.loads(out)
        self.assertEqual(env["best_of"], 3)
        self.assertEqual(len(gate.calls), 1)          # ONE up-front gate for K
        findings = env["findings"]
        self.assertEqual(findings[0]["corroboration"]["count"], 2)
        self.assertIn("race", findings[0]["title"])
        self.assertEqual(findings[1]["corroboration"]["count"], 1)

    def test_each_sample_gets_a_distinct_cache_salt(self):
        salts = []

        def stub(model, catalog, labeled, sys_prompt, args, api_key, api_url,
                 conf, gate=None, cancel_event=None, session=None):
            salts.append(args._cache_salt)
            return [], True

        self._run(audit_args(paths=[self.src], best_of=3, format="json"), stub)
        self.assertEqual(len(set(salts)), 3)

    def test_best_of_conflicts_with_consensus(self):
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stderr(io.StringIO()):
            self._run(audit_args(paths=[self.src], best_of=3,
                                 consensus="cheap/model,other/model"),
                      lambda *a, **k: ([], True))
        self.assertEqual(cm.exception.code, 64)


# ---------------------------------------------------------- 7b ask consensus

class AskConsensusTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def _run(self, args, complete):
        gate = GateRecorder()
        out, err = io.StringIO(), io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: fake_catalog(),
                     complete=complete, _gate_amount=gate,
                     warn_if_stdin_ignored=lambda *a, **k: None,
                     read_stdin_if_piped=lambda: "",
                     CACHE_DIR=self.cache), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            amb.cmd_ask(args, KEY, "https://api.ambient.xyz", {})
        return out.getvalue(), err.getvalue(), gate

    def test_runs_every_model_one_summed_gate_agreement_note(self):
        rec = CompleteRecorder(answers=["The answer is 42."])
        out, err, gate = self._run(
            ask_args(consensus="cheap/model,other/model"), rec)
        models = {c["model"] for c in rec.calls}
        self.assertEqual(models, {"cheap/model", "other/model"})
        self.assertEqual(len(gate.calls), 1)          # one summed up-front gate
        self.assertIn("cheap/model", out)
        self.assertIn("other/model", out)
        self.assertIn("greement", out + err)          # agreement note emitted

    def test_json_envelope_answers_and_agreement(self):
        rec = CompleteRecorder(answers=["The answer is 42."])
        out, _err, _g = self._run(
            ask_args(consensus="cheap/model,other/model", json=True), rec)
        env = json.loads(out)
        self.assertEqual(env["schema_version"], 1)
        self.assertEqual(env["consensus"], ["cheap/model", "other/model"])
        self.assertEqual(len(env["answers"]), 2)
        self.assertIn(env["agreement"]["level"], ("high", "medium", "low"))
        self.assertEqual(env["agreement"]["level"], "high")

    def test_funds_error_fails_fast(self):
        rec = CompleteRecorder(
            raise_exc=amb.ChatError("funds", "out of funds"))
        with self.assertRaises((amb.ChatError, SystemExit)):
            self._run(ask_args(consensus="cheap/model,other/model"), rec)

    def test_one_failed_model_is_partial_not_clean(self):
        rec = CompleteRecorder(
            answers=["fine answer"],
            raise_exc=amb.ChatError("stall", "workers unhealthy"),
            raise_for_models=("other/model",))
        with self.assertRaises(SystemExit) as cm:
            self._run(ask_args(consensus="cheap/model,other/model"), rec)
        self.assertEqual(cm.exception.code, amb.EXIT_PARTIAL)

    def test_consensus_plus_best_of_refused(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(ask_args(consensus="cheap/model,other/model",
                               best_of=2), CompleteRecorder())
        self.assertEqual(cm.exception.code, 64)

    def test_unknown_model_refused_before_any_call(self):
        rec = CompleteRecorder()
        with self.assertRaises(SystemExit):
            self._run(ask_args(consensus="cheap/model,nope/nope"), rec)
        self.assertEqual(len(rec.calls), 0)


# ------------------------------------------------------------------- 7c chat

class ScriptedInput:
    def __init__(self, lines):
        self.lines = list(lines)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        nxt = self.lines.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


class ChatReplTests(unittest.TestCase):
    def _run(self, lines, complete, args=None, extra=None):
        script = ScriptedInput(lines)
        out, err = io.StringIO(), io.StringIO()
        patches = dict(
            safe_catalog=lambda *a, **k: fake_catalog(),
            complete=complete, _gate_amount=GateRecorder(),
            _stdin_is_tty=lambda: True, _chat_input=script,
        )
        if extra:
            patches.update(extra)
        with patched(amb, **patches), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            amb.cmd_chat(args or chat_args(), KEY,
                         "https://api.ambient.xyz", {})
        return out.getvalue(), err.getvalue(), script

    def test_non_tty_is_a_clean_usage_error_pointing_at_ask(self):
        err = io.StringIO()
        with patched(amb, _stdin_is_tty=lambda: False,
                     safe_catalog=lambda *a, **k: fake_catalog()), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as cm:
            amb.cmd_chat(chat_args(), KEY, "https://api.ambient.xyz", {})
        self.assertEqual(cm.exception.code, 64)
        self.assertIn("ambient ask", err.getvalue())

    def test_turn_streams_and_prints_receipt(self):
        rec = CompleteRecorder(answers=["hi there"], stream=True)
        out, err, _s = self._run(["hello", "/exit"], rec)
        self.assertEqual(len(rec.calls), 1)
        self.assertIsNotNone(rec.calls[0]["on_delta"])   # streaming requested
        self.assertIn("hi there", out)
        self.assertIn("[ambient cheap/model", err)       # per-turn receipt
        self.assertIn("tokens", err)

    def test_model_switch_is_explicit_and_printed(self):
        rec = CompleteRecorder(answers=["ok"])
        out, err, _s = self._run(
            ["/model other/model", "hello", "/exit"], rec)
        self.assertEqual(rec.calls[0]["model"], "other/model")
        self.assertIn("other/model", err)                # switch printed

    def test_ctrl_c_interrupts_turn_without_exiting(self):
        rec = CompleteRecorder(raise_exc=KeyboardInterrupt())
        out, err, script = self._run(["boom", "/exit"], rec)
        self.assertEqual(len(rec.calls), 1)              # the turn ran
        self.assertIn("interrupt", err.lower())
        # the REPL survived to consume /exit (no exception escaped)
        self.assertEqual(script.lines, [])

    def test_clear_resets_history(self):
        rec = CompleteRecorder(answers=["reply one", "reply two"])
        self._run(["first turn", "/clear", "second turn", "/exit"], rec)
        second = rec.calls[1]["messages"]
        joined = " ".join(m["content"] for m in second)
        self.assertNotIn("first turn", joined)
        self.assertNotIn("reply one", joined)
        self.assertIn("second turn", joined)

    def test_history_carries_between_turns(self):
        rec = CompleteRecorder(answers=["reply one", "reply two"])
        self._run(["first turn", "second turn", "/exit"], rec)
        second = rec.calls[1]["messages"]
        joined = " ".join(m["content"] for m in second)
        self.assertIn("first turn", joined)
        self.assertIn("reply one", joined)

    def test_chat_error_does_not_kill_the_repl(self):
        rec = CompleteRecorder(
            raise_exc=amb.ChatError("model", "no workers"))
        out, err, script = self._run(["hello", "/exit"], rec)
        self.assertIn("no workers", err)
        self.assertEqual(script.lines, [])


# ----------------------------------------------------------- 7d install-hook

def make_git_repo():
    tmp = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", tmp], check=True,
                   capture_output=True)
    return tmp


class InstallHookTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo()
        self.hooks = os.path.join(self.repo, ".git", "hooks")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def _hook(self, name="pre-commit"):
        return os.path.join(self.hooks, name)

    def test_install_writes_fixed_executable_hook(self):
        out = io.StringIO()
        with chdir(self.repo), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit_hook(hook_args(install_hook="pre-commit"))
        path = self._hook()
        self.assertTrue(os.path.isfile(path))
        if os.name != "nt":
            # Windows has no unix exec bit (os.chmod only toggles read-only);
            # git-for-windows runs hooks via its bundled bash regardless.
            mode = os.stat(path).st_mode
            self.assertTrue(mode & stat.S_IXUSR)
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        # FIXED script: byte-identical to the compiled-in template — never
        # model output, and it runs `ambient audit` on the staged diff.
        self.assertEqual(body, amb._render_hook("pre-commit"))
        self.assertIn(amb.AMBIENT_HOOK_MARKER, body)
        self.assertIn("ambient audit --staged --json", body)
        self.assertTrue(body.startswith("#!/bin/sh"))
        # tells the user how to bypass
        self.assertIn("--no-verify", out.getvalue() + body)

    def test_install_pre_push_variant(self):
        with chdir(self.repo), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit_hook(hook_args(install_hook="pre-push"))
        with open(self._hook("pre-push")) as fh:
            body = fh.read()
        self.assertIn("ambient audit --diff", body)

    def test_refuses_to_clobber_foreign_hook_without_force(self):
        os.makedirs(self.hooks, exist_ok=True)
        with open(self._hook(), "w") as fh:
            fh.write("#!/bin/sh\necho custom hook\n")
        with chdir(self.repo), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            amb.cmd_audit_hook(hook_args(install_hook="pre-commit"))
        with open(self._hook()) as fh:
            self.assertIn("custom hook", fh.read())     # untouched

    def test_force_replaces_and_backs_up_foreign_hook(self):
        os.makedirs(self.hooks, exist_ok=True)
        with open(self._hook(), "w") as fh:
            fh.write("#!/bin/sh\necho custom hook\n")
        with chdir(self.repo), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit_hook(hook_args(install_hook="pre-commit",
                                         force=True))
        with open(self._hook()) as fh:
            self.assertIn(amb.AMBIENT_HOOK_MARKER, fh.read())
        with open(self._hook() + ".pre-ambient.bak") as fh:
            self.assertIn("custom hook", fh.read())     # original preserved

    def test_uninstall_removes_only_ours(self):
        with chdir(self.repo), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit_hook(hook_args(install_hook="pre-commit"))
            amb.cmd_audit_hook(hook_args(uninstall_hook="pre-commit"))
        self.assertFalse(os.path.exists(self._hook()))

    def test_uninstall_refuses_foreign_hook(self):
        os.makedirs(self.hooks, exist_ok=True)
        with open(self._hook(), "w") as fh:
            fh.write("#!/bin/sh\necho custom hook\n")
        with chdir(self.repo), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            amb.cmd_audit_hook(hook_args(uninstall_hook="pre-commit"))
        self.assertTrue(os.path.exists(self._hook()))

    def test_outside_a_repo_is_a_usage_error(self):
        plain = tempfile.mkdtemp()
        try:
            with chdir(plain), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as cm:
                amb.cmd_audit_hook(hook_args(install_hook="pre-commit"))
            self.assertEqual(cm.exception.code, 64)
        finally:
            shutil.rmtree(plain, ignore_errors=True)

    def test_e2e_install_needs_no_api_key(self):
        # `ambient audit --install-hook` is pure hooks-file management: it
        # must work before any credentials exist (subprocess, clean HOME).
        home = tempfile.mkdtemp()
        try:
            env = dict(os.environ, HOME=home)
            env.pop("AMBIENT_API_KEY", None)
            proc = subprocess.run(
                [sys.executable, BIN, "audit", "--install-hook"],
                cwd=self.repo, env=env, capture_output=True, text=True,
                timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(os.path.isfile(self._hook()))
        finally:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
