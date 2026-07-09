"""Hermetic tests: the --parallel/AMBIENT_MAX_PARALLEL fan-out
knob, the concurrent consensus lane (order-preserving), and the machine-
readable JSON error envelope on --json failures. No network, no live API."""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v3", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v3", loader)
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


@contextlib.contextmanager
def env_var(name, value):
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


class TestResolveParallel(unittest.TestCase):
    def test_default_is_max_parallel_chunks(self):
        with env_var("AMBIENT_MAX_PARALLEL", None):
            args = argparse.Namespace()
            self.assertEqual(amb._resolve_parallel(args),
                             amb.MAX_PARALLEL_CHUNKS)

    def test_flag_beats_env_beats_default(self):
        with env_var("AMBIENT_MAX_PARALLEL", "10"):
            self.assertEqual(
                amb._resolve_parallel(argparse.Namespace(parallel=5)), 5)
            # env used when the flag is absent
            self.assertEqual(
                amb._resolve_parallel(argparse.Namespace(parallel=None)), 10)

    def test_clamped_to_1_16(self):
        self.assertEqual(
            amb._resolve_parallel(argparse.Namespace(parallel=99)), 16)
        self.assertEqual(
            amb._resolve_parallel(argparse.Namespace(parallel=0)), 1)
        self.assertEqual(
            amb._resolve_parallel(argparse.Namespace(parallel=-7)), 1)
        with env_var("AMBIENT_MAX_PARALLEL", "1000"):
            self.assertEqual(
                amb._resolve_parallel(argparse.Namespace(parallel=None)), 16)

    def test_bad_values_fall_back_never_crash(self):
        with env_var("AMBIENT_MAX_PARALLEL", "banana"):
            self.assertEqual(
                amb._resolve_parallel(argparse.Namespace(parallel=None)),
                amb.MAX_PARALLEL_CHUNKS)
        # non-numeric flag value (defensive: argparse type=int prevents this
        # from the CLI, but a programmatic caller must not crash a paid run)
        with env_var("AMBIENT_MAX_PARALLEL", None):
            self.assertEqual(
                amb._resolve_parallel(argparse.Namespace(parallel="junk")),
                amb.MAX_PARALLEL_CHUNKS)

    def test_parallel_flag_exists_on_task_subparsers(self):
        # The shared flag helper must expose --parallel to ask/audit/code/build.
        p = argparse.ArgumentParser()
        amb.add_common_flags(p)
        args = p.parse_args(["--parallel", "4"])
        self.assertEqual(args.parallel, 4)


def _consensus_args(src, fmt="json"):
    return argparse.Namespace(
        paths=[src], staged=False, diff=None, focus=None, allow_secrets=False,
        format=fmt, dry_run=False,
        consensus="fake/model-a,fake/model-b", model=None,
        max_tokens=None, temperature=0.1, timeout=30, raw=False,
        fallback=False, allow_partial=False, allow_cost=True, yes=True,
        no_cache=True, cache_ttl=None, parallel=None)


def _consensus_catalog():
    return [
        {"id": "fake/model-a", "context_length": 200000,
         "max_output_length": 200000, "is_ready": True,
         "supported_features": ["reasoning"], "output_modalities": ["text"],
         "pricing": {"input": 1.0, "output": 4.0}},
        {"id": "fake/model-b", "context_length": 200000,
         "max_output_length": 200000, "is_ready": True,
         "supported_features": ["reasoning"], "output_modalities": ["text"],
         "pricing": {"input": 1.0, "output": 4.0}},
    ]


