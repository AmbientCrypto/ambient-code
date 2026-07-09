"""Hermetic engine-level tests: SSE parsing, the complete() degradation ladder,
the SACRED-model invariant, hostile catalogs, exit codes, config corruption,
cache concurrency, and output sanitization. No network, no live API."""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v2", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v2", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = load_module()


def rich_catalog():
    return [
        {"id": "z-ai/glm-5.2", "context_length": 202752,
         "max_output_length": 202752, "is_ready": True,
         "supported_features": ["reasoning", "structured_outputs"],
         "output_modalities": ["text"],
         "pricing": {"input": 0.9, "output": 3.6}},
        {"id": "moonshotai/kimi-k2.7-code", "context_length": 262144,
         "max_output_length": 262144, "is_ready": True,
         "supported_features": ["reasoning", "structured_outputs"],
         "output_modalities": ["text"],
         "pricing": {"input": 1.0, "output": 3.83}},
        {"id": "alt/json-only", "context_length": 300000,
         "max_output_length": 60000, "is_ready": True,
         "supported_features": ["reasoning", "json_mode"],
         "output_modalities": ["text"],
         "pricing": {"input": 0.1, "output": 0.4}},
    ]


def ns(**kw):
    base = dict(max_tokens=8000, temperature=0.1, timeout=30, raw=False,
                fallback=False, allow_partial=False, allow_cost=True, yes=True,
                no_cache=True, cache_ttl=None, model=None,
                escalation_ceiling=30000, _auto_budget=True)
    base.update(kw)
    return argparse.Namespace(**base)


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


class FakeSSE:
    """Stand-in for the urlopen response: SSE lines then EOF."""

    def __init__(self, lines, ctype="text/event-stream", status=200):
        self._lines = [ln if isinstance(ln, bytes) else ln.encode()
                       for ln in lines]
        self.status = status
        self.headers = {"Content-Type": ctype}

    def readline(self, _size=-1):   # real http.client readline takes an optional size cap
        return self._lines.pop(0) if self._lines else b""

    def read(self):
        return b"".join(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def sse_call(lines, **kw):
    """Run stream_completion against a canned SSE response."""
    resp = FakeSSE(lines, **kw)
    with patched(amb.urllib.request, urlopen=lambda req, timeout=None: resp):
        return amb.stream_completion("https://x", "k", {"model": "m"}, 30)


def delta(content=None, reasoning=None, reasoning_content=None, finish=None,
          usage=None):
    ch = {"delta": {}}
    if content is not None:
        ch["delta"]["content"] = content
    if reasoning is not None:
        ch["delta"]["reasoning"] = reasoning
    if reasoning_content is not None:
        ch["delta"]["reasoning_content"] = reasoning_content
    if finish is not None:
        ch["finish_reason"] = finish
    obj = {"choices": [ch]}
    if usage is not None:
        obj["usage"] = usage
    return "data: " + json.dumps(obj) + "\n"


class TestFakeSSE(unittest.TestCase):
    def test_assembles_deltas_and_done(self):
        status, body = sse_call([delta("Hel"), "\n", delta("lo"), "\n",
                                 "data: [DONE]\n", "\n"])
        self.assertEqual(status, 200)
        self.assertEqual(body["content"], "Hello")

    def test_multiline_data_joined_with_newline(self):
        # Two data: lines in ONE event join with \n per the SSE spec.
        payload = json.dumps({"choices": [{"delta": {"content": "X"}}]})
        status, body = sse_call([f"data: {payload[:10]}\n",
                                 f"data:{payload[10:]}\n", "\n",
                                 "data: [DONE]\n", "\n"])
        self.assertEqual(body["content"], "X")

    def test_keepalive_comments_ignored(self):
        status, body = sse_call([": ping\n", delta("ok", finish="stop"), "\n"])
        self.assertEqual(body["content"], "ok")
        self.assertEqual(body["finish_reason"], "stop")

    def test_reasoning_captured_under_both_keys(self):
        status, body = sse_call([delta(reasoning="think1 "), "\n",
                                 delta(reasoning_content="think2"), "\n",
                                 delta("ans", finish="stop"), "\n"])
        self.assertEqual(body["reasoning"], "think1 think2")

    def test_usage_from_final_chunk(self):
        status, body = sse_call([delta("a", usage=None), "\n",
                                 delta("", finish="stop",
                                       usage={"prompt_tokens": 5,
                                              "completion_tokens": 7}), "\n"])
        self.assertEqual(body["usage"]["completion_tokens"], 7)

    def test_eof_without_terminator_raises_stall_with_partial(self):
        with self.assertRaises(amb.StallError) as cm:
            sse_call([delta("half"), "\n"])  # no [DONE], no finish_reason
        self.assertEqual(cm.exception.partial, "half")

    def test_non_sse_content_type_parses_whole_body(self):
        body_json = json.dumps({"choices": [{"message": {"content": "hi"},
                                             "finish_reason": "stop"}]})
        status, body = sse_call([body_json], ctype="application/json")
        self.assertEqual(status, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], "hi")


