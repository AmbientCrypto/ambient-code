"""tests.

- H1: ask/code --best-of must never OVER-STATE a saving when samples were
  served by DIFFERENT models (--fallback): the true cost prices EACH
  sample's tokens at its OWN served model and sums; --json carries the
  per-served-model token split.
- H2: a chat turn's ASSEMBLED request (system + trimmed history + latest)
  must fit the model window before gating/sending — the 2000-char history
  floor must never bill a doomed over-window request.
- _hook_is_ours requires the EXACT header lines — a foreign hook whose
  line 3 merely shares the installed-by prefix is foreign.
- an unknown ask --consensus model is a USAGE error — exit 64 on both
  the --json and prose paths.
- LOW: cmd_code's --best-of 0→0.7 temperature bump applies to the
  GENERATION samples only, not the billed distillation passes.

The "~N% cheaper / costlier than a frontier model" receipt tail is opt-in and
OFF by default, so the mixed-served honesty tests enable it (AMBIENT_SAVINGS=on)
to render the comparison they assert on — the over-stating-a-saving hazard only
exists once that tail is shown. The JSON per-served-model split is unconditional
and needs no opt-in.
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

BIN = os.path.join(os.path.dirname(__file__), "..", "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v13p7", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v13p7", loader)
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


@contextlib.contextmanager
def env_var(name, value):
    """Set/clear an env var for the duration of a call — the saving receipt is
    gated on AMBIENT_SAVINGS, which the honest-pricing tests opt into."""
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


def fake_catalog():
    base = {"context_length": 200_000, "max_output_length": 16384,
            "is_ready": True, "supported_features": [],
            "output_modalities": ["text"]}
    return [dict(base, id="cheap/model",
                 pricing={"input": 0.2, "output": 0.8}),
            dict(base, id="other/model",
                 pricing={"input": 0.2, "output": 0.8}),
            dict(base, id="pricey/model",
                 pricing={"input": 10.0, "output": 40.0})]


def ask_args(**kw):
    base = dict(prompt=["hello", "world"], system=None, allow_secrets=False,
                json=False, model="cheap/model", max_tokens=None,
                temperature=0.7, timeout=30, raw=False, fallback=False,
                allow_partial=False, allow_cost=True, yes=True,
                no_cache=True, cache_ttl=None, parallel=None,
                reduce_model=None, best_of=None, consensus=None)
    base.update(kw)
    return argparse.Namespace(**base)


def code_args(**kw):
    base = dict(task=["write", "a", "thing"], context=[], system=None,
                allow_secrets=False, json=False, model="cheap/model",
                max_tokens=None, temperature=0.7, timeout=30, raw=False,
                fallback=False, allow_partial=False, allow_cost=True,
                yes=True, no_cache=True, cache_ttl=None, parallel=None,
                reduce_model=None, best_of=None)
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


class ServedRecorder:
    """complete() stand-in that reports a per-call SERVED model (fallback
    simulation) — call order is deterministic under parallel=1."""

    def __init__(self, served=("cheap/model",), answers=("same answer",),
                 usage=None):
        self.calls = []
        self.served = list(served)
        self.answers = list(answers)
        self.usage = usage or {"prompt_tokens": 1000,
                               "completion_tokens": 1000}
        self._lock = threading.Lock()

    def __call__(self, api_key, api_url, model, messages, args, **kw):
        with self._lock:
            n = len(self.calls)
            self.calls.append({
                "model": model, "messages": messages,
                "temperature": getattr(args, "temperature", None),
            })
        usage = dict(self.usage)
        return (self.answers[n % len(self.answers)], usage,
                {"usage": usage, "finish_reason": "stop",
                 "_served_model": self.served[n % len(self.served)]})


class GateRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))


def run_ask(args, complete, cache_dir):
    gate = GateRecorder()
    out, err = io.StringIO(), io.StringIO()
    with patched(amb,
                 safe_catalog=lambda *a, **k: fake_catalog(),
                 complete=complete, cost_gate=gate,
                 _gate_amount=GateRecorder(),
                 warn_if_stdin_ignored=lambda *a, **k: None,
                 read_stdin_if_piped=lambda: "",
                 CACHE_DIR=cache_dir,
                 _PRICING_CATALOG=fake_catalog(),
                 _REF_CACHE=(3.0, 15.0)), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        amb.cmd_ask(args, KEY, "https://api.ambient.xyz", {})
    return out.getvalue(), err.getvalue(), gate


# ------------------------------------------- H1: mixed-served best-of pricing

class BestOfMixedServedPricingTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_mixed_served_receipt_never_over_states_saving(self):
        # Sample 1 served by the requested cheap model, sample 2 fell back
        # to a PRICIER model. True cost (per-served): 0.001 + 0.05 =
        # 0.051 > the 0.036 frontier reference — there is NO saving.
        # Pricing the 4000-token aggregate at the selected (cheap) sample's
        # model would fabricate "saved 94%".
        rec = ServedRecorder(served=("cheap/model", "pricey/model"))
        # Opt in to the saving tail so the mixed-served pricing is rendered.
        with env_var("AMBIENT_SAVINGS", "on"):
            _out, err, _g = run_ask(ask_args(best_of=2, parallel=1), rec,
                                    self.cache)
        self.assertEqual(len(rec.calls), 2)
        receipt = [ln for ln in err.splitlines() if "tokens" in ln]
        self.assertTrue(receipt, f"no receipt line in stderr: {err!r}")
        self.assertNotIn("saved", receipt[-1])
        self.assertIn("costlier", receipt[-1])
        self.assertIn("mixed", receipt[-1])

    def test_mixed_served_json_carries_per_served_token_split(self):
        rec = ServedRecorder(served=("cheap/model", "pricey/model"))
        out, _err, _g = run_ask(
            ask_args(best_of=2, parallel=1, json=True), rec, self.cache)
        env = json.loads(out)
        split = env["usage_by_served_model"]
        self.assertEqual(split["cheap/model"]["prompt_tokens"], 1000)
        self.assertEqual(split["cheap/model"]["completion_tokens"], 1000)
        self.assertEqual(split["pricey/model"]["prompt_tokens"], 1000)
        self.assertEqual(split["pricey/model"]["completion_tokens"], 1000)
        self.assertEqual(env["usage"]["prompt_tokens"], 2000)
        self.assertEqual(env["usage"]["completion_tokens"], 2000)

    def test_single_served_receipt_still_claims_the_true_saving(self):
        rec = ServedRecorder(served=("cheap/model",))
        # Opt in to the saving tail so the honest single-served claim shows.
        with env_var("AMBIENT_SAVINGS", "on"):
            _out, err, _g = run_ask(ask_args(best_of=2, parallel=1), rec,
                                    self.cache)
        receipt = [ln for ln in err.splitlines() if "tokens" in ln]
        self.assertTrue(receipt)
        self.assertIn("cheaper", receipt[-1])        # honest claim survives
        self.assertNotIn("mixed", receipt[-1])


# ---------------------------------------- H2: chat assembled-request sizing

class ScriptedInput:
    def __init__(self, lines):
        self.lines = list(lines)

    def __call__(self, prompt):
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)


class ChatAssembledWindowTests(unittest.TestCase):
    def _run(self, lines, complete, args=None):
        script = ScriptedInput(lines)
        out, err = io.StringIO(), io.StringIO()
        with patched(amb,
                     safe_catalog=lambda *a, **k: fake_catalog(),
                     complete=complete, _gate_amount=GateRecorder(),
                     _stdin_is_tty=lambda: True, _chat_input=script), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            amb.cmd_chat(args or chat_args(), KEY,
                         "https://api.ambient.xyz", {})
        return out.getvalue(), err.getvalue()

    def test_near_window_turn_drops_history_so_request_fits(self):
        profile = amb.model_profile(fake_catalog(), "cheap/model")
        single = profile.single_shot_chars
        # Turn 1 leaves a 6000-char assistant reply in history; turn 2's
        # latest line nearly fills the window on its own. The 2000-char
        # trim floor keeps the reply, so the assembled request would be
        # ~5990 chars OVER the window — it must be dropped, not billed.
        big_reply = "r" * 6000
        latest = "y" * (single - 10)
        rec = ServedRecorder(answers=(big_reply, "ok"))
        self._run(["seed question", latest], rec)
        self.assertEqual(len(rec.calls), 2)
        sent = rec.calls[1]["messages"]
        total = sum(len(m.get("content", "")) for m in sent)
        self.assertLessEqual(total, single)           # never over the window
        self.assertIn(latest, [m["content"] for m in sent])  # latest survives
        self.assertNotIn(big_reply,
                         [m.get("content") for m in sent])   # history dropped

    def test_fitting_turn_keeps_history(self):
        rec = ServedRecorder(answers=("first reply", "ok"))
        self._run(["seed question", "follow-up"], rec)
        joined = " ".join(m["content"] for m in rec.calls[1]["messages"])
        self.assertIn("first reply", joined)          # history retained


# ------------------------------------------- exact hook-header ownership

def make_git_repo():
    tmp = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", tmp], check=True,
                   capture_output=True)
    return tmp


FOREIGN_PREFIX_SHARING = (
    "#!/bin/sh\n"
    "# ambient-code audit hook v1 (pre-commit)\n"
    "# Installed by: ambient audit --install-hook pre-commit"
    " (mycorp fork — custom pipeline)\n"
    "echo mine\n"
)


class ExactHookHeaderTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo()
        self.hooks = os.path.join(self.repo, ".git", "hooks")
        os.makedirs(self.hooks, exist_ok=True)
        self.path = os.path.join(self.hooks, "pre-commit")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_prefix_sharing_line3_is_foreign_not_clobbered(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(FOREIGN_PREFIX_SHARING)
        with chdir(self.repo), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            amb.cmd_audit_hook(hook_args(install_hook="pre-commit"))
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), FOREIGN_PREFIX_SHARING)  # untouched

    def test_prefix_sharing_line3_is_foreign_not_uninstalled(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(FOREIGN_PREFIX_SHARING)
        with chdir(self.repo), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            amb.cmd_audit_hook(hook_args(uninstall_hook="pre-commit"))
        self.assertTrue(os.path.exists(self.path))

    def test_exact_header_still_ours(self):
        exact = ("#!/bin/sh\n"
                 f"{amb.AMBIENT_HOOK_MARKER} (pre-commit)\n"
                 "# Installed by: ambient audit --install-hook pre-commit\n"
                 "echo legacy body\n")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(exact)
        with chdir(self.repo), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit_hook(hook_args(uninstall_hook="pre-commit"))
        self.assertFalse(os.path.exists(self.path))


# --------------------------------- unknown consensus model exits 64

class ConsensusUnknownModelUsageExitTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def _run(self, args):
        rec = ServedRecorder()
        out, err = io.StringIO(), io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: fake_catalog(),
                     complete=rec, _gate_amount=GateRecorder(),
                     warn_if_stdin_ignored=lambda *a, **k: None,
                     read_stdin_if_piped=lambda: "",
                     CACHE_DIR=self.cache), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as cm:
            amb.cmd_ask(args, KEY, "https://api.ambient.xyz", {})
        return out.getvalue(), err.getvalue(), cm.exception.code, rec

    def test_json_envelope_and_process_exit_64(self):
        out, _err, code, rec = self._run(
            ask_args(consensus="cheap/model,nope/nope", json=True))
        self.assertEqual(code, 64)
        env = json.loads(out)
        self.assertEqual(env["schema_version"], 1)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["category"], "usage")
        self.assertEqual(env["exit_code"], 64)
        self.assertEqual(len(rec.calls), 0)           # nothing run or billed

    def test_prose_exits_64(self):
        _out, err, code, rec = self._run(
            ask_args(consensus="cheap/model,nope/nope"))
        self.assertEqual(code, 64)
        self.assertIn("unknown consensus model", err)
        self.assertEqual(len(rec.calls), 0)


# ------------------- LOW: best-of temp bump scoped to generation (cmd_code)

class MapReduceRecorder:
    def __init__(self):
        self.temps = []

    def __call__(self, api_key, api_url, model, map_system, chunks, args,
                 reduce_system, single, **kw):
        self.temps.append(getattr(args, "temperature", None))
        return "distilled brief", False, None


class CodeBestOfTemperatureScopeTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_distillation_keeps_user_temperature_generation_bumped(self):
        single = amb.model_profile(fake_catalog(),
                                   "cheap/model").single_shot_chars
        rec = ServedRecorder(answers=("code A", "code B"))
        mr = MapReduceRecorder()
        out, err = io.StringIO(), io.StringIO()
        args = code_args(task=["x" * (single + 10)], best_of=2,
                         temperature=0.0, parallel=1)
        with patched(amb,
                     safe_catalog=lambda *a, **k: fake_catalog(),
                     complete=rec, cost_gate=GateRecorder(),
                     cost_gate_mr=GateRecorder(),
                     _gate_amount=GateRecorder(),
                     run_map_reduce=mr,
                     resolve_reduce_model=lambda *a, **k: "cheap/model",
                     warn_if_stdin_ignored=lambda *a, **k: None,
                     CACHE_DIR=self.cache), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            amb.cmd_code(args, KEY, "https://api.ambient.xyz", {})
        # the billed task-brief distillation ran at the USER's temperature…
        self.assertEqual(mr.temps, [0.0])
        # …while the K generation samples got the 0→0.7 diversity bump.
        self.assertEqual(len(rec.calls), 2)
        self.assertTrue(all(c["temperature"] == amb.BEST_OF_TEMPERATURE
                            for c in rec.calls), rec.calls)


if __name__ == "__main__":
    unittest.main()