class TestParallelConsensus(unittest.TestCase):
    def test_models_run_concurrently_and_order_is_preserved(self):
        # Both fakes rendezvous on a Barrier(2): if the lane were still
        # sequential the first call would block alone and the barrier would
        # break — the test then fails loudly. Model B finishes FIRST (A sleeps
        # after the rendezvous), yet the aggregated findings must come out in
        # the caller's model order: A's finding before B's.
        d = tempfile.mkdtemp()
        src = os.path.join(d, "x.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("def f(a, b):\n    return a / b\n")
        barrier = threading.Barrier(2)
        done_order = []

        def fake_audit(model, catalog, labeled, sys_prompt, args,
                       api_key, api_url, conf, **kw):  # kw: gate/cancel_event
            barrier.wait(timeout=10)
            if model == "fake/model-a":
                import time
                time.sleep(0.2)
                finding = {"severity": "HIGH", "file": "x.py", "line": 10,
                           "title": "alpha bug", "scenario": "a"}
            else:
                finding = {"severity": "HIGH", "file": "x.py", "line": 50,
                           "title": "beta bug", "scenario": "b"}
            done_order.append(model)
            return [finding], True

        args = _consensus_args(src)
        buf = io.StringIO()
        with patched(amb,
                     safe_catalog=lambda *a, **k: _consensus_catalog(),
                     _gate_amount=lambda *a, **k: None,
                     run_one_audit=fake_audit), \
                contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, "key-abcdef123456", "https://x", {})
        # concurrency really happened: B (no sleep) completed before A
        self.assertEqual(done_order[0], "fake/model-b")
        env = json.loads(buf.getvalue())
        self.assertEqual(env["status"], "ok")
        findings = env["findings"]
        self.assertEqual(len(findings), 2)
        # original model order preserved: A's finding ranks before B's
        self.assertEqual(findings[0]["line"], 10)
        self.assertEqual(findings[0]["corroboration"]["models"],
                         ["fake/model-a"])
        self.assertEqual(findings[1]["line"], 50)

    def test_worker_exception_still_propagates(self):
        # A key/funds ChatError raised inside a threaded run_one_audit must
        # surface to the caller exactly as it did on the sequential path.
        d = tempfile.mkdtemp()
        src = os.path.join(d, "x.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("print('hi')\n")

        def fake_audit(model, *a, **k):
            raise amb.ChatError("key", "invalid key")

        args = _consensus_args(src)
        with patched(amb,
                     safe_catalog=lambda *a, **k: _consensus_catalog(),
                     _gate_amount=lambda *a, **k: None,
                     run_one_audit=fake_audit), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(amb.ChatError) as cm:
                amb.cmd_audit(args, "key-abcdef123456", "https://x", {})
        self.assertEqual(cm.exception.category, "key")


class TestJsonErrorEnvelope(unittest.TestCase):
    KEY = "sk-test-key-abcdef1234567890"

    def _run_main(self, argv, handler):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with patched(amb,
                     load_config=lambda: (self.KEY, "https://x", {}),
                     cmd_audit=handler), \
                patched(sys, argv=argv), \
                contextlib.redirect_stdout(buf_out), \
                contextlib.redirect_stderr(buf_err):
            with self.assertRaises(SystemExit) as cm:
                amb.main()
        return cm.exception.code, buf_out.getvalue(), buf_err.getvalue()

    def test_chat_error_on_json_audit_emits_error_envelope(self):
        def boom(args, api_key, api_url, conf):
            raise amb.ChatError("funds", f"balance empty for {self.KEY}")

        code, out, _err = self._run_main(
            ["ambient", "audit", "somefile.py", "--json"], boom)
        self.assertEqual(code, 1)
        env = json.loads(out)
        self.assertEqual(env["schema_version"], 1)
        self.assertEqual(env["kind"], "audit")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["category"], "funds")
        self.assertEqual(env["exit_code"], 1)
        self.assertIn("balance empty", env["diagnosis"])
        # the key must NEVER appear anywhere in the output
        self.assertNotIn(self.KEY, out)

    def test_network_error_on_json_audit_emits_error_envelope(self):
        def boom(args, api_key, api_url, conf):
            raise amb.NetworkError("connection refused")

        code, out, _err = self._run_main(
            ["ambient", "audit", "somefile.py", "--json"], boom)
        self.assertEqual(code, 1)
        env = json.loads(out)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["category"], "network")
        self.assertIn("connection refused", env["diagnosis"])

    def test_non_json_failure_keeps_the_stderr_path(self):
        def boom(args, api_key, api_url, conf):
            raise amb.ChatError("funds", "balance empty")

        code, out, _err = self._run_main(
            ["ambient", "audit", "somefile.py"], boom)
        # sys.exit(str) → the message IS the exit payload; stdout stays clean
        self.assertIsInstance(code, str)
        self.assertTrue(code.startswith("ambient [funds]:"))
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