class TestTransportClassification(unittest.TestCase):
    """re-verify coverage: the stream_completion error-classification coverage."""

    def test_connect_oserror_becomes_networkerror(self):
        # #3: a bare OSError from urlopen (RemoteDisconnected/ConnectionReset
        # out of getresponse(), before headers) must map to NetworkError, not
        # surface raw as an [internal] error.
        def boom(req, timeout=None):
            raise ConnectionResetError("peer reset before headers")
        with patched(amb.urllib.request, urlopen=boom):
            with self.assertRaises(amb.NetworkError):
                amb.stream_completion("https://x", "k", {"model": "m"}, 30)

    def test_non_sse_body_read_reset_becomes_networkerror(self):
        # #4: a reset mid-body on the non-SSE fallback must map to NetworkError.
        class ResetOnRead:
            status = 200
            headers = {"Content-Type": "application/json"}
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                raise ConnectionResetError("reset mid-body")
        with patched(amb.urllib.request,
                     urlopen=lambda req, timeout=None: ResetOnRead()):
            with self.assertRaises(amb.NetworkError):
                amb.stream_completion("https://x", "k", {"model": "m"}, 30)

    def test_non_sse_invalid_utf8_decodes_lossily(self):
        # #4: invalid UTF-8 in the non-SSE body must decode with errors=replace
        # (→ clean non-JSON report), never a raw UnicodeDecodeError.
        class BadUtf8:
            status = 200
            headers = {"Content-Type": "application/json"}
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return b"\xff\xfe not valid json"
        with patched(amb.urllib.request,
                     urlopen=lambda req, timeout=None: BadUtf8()):
            status, body = amb.stream_completion("https://x", "k",
                                                 {"model": "m"}, 30)
        self.assertEqual(status, 200)
        self.assertIn("error", body)  # reported cleanly, no crash


class TestMapReduceHonesty(unittest.TestCase):
    """arch-audit: the SYNTHESIS/reduce tier must not silently truncate."""

    def setUp(self):
        self._logu = amb.log_usage
        amb.log_usage = lambda *a, **k: None

    def tearDown(self):
        amb.log_usage = self._logu

    def test_truncated_synthesis_marks_partial(self):
        # A length-cut merge (finish_reason=length) must set PARTIAL — the map
        # phase already guards this; the reduce phase must too.
        def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
            if "SYNTH-MARK" in messages[0]["content"]:
                return ("truncated merge", None, {"finish_reason": "length"})
            return ("chunk report", None, {"finish_reason": "stop"})
        with patched(amb, complete=fake):
            final, partial, reason = amb.run_map_reduce(
                "k", "u", "m", "map instructions",
                ["chunk one", "chunk two"], ns(),
                "SYNTH-MARK synthesize", 8000)
        self.assertTrue(partial)
        self.assertIn("synthesis", reason)

    def test_clean_synthesis_is_not_partial(self):
        # Converse: a clean synthesis over clean chunks is NOT partial.
        def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
            return ("ok text", None, {"finish_reason": "stop"})
        with patched(amb, complete=fake):
            final, partial, reason = amb.run_map_reduce(
                "k", "u", "m", "map", ["chunk one", "chunk two"], ns(),
                "SYNTH-MARK synth", 8000)
        self.assertFalse(partial)


def stream_seq(*results):
    """Fake stream_completion returning canned results per call; StallError
    instances raise. Returns (fake, payload-log)."""
    calls = []

    def fake(api_url, api_key, payload, timeout, on_delta=None):
        calls.append(payload)
        r = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    return fake, calls


def ok_body(content="ok", finish="stop", reasoning=""):
    return (200, {"content": content, "reasoning": reasoning, "usage": None,
                  "finish_reason": finish})


