"""Hermetic tests: `ambient map` — the bulk per-item inference
lane. Every test patches complete()/_cache_*/safe_catalog; no network, no
live API, no writes outside tempdirs."""
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
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_map", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_map", loader)
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


def _catalog():
    return [{"id": "fake/model-a", "context_length": 200000,
             "max_output_length": 200000, "is_ready": True,
             "supported_features": ["reasoning"],
             "output_modalities": ["text"],
             "pricing": {"input": 1.0, "output": 4.0}}]


def _map_args(prompt, paths=(), **over):
    base = dict(
        prompt=prompt, paths=list(paths), jsonl=False, json=True,
        allow_secrets=False, model="fake/model-a", system=None,
        max_tokens=None, temperature=0.1, timeout=30, raw=False,
        fallback=False, allow_partial=False, allow_cost=True, yes=True,
        no_cache=True, cache_ttl=None, parallel=None)
    base.update(over)
    return argparse.Namespace(**base)


def _write_items(d, texts):
    paths = []
    for i, text in enumerate(texts):
        p = os.path.join(d, f"item{i}.txt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        paths.append(p)
    return paths


class _FakeCache:
    """Dict-backed stand-in for _cache_get/_cache_put."""

    def __init__(self):
        self.store = {}

    def get(self, key, ttl):
        return self.store.get(key)

    def put(self, key, text):
        self.store[key] = text


def run_map(args, *, complete, cache=None, stdin=None, gate_spy=None,
            extra=None):
    """Run cmd_map hermetically; returns (exit_code_or_None, stdout, stderr)."""
    cache = cache or _FakeCache()
    patches = dict(
        safe_catalog=lambda *a, **k: _catalog(),
        complete=complete,
        _cache_get=cache.get,
        _cache_put=cache.put,
        _gate_amount=lambda *a, **k: None,
    )
    if stdin is not None:
        patches["read_stdin_if_piped"] = lambda: stdin
        patches["warn_if_stdin_ignored"] = lambda hint: None
    if gate_spy is not None:
        patches["cost_gate"] = gate_spy
    if extra:
        patches.update(extra)
    out, err = io.StringIO(), io.StringIO()
    code = None
    with patched(amb, **patches), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            amb.cmd_map(args, KEY, "https://x", {})
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def envelopes(out):
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


class TestMapHappyPath(unittest.TestCase):
    def test_three_files_three_ok_envelopes_with_ids(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["alpha content", "beta content", "gamma content"])

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            # messages = [{system: prompt}, {user: item}]
            self.assertEqual(messages[0]["role"], "system")
            self.assertEqual(messages[0]["content"], "classify")
            self.assertEqual(messages[1]["role"], "user")
            return f"OUT:{messages[1]['content']}", {}, {}

        code, out, err = run_map(_map_args("classify", paths),
                                 complete=fake_complete)
        self.assertIsNone(code)  # all ok → clean return (exit 0)
        envs = envelopes(out)
        self.assertEqual(len(envs), 3)
        by_id = {e["id"]: e for e in envs}
        for p, text in zip(paths, ["alpha content", "beta content",
                                   "gamma content"]):
            self.assertIn(p, by_id)
            e = by_id[p]
            self.assertEqual(e["schema_version"], 1)
            self.assertEqual(e["kind"], "map")
            self.assertEqual(e["status"], "ok")
            self.assertEqual(e["content"], f"OUT:{text}")
            self.assertEqual(e["exit_code"], 0)
        self.assertIn("3 ok / 0 failed / 0 cached", err)

    def test_stdin_lines_are_items(self):
        def fake_complete(api_key, api_url, model, messages, args, **kw):
            return messages[1]["content"].upper(), {}, {}

        code, out, _err = run_map(_map_args("upcase"), complete=fake_complete,
                                  stdin="one\ntwo\n\nthree\n")
        self.assertIsNone(code)
        envs = envelopes(out)
        self.assertEqual(len(envs), 3)
        got = {e["id"]: e["content"] for e in envs}
        self.assertEqual(got, {1: "ONE", 2: "TWO", 3: "THREE"})

    def test_jsonl_stdin_uses_input_and_id_fields(self):
        def fake_complete(api_key, api_url, model, messages, args, **kw):
            return "ans:" + messages[1]["content"], {}, {}

        stdin = ('{"id": "q-7", "input": "what is up"}\n'
                 '{"input": "no id here"}\n')
        code, out, _err = run_map(_map_args("answer", jsonl=True),
                                  complete=fake_complete, stdin=stdin)
        self.assertIsNone(code)
        envs = envelopes(out)
        got = {e["id"]: e["content"] for e in envs}
        self.assertEqual(got, {"q-7": "ans:what is up", 2: "ans:no id here"})

    def test_non_json_mode_prints_headers_with_ids(self):
        def fake_complete(api_key, api_url, model, messages, args, **kw):
            return "the answer", {}, {}

        code, out, _err = run_map(_map_args("do it", json=False),
                                  complete=fake_complete, stdin="one item\n")
        self.assertIsNone(code)
        self.assertIn("the answer", out)
        self.assertIn("1", out)  # the item id appears in the header


class TestMapCostGate(unittest.TestCase):
    def test_one_upfront_gate_with_n_calls_equal_item_count(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["aaa", "bbb", "ccc"])
        calls = []

        def gate_spy(catalog, model, input_chars, n_calls, args, conf):
            calls.append((input_chars, n_calls))

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            # the gate must have fired BEFORE any network call
            self.assertEqual(len(calls), 1)
            return "ok", {}, {}

        code, _out, _err = run_map(_map_args("p", paths),
                                   complete=fake_complete, gate_spy=gate_spy)
        self.assertIsNone(code)
        self.assertEqual(len(calls), 1)  # ONE batch gate, not per item
        self.assertEqual(calls[0][1], 3)  # n_calls == item count
        self.assertGreaterEqual(calls[0][0], len("aaabbbccc"))


class TestMapFailureModes(unittest.TestCase):
    def test_fatal_funds_error_fails_fast_and_stops_billing(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["one", "two", "three", "four"])
        billed = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            billed.append(messages[1]["content"])
            raise amb.ChatError("funds", "balance empty")

        args = _map_args("p", paths, parallel=1)
        out, err = io.StringIO(), io.StringIO()
        with patched(amb,
                     safe_catalog=lambda *a, **k: _catalog(),
                     complete=fake_complete,
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(amb.ChatError) as cm:
                amb.cmd_map(args, KEY, "https://x", {})
        self.assertEqual(cm.exception.category, "funds")
        # fail-fast: the queued siblings were cancelled, NOT all billed
        self.assertEqual(len(billed), 1)

    def test_network_error_fails_fast(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["one", "two", "three"])
        billed = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            billed.append(1)
            raise amb.NetworkError("connection refused")

        args = _map_args("p", paths, parallel=1)
        with patched(amb,
                     safe_catalog=lambda *a, **k: _catalog(),
                     complete=fake_complete,
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(amb.NetworkError):
                amb.cmd_map(args, KEY, "https://x", {})
        self.assertEqual(len(billed), 1)

    def test_nonfatal_item_error_continues_batch_exit_partial(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["good one", "bad one", "good two"])

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            if "bad" in messages[1]["content"]:
                raise amb.ChatError("stall", "worker wedged")
            return "fine", {}, {}

        code, out, err = run_map(_map_args("p", paths, parallel=1),
                                 complete=fake_complete)
        self.assertEqual(code, amb.EXIT_PARTIAL)
        envs = envelopes(out)
        self.assertEqual(len(envs), 3)
        statuses = sorted(e["status"] for e in envs)
        self.assertEqual(statuses, ["error", "ok", "ok"])
        bad = next(e for e in envs if e["status"] == "error")
        self.assertEqual(bad["category"], "stall")
        self.assertIn("worker wedged", bad["diagnosis"])
        self.assertEqual(bad["exit_code"], 1)
        self.assertIsNone(bad["content"])
        self.assertIn("2 ok / 1 failed", err)

    def test_oversized_item_is_per_item_error_rest_succeed(self):
        d = tempfile.mkdtemp()
        single = amb.model_profile(_catalog(), "fake/model-a").single_shot_chars
        paths = _write_items(d, ["small a", "x" * (single + 100), "small b"])

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            self.assertLess(len(messages[1]["content"]), single)
            return "ok", {}, {}

        code, out, _err = run_map(_map_args("p", paths),
                                  complete=fake_complete)
        self.assertEqual(code, amb.EXIT_PARTIAL)
        envs = envelopes(out)
        self.assertEqual(len(envs), 3)
        big = next(e for e in envs if e["status"] == "error")
        self.assertEqual(big["id"], paths[1])
        self.assertIn("too large for map", big["diagnosis"])
        self.assertIn("ambient audit", big["diagnosis"])
        oks = [e for e in envs if e["status"] == "ok"]
        self.assertEqual(len(oks), 2)

    def test_directory_path_is_per_item_error_telling_user_to_glob(self):
        d = tempfile.mkdtemp()
        sub = os.path.join(d, "adir")
        os.mkdir(sub)
        paths = _write_items(d, ["fine"]) + [sub]

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            return "ok", {}, {}

        code, out, _err = run_map(_map_args("p", paths),
                                  complete=fake_complete)
        self.assertEqual(code, amb.EXIT_PARTIAL)
        envs = envelopes(out)
        dir_err = next(e for e in envs if e["id"] == sub)
        self.assertEqual(dir_err["status"], "error")
        self.assertIn("glob", dir_err["diagnosis"])

    def test_allow_partial_returns_zero_on_item_failure(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["good", "bad"])

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            if "bad" in messages[1]["content"]:
                raise amb.ChatError("stall", "wedged")
            return "fine", {}, {}

        code, _out, _err = run_map(
            _map_args("p", paths, parallel=1, allow_partial=True),
            complete=fake_complete)
        self.assertIsNone(code)


class TestMapCacheResume(unittest.TestCase):
    def test_rerun_serves_from_cache_and_rebills_only_missing(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["one", "two", "three"])
        cache = _FakeCache()
        calls = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            calls.append(messages[1]["content"])
            return "R:" + messages[1]["content"], {}, {}

        code, _o, _e = run_map(_map_args("p", paths, no_cache=False),
                               complete=fake_complete, cache=cache)
        self.assertIsNone(code)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(cache.store), 3)
        # Re-run: every item served from cache, complete() never called again.
        code, out, err = run_map(_map_args("p", paths, no_cache=False),
                                 complete=fake_complete, cache=cache)
        self.assertIsNone(code)
        self.assertEqual(len(calls), 3)
        envs = envelopes(out)
        self.assertEqual(sorted(e["content"] for e in envs),
                         ["R:one", "R:three", "R:two"])
        self.assertIn("3 cached", err)

    def test_no_cache_disables_reuse(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["one", "two"])
        cache = _FakeCache()
        calls = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            calls.append(1)
            return "r", {}, {}

        for _ in range(2):
            code, _o, _e = run_map(_map_args("p", paths, no_cache=True),
                                   complete=fake_complete, cache=cache)
            self.assertIsNone(code)
        self.assertEqual(len(calls), 4)  # nothing reused
        self.assertEqual(cache.store, {})  # nothing written either

    def test_partial_result_never_cached(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["one"])
        cache = _FakeCache()

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            return "half", {}, {"finish_reason": "length"}

        code, out, _e = run_map(_map_args("p", paths, no_cache=False),
                                complete=fake_complete, cache=cache)
        self.assertEqual(code, amb.EXIT_PARTIAL)
        self.assertEqual(cache.store, {})
        env = envelopes(out)[0]
        self.assertEqual(env["status"], "partial")
        self.assertEqual(env["exit_code"], amb.EXIT_PARTIAL)


class TestMapJsonContract(unittest.TestCase):
    def test_jsonl_output_one_object_per_line_key_redacted(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["a", "b"])

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            return f"leak {KEY} end", {}, {}

        code, out, _e = run_map(_map_args("p", paths),
                                complete=fake_complete)
        self.assertIsNone(code)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        for ln in lines:
            env = json.loads(ln)  # each LINE is one valid JSON object
            self.assertEqual(env["schema_version"], 1)
            self.assertNotIn(KEY, ln)
            self.assertIn("[AMBIENT_API_KEY]", env["content"])

    def test_empty_prompt_is_usage_error_64_with_envelope(self):
        code, out, _e = run_map(_map_args("   ", ["x.txt"]),
                                complete=lambda *a, **k: ("", {}, {}))
        self.assertEqual(code, amb.EXIT_USAGE)
        env = json.loads(out)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["kind"], "map")
        self.assertEqual(env["category"], "usage")
        self.assertEqual(env["exit_code"], amb.EXIT_USAGE)

    def test_no_items_is_usage_error_64_with_envelope(self):
        code, out, _e = run_map(_map_args("prompt"),
                                complete=lambda *a, **k: ("", {}, {}),
                                stdin="")
        self.assertEqual(code, amb.EXIT_USAGE)
        env = json.loads(out)
        self.assertEqual(env["category"], "usage")
        self.assertEqual(env["exit_code"], amb.EXIT_USAGE)

    def test_missing_prompt_via_main_argparse_emits_map_usage_envelope(self):
        out = io.StringIO()
        with patched(sys, argv=["ambient", "map", "--json"]), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.main()
        self.assertEqual(cm.exception.code, amb.EXIT_USAGE)
        env = json.loads(out.getvalue())
        self.assertEqual(env["kind"], "map")
        self.assertEqual(env["category"], "usage")

    def test_map_registered_in_parser_and_handlers(self):
        # `ambient map "p" f1 f2 --jsonl --parallel 4` must parse end-to-end
        # through main()'s wiring into the handlers dict.
        seen = {}

        def stub(args, api_key, api_url, conf):
            seen["prompt"] = args.prompt
            seen["paths"] = args.paths
            seen["jsonl"] = args.jsonl
            seen["parallel"] = args.parallel

        with patched(amb,
                     load_config=lambda: (KEY, "https://x", {}),
                     cmd_map=stub), \
                patched(sys, argv=["ambient", "map", "do it", "f1.py",
                                   "f2.py", "--jsonl", "--parallel", "4"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.main()
        self.assertEqual(seen["prompt"], "do it")
        self.assertEqual(seen["paths"], ["f1.py", "f2.py"])
        self.assertTrue(seen["jsonl"])
        self.assertEqual(seen["parallel"], 4)


class TestMapCacheBeforeGate(unittest.TestCase):
    """H1: the batch cost gate must price only the CACHE MISSES — a resumed
    (fully-cached) re-run makes zero calls and must never be re-priced or
    refused by AMBIENT_MAX_SPEND."""

    def _gate_spy(self, calls):
        def spy(catalog, model, input_chars, n_calls, args, conf):
            calls.append((input_chars, n_calls))
        return spy

    def test_warm_cache_rerun_never_calls_cost_gate(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["aaa", "bbb", "ccc"])
        cache = _FakeCache()
        gates = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            return "R:" + messages[1]["content"], {}, {}

        code, _o, _e = run_map(_map_args("p", paths, no_cache=False),
                               complete=fake_complete, cache=cache,
                               gate_spy=self._gate_spy(gates))
        self.assertIsNone(code)
        self.assertEqual(gates, [(gates[0][0], 3)])  # cold run: gate all 3
        # Warm re-run: every item cached → the gate must not fire AT ALL.
        code, out, err = run_map(_map_args("p", paths, no_cache=False),
                                 complete=fake_complete, cache=cache,
                                 gate_spy=self._gate_spy(gates))
        self.assertIsNone(code)
        self.assertEqual(len(gates), 1)  # ZERO gate calls on the warm run
        envs = envelopes(out)
        self.assertEqual(len(envs), 3)
        self.assertTrue(all(e.get("cached") for e in envs))
        self.assertIn("3 cached", err)

    def test_partial_cache_gates_only_the_misses(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["aaa", "bbb", "ccc"])
        cache = _FakeCache()
        gates = []
        billed = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            billed.append(messages[1]["content"])
            return "R:" + messages[1]["content"], {}, {}

        code, _o, _e = run_map(_map_args("p", paths[:2], no_cache=False),
                               complete=fake_complete, cache=cache,
                               gate_spy=self._gate_spy(gates))
        self.assertIsNone(code)
        self.assertEqual(gates[-1][1], 2)
        # Second run adds one NEW item: the gate prices exactly that one miss.
        code, _o, _e = run_map(_map_args("p", paths, no_cache=False),
                               complete=fake_complete, cache=cache,
                               gate_spy=self._gate_spy(gates))
        self.assertIsNone(code)
        self.assertEqual(len(gates), 2)
        self.assertEqual(gates[-1][1], 1)   # n_calls == len(misses)
        self.assertEqual(len(billed), 3)    # the hit items were never re-billed


class TestMapUsageExitCode(unittest.TestCase):
    """H2: map usage errors must exit 64 on the PROSE path too (the --json
    twin already does), with the message on stderr."""

    def test_map_usage_error_non_json_exits_64(self):
        code, out, err = run_map(
            _map_args("   ", ["x.txt"], json=False),
            complete=lambda *a, **k: ("", {}, {}))
        self.assertEqual(code, amb.EXIT_USAGE)
        self.assertEqual(out, "")
        self.assertIn("map needs a per-item instruction", err)

    def test_map_no_items_non_json_exits_64(self):
        code, _out, err = run_map(
            _map_args("prompt", json=False),
            complete=lambda *a, **k: ("", {}, {}), stdin="")
        self.assertEqual(code, amb.EXIT_USAGE)
        self.assertIn("no items to map", err)


class TestMapFatalGateRace(unittest.TestCase):
    """H3: a worker hitting a fatal (key/funds/network) error must set
    cancel_event BEFORE its gate slot is released, so a queued sibling that
    grabs the freed slot can never start a billed call."""

    def _run_fatal(self, fake_complete):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["one", "two"])
        state = {"event": None, "release_saw_cancel": []}

        class SpySem:
            def __init__(self, width):
                self._sem = threading.Semaphore(width)

            def __enter__(self):
                self._sem.acquire()
                return self

            def __exit__(self, *exc):
                state["release_saw_cancel"].append(state["event"].is_set())
                self._sem.release()
                return False

        def make_event():
            ev = threading.Event()
            state["event"] = ev
            return ev

        shim = types.SimpleNamespace(Semaphore=SpySem, Event=make_event)
        args = _map_args("p", paths, parallel=1)
        with patched(amb,
                     safe_catalog=lambda *a, **k: _catalog(),
                     complete=fake_complete,
                     _gate_amount=lambda *a, **k: None,
                     threading=shim), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((amb.ChatError, amb.NetworkError)):
                amb.cmd_map(args, KEY, "https://x", {})
        return state

    def test_funds_error_sets_cancel_before_gate_release(self):
        def fatal(api_key, api_url, model, messages, args, **kw):
            raise amb.ChatError("funds", "balance empty")

        state = self._run_fatal(fatal)
        self.assertTrue(state["release_saw_cancel"],
                        "the fatal worker never entered the gate")
        self.assertTrue(
            state["release_saw_cancel"][0],
            "cancel_event was NOT set before the gate slot was released — "
            "a queued worker could grab the slot and start billing")

    def test_network_error_sets_cancel_before_gate_release(self):
        def fatal(api_key, api_url, model, messages, args, **kw):
            raise amb.NetworkError("connection refused")

        state = self._run_fatal(fatal)
        self.assertTrue(state["release_saw_cancel"][0])


class TestMapFatalJsonOneLine(unittest.TestCase):
    """H4: under --json every map line — item results AND the terminal fatal
    error envelope — must be exactly ONE line of JSON (JSONL contract)."""

    def test_fatal_mid_batch_error_envelope_is_single_line(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["one", "two", "three"])
        calls = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            calls.append(1)
            if len(calls) == 1:
                return "fine", {}, {}
            raise amb.ChatError("funds", f"balance empty near {KEY}")

        out = io.StringIO()
        with patched(amb,
                     load_config=lambda: (KEY, "https://x", {}),
                     safe_catalog=lambda *a, **k: _catalog(),
                     complete=fake_complete,
                     _gate_amount=lambda *a, **k: None), \
                patched(sys, argv=["ambient", "map", "p", *paths,
                                   "--parallel", "1", "--json",
                                   "--no-cache", "--allow-cost"]), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.main()
        self.assertEqual(cm.exception.code, 1)
        lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        self.assertGreaterEqual(len(lines), 2)  # ≥1 item line + the error line
        for ln in lines:  # EVERY line is one complete JSON object
            json.loads(ln)
        err_env = json.loads(lines[-1])
        self.assertEqual(err_env["status"], "error")
        self.assertEqual(err_env["kind"], "map")
        self.assertEqual(err_env["category"], "funds")
        self.assertNotIn("\n", lines[-1])
        self.assertNotIn(KEY, out.getvalue())  # still redacted


class _FlushSpyIO(io.StringIO):
    """StringIO that counts flush() calls (io.StringIO is a C type — plain
    attribute assignment on an instance is not allowed, so subclass)."""

    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        super().flush()


class TestMapInterruptPrompt(unittest.TestCase):
    """M5 + MED: Ctrl-C must end the PROCESS promptly. The pool's
    worker threads are non-daemon, so merely re-raising KeyboardInterrupt
    leaves the interpreter joining an in-flight complete() at exit (up to
    --timeout). cmd_map must flush stdout+stderr and os._exit(130)."""

    def tearDown(self):
        # These tests drive the Ctrl-C path with a MOCKED os._exit, so the pool's
        # non-daemon workers survive the test (in production the real os._exit
        # kills them). Drain them so a leaked worker can't pollute a later test
        # by resolving a module-global that test re-patched (cross-test flake).
        deadline = time.monotonic() + 10.0
        for t in list(threading.enumerate()):
            if (t is threading.main_thread() or not t.is_alive()
                    or not t.name.startswith("ThreadPoolExecutor")):
                continue
            t.join(timeout=max(0.0, deadline - time.monotonic()))

    def _interrupt_run(self):
        """Drive cmd_map into the Ctrl-C path with one call still in flight.
        Returns (exit_codes, systemexit_code, elapsed, out_spy, err_spy)."""
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["boom", "slow"])
        slow_started = threading.Event()
        exit_codes = []

        def fake_exit(code):
            # Halt cmd_map exactly where a real os._exit would (nothing after
            # the call may run), but keep the test process alive.
            exit_codes.append(code)
            raise SystemExit(code)

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            if "boom" in messages[1]["content"]:
                # Only interrupt once the slow sibling is genuinely IN FLIGHT,
                # so the abort has something it could wrongly wait for.
                slow_started.wait(5)
                raise KeyboardInterrupt
            slow_started.set()
            time.sleep(2.0)  # an in-flight call the exit must NOT wait for
            return "late", {}, {}

        args = _map_args("p", paths, parallel=2)
        out_spy, err_spy = _FlushSpyIO(), _FlushSpyIO()
        start = time.monotonic()
        with patched(amb,
                     safe_catalog=lambda *a, **k: _catalog(),
                     complete=fake_complete,
                     _gate_amount=lambda *a, **k: None), \
                patched(amb.os, _exit=fake_exit), \
                contextlib.redirect_stdout(out_spy), \
                contextlib.redirect_stderr(err_spy):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_map(args, KEY, "https://x", {})
        elapsed = time.monotonic() - start
        return exit_codes, cm.exception.code, elapsed, out_spy, err_spy

    def test_keyboard_interrupt_exits_130_without_waiting(self):
        exit_codes, se_code, elapsed, _out, err = self._interrupt_run()
        self.assertEqual(exit_codes, [130],
                         "Ctrl-C must os._exit(130) so non-daemon pool "
                         "threads cannot stall process exit")
        self.assertEqual(se_code, 130)
        # Prompt exit returns in ~0s; the in-flight call sleeps 2.0s. Assert
        # well under that (1.5s margin absorbs CI load without timing sensitivity).
        self.assertLess(elapsed, 1.5,
                        "exit blocked draining an in-flight call")
        self.assertIn("cancelling map", err.getvalue())

    def test_keyboard_interrupt_flushes_streams_before_exit(self):
        exit_codes, _se, _elapsed, out, err = self._interrupt_run()
        self.assertEqual(exit_codes, [130])
        # os._exit skips interpreter teardown (no atexit / buffered-stream
        # flushing) — cmd_map itself must flush so no output is lost.
        self.assertGreaterEqual(out.flushes, 1,
                                "stdout was not flushed before os._exit")
        self.assertGreaterEqual(err.flushes, 1,
                                "stderr was not flushed before os._exit")


class TestMapInputCaps(unittest.TestCase):
    """M6/batch input is bounded — a cumulative total cap while gathering,
    and a PER-ITEM error (never a silent truncation) for a file bigger than
    the per-item ceiling."""

    def test_cumulative_input_cap_errors_cleanly(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["a" * 60, "b" * 60, "c" * 60])
        billed = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            billed.append(1)
            return "ok", {}, {}

        code, out, _e = run_map(_map_args("p", paths),
                                complete=fake_complete,
                                extra={"ABS_MAX_CHARS": 100})
        self.assertEqual(code, amb.EXIT_PARTIAL)
        envs = envelopes(out)
        self.assertEqual(len(envs), 3)
        by_id = {e["id"]: e for e in envs}
        self.assertEqual(by_id[paths[0]]["status"], "ok")
        for p in paths[1:]:
            self.assertEqual(by_id[p]["status"], "error")
            self.assertIn("cap", by_id[p]["diagnosis"])
        self.assertEqual(len(billed), 1)  # only the in-cap item was billed

    def test_oversize_file_errors_instead_of_silent_truncation(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["x" * 50, "y" * 300])

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            return "ok", {}, {}

        code, out, _e = run_map(_map_args("p", paths),
                                complete=fake_complete,
                                extra={"ABS_MAX_CHARS": 100})
        self.assertEqual(code, amb.EXIT_PARTIAL)
        envs = envelopes(out)
        by_id = {e["id"]: e for e in envs}
        self.assertEqual(by_id[paths[0]]["status"], "ok")
        big = by_id[paths[1]]
        self.assertEqual(big["status"], "error")
        self.assertIn("exceeds", big["diagnosis"])


class TestMapDensityBudget(unittest.TestCase):
    """the output budget must be sized by the same density-adjusted
    length the oversize refusal uses, so CJK/token-dense items are not
    under-budgeted."""

    def test_output_budget_uses_density_adjusted_size(self):
        d = tempfile.mkdtemp()
        cjk = "中" * 1000
        paths = _write_items(d, [cjk])
        seen = []

        def budget_spy(args, profile, input_chars=None):
            seen.append(input_chars)
            args.max_tokens = 1000
            args.escalation_ceiling = 0

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            return "ok", {}, {}

        code, _o, _e = run_map(
            _map_args("p", paths), complete=fake_complete,
            gate_spy=lambda *a, **k: None,
            extra={"apply_output_budget": budget_spy})
        self.assertIsNone(code)
        expected = int((len("p") + len(cjk)) * amb.density_factor(cjk))
        self.assertEqual(seen, [expected])


class TestMapPerItemBudget(unittest.TestCase):
    """HIGH: each item is budgeted INDEPENDENTLY from its OWN size, so
    its cache key depends only on (model, prompt, item, its-own-budget, temp,
    response_format) — stable no matter what else is in the batch. A shared
    batch-max budget would re-key (and re-bill) every cached item the moment
    a larger item joins the batch."""

    def test_evolving_batch_serves_old_items_from_cache(self):
        d = tempfile.mkdtemp()
        small = _write_items(d, ["alpha item", "beta item"])
        cache = _FakeCache()
        calls = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            calls.append(messages[1]["content"])
            return "R:" + messages[1]["content"][:8], {}, {}

        code, _o, _e = run_map(_map_args("p", small, no_cache=False),
                               complete=fake_complete, cache=cache)
        self.assertIsNone(code)
        self.assertEqual(len(calls), 2)
        # Add one item LARGE enough to move a batch-max output budget, then
        # re-run: the small items MUST come from cache (only big is billed).
        big_text = "z" * 20000
        big = os.path.join(d, "big.txt")
        with open(big, "w", encoding="utf-8") as fh:
            fh.write(big_text)
        code, _o, err = run_map(_map_args("p", small + [big], no_cache=False),
                                complete=fake_complete, cache=cache)
        self.assertIsNone(code)
        self.assertEqual(calls[2:], [big_text],
                         "cached small items were re-billed — per-item cache "
                         "keys must not depend on the rest of the batch")
        self.assertIn("2 cached", err)

    def test_budget_sized_to_each_items_own_size_not_batch_max(self):
        d = tempfile.mkdtemp()
        small, large = "tiny", "y" * 5000
        paths = _write_items(d, [small, large])
        seen, got = [], {}

        def budget_spy(args, profile, input_chars=None):
            seen.append(input_chars)
            args.max_tokens = int(input_chars)  # distinct per item
            args.escalation_ceiling = 0

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            got[messages[1]["content"]] = args.max_tokens
            return "ok", {}, {}

        code, _o, _e = run_map(
            _map_args("p", paths), complete=fake_complete,
            gate_spy=lambda *a, **k: None,
            extra={"apply_output_budget": budget_spy})
        self.assertIsNone(code)
        eff_small = int((len("p") + len(small)) * amb.density_factor(small))
        eff_large = int((len("p") + len(large)) * amb.density_factor(large))
        self.assertEqual(len(seen), 2, "budget must be applied ONCE PER ITEM")
        self.assertEqual(sorted(seen), sorted([eff_small, eff_large]))
        # The SMALL item's completion runs with its OWN budget — never the
        # batch largest.
        self.assertEqual(got[small], eff_small)
        self.assertEqual(got[large], eff_large)

    def test_explicit_max_tokens_overrides_every_item_and_is_in_key(self):
        d = tempfile.mkdtemp()
        paths = _write_items(d, ["tiny", "y" * 5000])
        key_tokens, got = [], {}
        real_key = amb._cache_key

        def key_spy(model, system, chunk, max_tokens, temperature,
                    response_format=None):
            key_tokens.append(max_tokens)
            return real_key(model, system, chunk, max_tokens, temperature,
                            response_format)

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            got[messages[1]["content"]] = args.max_tokens
            return "ok", {}, {}

        code, _o, _e = run_map(
            _map_args("p", paths, max_tokens=5000, no_cache=False),
            complete=fake_complete, extra={"_cache_key": key_spy})
        self.assertIsNone(code)
        self.assertEqual(key_tokens, [5000, 5000],
                         "an explicit --max-tokens must key every item")
        self.assertEqual(set(got.values()), {5000},
                         "an explicit --max-tokens must apply to every item")


if __name__ == "__main__":
    unittest.main()