class TestCompleteLadder(unittest.TestCase):
    def setUp(self):
        self._logu = amb.log_usage
        amb.log_usage = lambda *a, **k: None

    def tearDown(self):
        amb.log_usage = self._logu

    def test_stall_retries_once_then_salvages_partial(self):
        fake, calls = stream_seq(amb.StallError("s", partial="A" * 500),
                                 amb.StallError("s", partial="B" * 500))
        with patched(amb, stream_completion=fake):
            content, usage, body = amb.complete("k", "u", "m", [], ns())
        self.assertEqual(len(calls), 2)  # one fresh retry
        self.assertTrue(content.startswith("[AMBIENT NOTE"))
        self.assertTrue(body.get("salvaged_partial"))

    def test_hard_wall_never_restarts(self):
        fake, calls = stream_seq(
            amb.StallError("wall", partial="C" * 500, hard_wall=True))
        with patched(amb, stream_completion=fake):
            content, _u, body = amb.complete("k", "u", "m", [], ns())
        self.assertEqual(len(calls), 1)  # salvaged immediately, no re-bill
        self.assertTrue(body.get("salvaged_partial"))

    def test_empty_content_escalates_once(self):
        fake, calls = stream_seq(ok_body(content=""), ok_body("done"))
        with patched(amb, stream_completion=fake):
            content, _u, _b = amb.complete(
                "k", "u", "m", [{"role": "user", "content": "x"}], ns())
        self.assertEqual(content, "done")
        self.assertEqual(len(calls), 2)
        # new_budget = min(max(2x, x+16384), ceiling) = min(24384, 30000)
        self.assertEqual(calls[1]["max_tokens"], 24384)

    def test_budget_400_halves_once(self):
        fake, calls = stream_seq(
            (400, {"error": {"message": "max_tokens exceeds model limit"}}),
            ok_body("fine"))
        with patched(amb, stream_completion=fake):
            content, _u, _b = amb.complete("k", "u", "m", [], ns())
        self.assertEqual(content, "fine")
        self.assertEqual(calls[1]["max_tokens"], 4000)

    def test_reasoning_draft_salvage_small_input_only(self):
        fake, _ = stream_seq(ok_body(content="", reasoning="R" * 300),
                             ok_body(content="", reasoning="R" * 300))
        msgs = [{"role": "user", "content": "small"}]
        with patched(amb, stream_completion=fake):
            content, _u, body = amb.complete("k", "u", "m", msgs, ns())
        self.assertIn("best-effort draft", content)
        self.assertTrue(body.get("salvaged_partial"))

    def test_classic_body_carries_finish_reason_to_top_level(self):
        classic = (200, {"choices": [{"message": {"content": "t"},
                                      "finish_reason": "length"}]})
        fake, _ = stream_seq(classic)
        with patched(amb, stream_completion=fake):
            _c, _u, body = amb.complete("k", "u", "m", [], ns())
        self.assertEqual(body["finish_reason"], "length")


class TestSacredModel(unittest.TestCase):
    """The product's #1 promise: the chosen model is NEVER silently swapped."""

    def setUp(self):
        self._logu = amb.log_usage
        amb.log_usage = lambda *a, **k: None

    def tearDown(self):
        amb.log_usage = self._logu

    def _no_workers(self):
        return (429, {"error": {"message": "No workers available"}})

    def test_no_fallback_when_flag_off(self):
        fake, calls = stream_seq(self._no_workers())
        with patched(amb, stream_completion=fake,
                     fetch_models=lambda *a: rich_catalog(),
                     read_config_file=lambda: {}):
            with self.assertRaises(amb.ChatError) as cm:
                amb.complete("k", "u", "z-ai/glm-5.2",
                             [{"role": "user", "content": "x"}],
                             ns(fallback=False))
        self.assertEqual(cm.exception.category, "model")
        self.assertTrue(all(p["model"] == "z-ai/glm-5.2" for p in calls))

    def test_fallback_regates_response_format(self):
        # Original model wants strict json_schema; the only viable alt has
        # json_mode only → the resent payload must downgrade, not 400.
        fake, calls = stream_seq(self._no_workers(), ok_body("j"))
        cat = [m for m in rich_catalog() if m["id"] != "moonshotai/kimi-k2.7-code"]
        args = ns(fallback=True)
        args.response_format = {"type": "json_schema",
                                "json_schema": {"name": "x", "strict": True,
                                                "schema": {}}}
        with patched(amb, stream_completion=fake,
                     fetch_models=lambda *a: cat,
                     read_config_file=lambda: {}):
            content, _u, _b = amb.complete(
                "k", "u", "z-ai/glm-5.2",
                [{"role": "user", "content": "x"}], args)
        self.assertEqual(content, "j")
        self.assertEqual(calls[1]["model"], "alt/json-only")
        self.assertEqual(calls[1]["response_format"], {"type": "json_object"})

    def test_fallback_rejects_alt_with_too_small_context(self):
        fake, calls = stream_seq(self._no_workers())
        cat = [dict(rich_catalog()[0]),
               {"id": "tiny/model", "context_length": 4000,
                "max_output_length": 2000, "is_ready": True,
                "supported_features": [], "output_modalities": ["text"],
                "pricing": {"input": 0.1, "output": 0.1}}]
        big_input = [{"role": "user", "content": "x" * 400_000}]
        with patched(amb, stream_completion=fake,
                     fetch_models=lambda *a: cat,
                     read_config_file=lambda: {}):
            with self.assertRaises(amb.ChatError):
                amb.complete("k", "u", "z-ai/glm-5.2", big_input,
                             ns(fallback=True))
        self.assertTrue(all(p["model"] == "z-ai/glm-5.2" for p in calls))

    def test_pick_fallback_skips_hidden_and_prefers_defaults(self):
        cat = rich_catalog()
        conf = {"AMBIENT_MODELS_HIDE": "alt/*",
                "AMBIENT_MODEL": "moonshotai/kimi-k2.7-code"}
        pick = amb.pick_fallback_model(cat, "z-ai/glm-5.2", conf=conf)
        self.assertEqual(pick, "moonshotai/kimi-k2.7-code")

    def test_pick_fallback_all_hidden_returns_none(self):
        cat = rich_catalog()
        conf = {"AMBIENT_MODELS_HIDE": "*"}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            pick = amb.pick_fallback_model(cat, "z-ai/glm-5.2", conf=conf)
        self.assertIsNone(pick)
        self.assertIn("curation hides them", err.getvalue())


class TestHostileCatalog(unittest.TestCase):
    def test_profile_survives_junk_field_types(self):
        for junk in (None, "x", "202752", {}, [], -5, True, 1e30, float("nan")):
            cat = [{"id": "m", "context_length": junk,
                    "max_output_length": junk, "supported_features": junk}]
            p = amb.model_profile(cat, "m")
            self.assertGreater(p.single_shot_chars, 0)
            self.assertGreater(p.output_budget, 0)
            self.assertGreaterEqual(p.context_length, 4000)

    def test_string_numeric_context_is_coerced(self):
        cat = [{"id": "m", "context_length": "202752",
                "max_output_length": "100000",
                "supported_features": ["reasoning"]}]
        p = amb.model_profile(cat, "m")
        self.assertEqual(p.context_length, 202752)

    def test_ready_model_ids_skips_junk_entries(self):
        self.assertEqual(
            amb.ready_model_ids([{"is_ready": True}, "junk", None,
                                 {"id": "ok", "is_ready": True}]),
            ["ok"])

    def test_fetch_models_normalizes(self):
        with patched(amb, api_request=lambda *a, **k: (
                200, {"data": [{"id": "good", "is_ready": True}, "bad",
                               {"is_ready": True}, {"id": ""}, 42]})):
            out = amb.fetch_models("https://x", "k")
        self.assertEqual([m["id"] for m in out], ["good"])

    def test_model_pricing_zero_or_missing_is_unpriced(self):
        self.assertIsNone(amb.model_pricing([{"id": "m", "pricing": {}}], "m"))
        self.assertIsNone(amb.model_pricing([{"id": "m"}], "m"))
        self.assertIsNone(amb.model_pricing(
            [{"id": "m", "pricing": {"input": 0, "output": 0}}], "m"))
        self.assertIsNone(amb.model_pricing(
            [{"id": "m", "pricing": {"input": float("nan"),
                                     "output": 1}}], "m"))
        self.assertEqual(amb.model_pricing(
            [{"id": "m", "pricing": {"input": 0.5, "output": 2}}], "m"),
            (0.5, 2.0))


class TestSpendGate(unittest.TestCase):
    def test_unpriced_engages_assumed_pricing(self):
        e, b, assumed = amb.estimate_cost([], "m", 100_000, 3, 30_000)
        self.assertTrue(assumed)
        self.assertGreater(e, 0)
        self.assertGreater(b, e)

    def test_ceiling_blocks_expected(self):
        with self.assertRaises(SystemExit) as cm:
            amb._gate_amount(9.0, ns(allow_cost=False), {})
        self.assertIn("ceiling", str(cm.exception))

    def test_worst_case_guard_blocks_3x(self):
        with self.assertRaises(SystemExit) as cm:
            amb._gate_amount(0.4, ns(allow_cost=False), {}, bound=16.0)
        self.assertIn("worst-case", str(cm.exception).lower())

    def test_bad_ceiling_string_defaults_to_5(self):
        with self.assertRaises(SystemExit):
            amb._gate_amount(6.0, ns(allow_cost=False),
                             {"AMBIENT_MAX_SPEND": "lots"})

    def test_allow_cost_overrides(self):
        amb._gate_amount(9.0, ns(allow_cost=True), {}, bound=99.0)  # no exit


class TestExitCodes(unittest.TestCase):
    def test_render_result_partial_exits_2(self):
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stdout(io.StringIO()):
            amb.render_result("t", True, "why", ns(allow_partial=False), "")
        self.assertEqual(cm.exception.code, 2)

    def test_render_result_allow_partial_returns(self):
        with contextlib.redirect_stdout(io.StringIO()):
            amb.render_result("t", True, "why", ns(allow_partial=True), "")

    def test_emit_json_partial_exit2_and_valid(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stdout(buf):
            amb.emit_json("ask", model="m", content="x", partial=True)
        self.assertEqual(cm.exception.code, 2)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["schema_version"], 1)
        self.assertEqual(env["exit_code"], 2)

    def test_emit_json_truncation_forces_partial(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(buf):
            amb.emit_json("code", model="m", content="x",
                          finish_reason="length")
        env = json.loads(buf.getvalue())
        self.assertEqual(env["status"], "partial")
        self.assertIn("token cap", env["coverage_gap"])


class TestSanitization(unittest.TestCase):
    def test_redact_strips_osc52_and_csi(self):
        evil = "SHIP\x1b]52;c;bWFsaWNpb3Vz\x07 and \x1b[2Aup\x1b[31mred"
        clean = amb.redact(evil, "")
        self.assertNotIn("\x1b", clean)
        self.assertIn("SHIP", clean)

    def test_redact_keeps_newlines_tabs_unicode(self):
        s = "line1\n\tline2 — ✓ 中文"
        self.assertEqual(amb.redact(s, ""), s)

    def test_redact_still_redacts_key(self):
        self.assertNotIn("sk-verysecret-key",
                         amb.redact("x sk-verysecret-key y",
                                    "sk-verysecret-key"))

    def test_render_findings_normalizes_rogue_verdict(self):
        raw = json.dumps({"findings": [], "verdict": "SHIP\x1b]0;evil\x07!!"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            amb.render_findings(raw, "report", "")
        out = buf.getvalue()
        self.assertNotIn("\x1b", out)
        self.assertIn("Verdict: SHIP", out)  # recomputed from the closed set

    def test_repaired_json_never_ships(self):
        trunc = ('{"findings": [{"severity":"LOW","confidence":"LOW",'
                 '"file":"a.py","line":1,"title":"t","defect":"d",'
                 '"scenario":"s","fix":"f"}]')
        obj = amb.extract_json(trunc)
        self.assertTrue(obj.get("_repaired"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            partial = amb.render_findings(json.dumps(obj), "report", "")
        self.assertTrue(partial)
        self.assertNotIn("Verdict: SHIP", buf.getvalue())


class TestSecretScan(unittest.TestCase):
    def test_linear_on_megabyte_line(self):
        t0 = time.time()
        amb._line_has_secret("a" * 1_000_000)
        self.assertLess(time.time() - t0, 1.0)

    def test_cred_after_many_benign_urls_is_caught(self):
        line = ("http://benign.example/p " + "a" * 3000) * 25 \
            + " https://user:supersecret1@evil.example/x"
        self.assertTrue(amb._line_has_secret(line))

    def test_credential_named_files_refused(self):
        for name in (".envrc", ".netrc", "id_rsa", "server.pem",
                     "credentials.json", ".env.production"):
            with self.assertRaises(SystemExit):
                amb.refuse_if_secrets([(name, "plain text")], allow=False)

    def test_env_lookalikes_allowed(self):
        amb.refuse_if_secrets([("environment.py", "x = 1"),
                               ("envelope.md", "hello")], allow=False)


class TestConfigCorruption(unittest.TestCase):
    def test_non_utf8_config_degrades(self):
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, "env")
        with open(cfg, "wb") as fh:
            fh.write(b"\xff\xfe\x00garbage")
        err = io.StringIO()
        with patched(amb, CONFIG_PATH=cfg), contextlib.redirect_stderr(err):
            out = amb.read_config_file()
        self.assertEqual(out, {})
        self.assertIn("corrupt", err.getvalue())

    def test_save_config_callable_merges_fresh_state(self):
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, "env")
        with patched(amb, CONFIG_PATH=cfg):
            amb.save_config_values({"A": "1"})
            amb.save_config_values(
                lambda fresh: {"A": fresh.get("A", "") + "2"})
            self.assertEqual(amb.read_config_file()["A"], "12")

    def test_save_config_none_deletes_key(self):
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, "env")
        with patched(amb, CONFIG_PATH=cfg):
            amb.save_config_values({"K": "v", "L": "w"})
            amb.save_config_values({"K": None})
            conf = amb.read_config_file()
        self.assertNotIn("K", conf)
        self.assertEqual(conf["L"], "w")

    def test_usage_log_trims_and_is_private(self):
        d = tempfile.mkdtemp()
        up = os.path.join(d, "usage.jsonl")
        with open(up, "w", encoding="utf-8") as fh:
            for i in range(25_000):  # trim keeps the newest 20,000 lines
                fh.write(json.dumps({"ts": i}) + "\n")
        os.chmod(up, 0o644)
        with patched(amb, USAGE_PATH=up, USAGE_MAX_BYTES=1000):
            amb.log_usage("m", {"prompt_tokens": 1, "completion_tokens": 2})
        with open(up, encoding="utf-8") as fh:
            n_lines = sum(1 for _ in fh)
        self.assertLessEqual(n_lines, 20_001)
        if os.name != "nt":  # Windows has no POSIX owner-only mode bits
            self.assertEqual(stat.S_IMODE(os.stat(up).st_mode), 0o600)

    def test_cache_concurrent_same_key_never_torn(self):
        d = tempfile.mkdtemp()
        val = "X" * 10_000
        results = []
        with patched(amb, CACHE_DIR=d):
            def put():
                for _ in range(30):
                    amb._cache_put("kk", val)

            def get():
                for _ in range(60):
                    results.append(amb._cache_get("kk", 3600))

            ts = [threading.Thread(target=put) for _ in range(4)] \
                + [threading.Thread(target=get)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertTrue(all(r is None or r == val for r in results))


class TestStdin(unittest.TestCase):
    def test_binary_stdin_decodes_lossily(self):
        class B:
            def read(self, n):
                return b"caf\xe9\x00diff"

        class S:
            buffer = B()

            def isatty(self):
                return False

            def fileno(self):
                raise ValueError  # select falls through

        err = io.StringIO()
        with patched(amb.sys, stdin=S()), contextlib.redirect_stderr(err):
            with patched(amb.select, select=lambda *a: ([1], [], [])):
                out = amb.read_stdin_if_piped()
        self.assertIn("caf", out)
        self.assertNotIn("\x00", out)


class TestPipedAskPrintsOnce(unittest.TestCase):
    def test_single_print_no_stream_when_piped(self):
        seen = {}

        def fake_complete(api_key, api_url, model, messages, args,
                          on_delta=None, **kw):
            seen["on_delta"] = on_delta
            return "ANSWER", {}, {"finish_reason": "stop"}

        buf = io.StringIO()  # not a tty
        with patched(amb, complete=fake_complete,
                     safe_catalog=lambda *a: rich_catalog(),
                     read_stdin_if_piped=lambda: "",
                     warn_if_stdin_ignored=lambda *a: None):
            with contextlib.redirect_stdout(buf):
                amb.cmd_ask(ns(prompt=["hi"], system=None, json=False,
                               allow_secrets=False),
                            "k", "https://x", {})
        self.assertIsNone(seen["on_delta"])  # no live stream off-TTY
        self.assertEqual(buf.getvalue().count("ANSWER"), 1)


if __name__ == "__main__":
    unittest.main()
