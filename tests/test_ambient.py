"""Hermetic tests for the ambient CLI's load-bearing pure functions.

No network, no live API. Run: python3 -m pytest tests/  (or python3 -m unittest).
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import math
import os
import random
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = load_module()
CPT = 3.2


def catalog():
    p = os.path.join(HERE, "catalog.json")
    if os.path.exists(p):
        return json.load(open(p))["data"]
    # minimal synthetic catalog if the snapshot isn't shipped
    return [
        {"id": "z-ai/glm-5.2", "context_length": 202752, "max_output_length": 202752,
         "supported_features": ["reasoning", "tools", "structured_outputs", "json_mode"],
         "pricing": {"input": 1.2, "output": 3.6}, "is_ready": True},
        {"id": "qwen/qwen3-coder", "context_length": 160000, "max_output_length": 32768,
         "supported_features": ["tools", "structured_outputs"],
         "pricing": {"input": 0.07, "output": 0.27}, "is_ready": False},
    ]


class TestProfileInvariants(unittest.TestCase):
    def _check(self, p):
        it = math.ceil(p.single_shot_chars / CPT)
        self.assertLessEqual(p.output_budget, p.max_output_length)
        self.assertLessEqual(it + p.output_budget, p.context_length)
        self.assertLessEqual(it + p.escalation_ceiling, p.context_length)
        self.assertLessEqual(p.chunk_chars, p.single_shot_chars)
        self.assertGreater(p.single_shot_chars, 0)
        self.assertGreater(p.output_budget, 0)

    def test_real_catalog(self):
        cat = catalog()
        for m in cat:
            self._check(amb.model_profile(cat, m["id"]))

    def test_fuzz(self):
        random.seed(1234)
        for _ in range(3000):
            ctx = random.randint(1000, 300000)
            mo = random.randint(200, 300000)
            feats = random.choice([["reasoning"], ["tools"], [], ["reasoning", "tools"]])
            meta = {"id": "x", "context_length": ctx, "max_output_length": mo,
                    "supported_features": feats}
            self._check(amb.model_profile([meta], "x"))

    def test_offline_fallback_is_reasoning(self):
        p = amb.model_profile([], "unknown/model")
        self.assertTrue(p.is_reasoning)
        self.assertGreater(p.single_shot_chars, 0)

    def test_single_shot_scales_up(self):
        cat = catalog()
        glm = amb.model_profile(cat, "z-ai/glm-5.2")
        self.assertGreater(glm.single_shot_chars, 32000)  # A1: not the old flat cap


class TestApplyBudget(unittest.TestCase):
    def _ns(self, **kw):
        import argparse
        return argparse.Namespace(**kw)

    def test_auto_rightsizes_small_input(self):
        p = amb.model_profile(catalog(), "z-ai/glm-5.2")
        a = self._ns(max_tokens=None)
        amb.apply_output_budget(a, p, 5)
        self.assertLessEqual(a.max_tokens, p.output_budget)
        self.assertLessEqual(math.ceil(p.single_shot_chars / CPT) + a.max_tokens,
                             p.context_length)

    def test_override_never_exceeds_cap_or_context(self):
        random.seed(7)
        for _ in range(500):
            ctx = random.randint(1000, 300000)
            mo = random.randint(200, 300000)
            p = amb.model_profile(
                [{"id": "x", "context_length": ctx, "max_output_length": mo,
                  "supported_features": random.choice([["reasoning"], []])}], "x")
            a = self._ns(max_tokens=random.choice([1, 50, 30000, 999999]))
            amb.apply_output_budget(a, p)
            self.assertLessEqual(a.max_tokens, p.max_output_length)
            self.assertLessEqual(math.ceil(p.single_shot_chars / CPT) + a.max_tokens,
                                 p.context_length + 1)


class TestPackChunks(unittest.TestCase):
    def test_every_chunk_within_budget(self):
        text = "\n".join(f"line {i}" for i in range(50000))
        for c in amb.pack_chunks([("f.py", text)], 30000):
            self.assertLessEqual(len(c), 30000)

    def test_minified_single_line_hard_split(self):
        for c in amb.pack_chunks([("min.js", "A" * 500000)], 100000):
            self.assertLessEqual(len(c), 100000)

    def test_long_label_never_overflows(self):
        # a long path + a minified line must not exceed the
        # effective budget (raised to fit the header), never silently overflow.
        label = "very/deep/" * 20 + "module.py"      # ~200-char label
        eff = max(27000, len(label) + 200)
        for c in amb.pack_chunks([(label, "B" * 400000)], 27000):
            self.assertLessEqual(len(c), eff)

    def test_ast_bias_keeps_functions_whole(self):
        src = ""
        for i in range(3):
            src += (f"def f_{i}(x):\n"
                    + "\n".join(f"    a_{j} = x" for j in range(60)) + "\n\n")
        chunks = amb.pack_chunks(amb.with_line_gutters([("m.py", src)]), 2000)
        # each chunk should begin at a def boundary
        for c in chunks:
            body = c.split("=====\n", 1)[-1]
            first = next((ln for ln in body.split("\n") if "f_" in ln), "")
            self.assertIn("def f_", first)


class TestJSONAndFindings(unittest.TestCase):
    def test_extract_json_variants(self):
        self.assertIsNotNone(amb.extract_json('{"a":1}'))
        self.assertIsNotNone(amb.extract_json('```json\n{"a":1}\n```'))
        self.assertEqual(amb.extract_json("pre {\"a\":1} post"), {"a": 1})
        self.assertIsNone(amb.extract_json("no json"))
        self.assertIsNone(amb.extract_json(""))

    def test_dedupe_and_verdict(self):
        c1 = {"findings": [{"severity": "HIGH", "file": "a.py", "line": 42,
                            "title": "SQL injection", "scenario": "s"}]}
        c2 = {"findings": [{"severity": "CRITICAL", "file": "a.py", "line": 43,
                            "title": "SQL injection here", "scenario": "longer"}]}
        merged = json.loads(amb.findings_reducer([json.dumps(c1), json.dumps(c2)]))
        self.assertEqual(len(merged["findings"]), 1)
        self.assertEqual(merged["findings"][0]["severity"], "CRITICAL")
        self.assertEqual(merged["verdict"], "FIX FIRST")

    def test_capability_gating(self):
        cat = catalog()
        glm = amb.model_profile(cat, "z-ai/glm-5.2")
        self.assertEqual(amb.response_format_for(glm, {})["type"], "json_schema")

    def test_unparseable_chunk_is_a_gap_not_a_ship(self):
        # a chunk whose output won't parse must force non-SHIP,
        # never silently reduce to {"findings":[], "SHIP"}.
        clean = json.dumps({"findings": [], "verdict": "SHIP"})
        prose = "The code looks fine to me, no issues."   # not JSON
        merged = json.loads(amb.findings_reducer([clean, prose]))
        self.assertNotEqual(merged["verdict"], "SHIP")
        self.assertEqual(merged["_unparsed_chunks"], 1)

    def test_extract_json_skips_invalid_brace_before_valid(self):
        self.assertEqual(
            amb.extract_json('note {not: json} then {"findings": []}'),
            {"findings": []})

    def test_extract_json_repairs_truncated_wrapper(self):
        # Stress-battery finding: strict json_schema isn't always honored — a
        # reply that closes the findings array but drops the verdict/brace must
        # be repaired into the real findings, not lost.
        trunc = ('{ "findings": [ {"severity":"CRITICAL","title":"div by zero",'
                 '"file":"a.py","line":2,"defect":"d","scenario":"s","fix":"f"} ]')
        obj = amb.extract_json(trunc)
        self.assertIsNotNone(obj)
        self.assertEqual(len(obj["findings"]), 1)

    def test_extract_json_does_not_repair_midstring_truncation(self):
        self.assertIsNone(amb.extract_json('{"findings":[{"title":"half a str'))

    def test_render_findings_json_always_valid(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            amb.render_findings("total garbage, not json at all", "json", "k")
        parsed = json.loads(buf.getvalue())  # must not raise
        self.assertEqual(parsed["verdict"], "NEEDS WORK")
        self.assertFalse(parsed["coverage_complete"])


class TestClassifyAndRedact(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(amb.classify_error(401, {"error": {"message": "x"}}, "k")[0], "key")
        self.assertEqual(amb.classify_error(402, {"error": {"message": "insufficient funds"}}, "k")[0], "funds")
        self.assertEqual(amb.classify_error(503, {"error": {"message": "down"}}, "k")[0], "service")
        self.assertEqual(amb.classify_error(429, {"error": {"message": "No workers available"}}, "k")[0], "model")

    def test_redact(self):
        key = "sk-abcdef-1234567890"
        self.assertNotIn(key, amb.redact(f"error with {key} in it", key))

    def test_short_key_not_redacted(self):
        self.assertEqual(amb.redact("workers", "k"), "workers")


class TestSecretsTripwire(unittest.TestCase):
    def test_catches_api_key(self):
        with self.assertRaises(SystemExit):
            amb.refuse_if_secrets([("s.py", 'api_key = "sk_live_abcdef1234567890"')], False)

    def test_dotenv_by_name(self):
        with self.assertRaises(SystemExit):
            amb.refuse_if_secrets([(".env", "X=1")], False)

    def test_allow_bypass(self):
        amb.refuse_if_secrets([("s.py", 'api_key = "sk_live_abcdef1234567890"')], True)


class TestConfigConcurrency(unittest.TestCase):
    def test_roundtrip_and_dedup(self):
        d = tempfile.mkdtemp()
        orig = amb.CONFIG_PATH
        amb.CONFIG_PATH = os.path.join(d, "env")
        try:
            amb.save_config_values({"AMBIENT_MODEL": "a", "AMBIENT_API_KEY": "k"})
            amb.save_config_values({"AMBIENT_MODEL": "b"})
            conf = amb.read_config_file()
            self.assertEqual(conf["AMBIENT_MODEL"], "b")
            self.assertEqual(conf["AMBIENT_API_KEY"], "k")
            # exactly one AMBIENT_MODEL line
            with open(amb.CONFIG_PATH, encoding="utf-8") as fh:
                self.assertEqual(fh.read().count("AMBIENT_MODEL="), 1)
        finally:
            amb.CONFIG_PATH = orig


class TestChunkCache(unittest.TestCase):
    def test_put_get_and_key_sensitivity(self):
        d = tempfile.mkdtemp()
        orig = amb.CACHE_DIR
        amb.CACHE_DIR = d
        try:
            k = amb._cache_key("glm", "sys", "body", 8192, 0.1)
            self.assertIsNone(amb._cache_get(k, 3600))
            amb._cache_put(k, "RESULT")
            self.assertEqual(amb._cache_get(k, 3600), "RESULT")
            # model / budget / chunk / response_format each change the address
            keys = {
                amb._cache_key("glm", "sys", "body", 8192, 0.1),
                amb._cache_key("kimi", "sys", "body", 8192, 0.1),
                amb._cache_key("glm", "sys", "body", 4096, 0.1),
                amb._cache_key("glm", "sys", "other", 8192, 0.1),
                # prose vs strict-schema share a prompt but
                # must NOT share a cache entry.
                amb._cache_key("glm", "sys", "body", 8192, 0.1,
                               {"type": "json_schema"}),
            }
            self.assertEqual(len(keys), 5)
        finally:
            amb.CACHE_DIR = orig


class TestWindowsPortability(unittest.TestCase):
    """Prove the code paths that only run where fcntl / darwin are absent."""

    def test_config_roundtrips_without_fcntl(self):
        # Simulate Windows: fcntl is None -> the portable O_EXCL lock path must
        # still round-trip config writes and clean up its lock file.
        d = tempfile.mkdtemp()
        orig_fcntl, orig_cfg = amb.fcntl, amb.CONFIG_PATH
        amb.fcntl = None
        amb.CONFIG_PATH = os.path.join(d, "env")
        try:
            amb.save_config_values({"AMBIENT_MODEL": "a", "AMBIENT_API_KEY": "k"})
            amb.save_config_values({"AMBIENT_MODEL": "b"})
            conf = amb.read_config_file()
            self.assertEqual(conf["AMBIENT_MODEL"], "b")
            self.assertEqual(conf["AMBIENT_API_KEY"], "k")
            self.assertFalse(os.path.exists(os.path.join(d, ".env.lock")))
        finally:
            amb.fcntl, amb.CONFIG_PATH = orig_fcntl, orig_cfg

    def test_secret_backend_off_platform_falls_back_to_file(self):
        import sys as _sys
        orig = _sys.platform
        _sys.platform = "win32"
        try:
            self.assertIsNone(amb.secret_backend())      # no OS secret store
            self.assertFalse(amb.keychain_available())    # -> 0600 file fallback
        finally:
            _sys.platform = orig


class TestConsensusFailure(unittest.TestCase):
    def test_all_models_fail_exits_2_not_clean(self):
        # Force every consensus model to fail (ok=False) with no findings; the
        # command must NOT print a clean result — it must exit 2
        # (consensus can't present total failure as "no defects").
        d = tempfile.mkdtemp()
        src = os.path.join(d, "x.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("def f(a, b):\n    return a / b\n")
        catalog = [{"id": "z-ai/glm-5.2", "context_length": 200000,
                    "max_output_length": 200000,
                    "supported_features": ["reasoning"], "is_ready": True},
                   {"id": "moonshotai/kimi-k2.7-code", "context_length": 262144,
                    "max_output_length": 262144,
                    "supported_features": ["reasoning"], "is_ready": True}]
        orig = (amb.safe_catalog, amb.cost_gate, amb.run_one_audit)
        amb.safe_catalog = lambda *a, **k: catalog
        amb.cost_gate = lambda *a, **k: None
        amb.run_one_audit = lambda *a, **k: ([], False)   # every model fails
        args = argparse.Namespace(
            paths=[src], staged=False, diff=None, focus=None, allow_secrets=False,
            format="prose", dry_run=False,
            consensus="z-ai/glm-5.2,moonshotai/kimi-k2.7-code", model=None,
            max_tokens=None, temperature=0.1, timeout=30, raw=False, fallback=False,
            allow_partial=False, allow_cost=True, yes=True, no_cache=True,
            cache_ttl=None, json=False)
        try:
            with self.assertRaises(SystemExit) as cm, \
                    contextlib.redirect_stdout(io.StringIO()):
                amb.cmd_audit(args, "key", "https://x", {})
            self.assertEqual(cm.exception.code, 2)
        finally:
            (amb.safe_catalog, amb.cost_gate, amb.run_one_audit) = orig


class TestRetryAfter(unittest.TestCase):
    def test_retry_delay_honors_header_and_clamps(self):
        # base 3, server asks 20 -> at least 20, at most 20 + 50% jitter
        d = amb._retry_delay(3, {"Retry-After": "20"})
        self.assertGreaterEqual(d, 20.0)
        self.assertLessEqual(d, 30.0)
        # absurd cooldown clamped to 60 (+ jitter)
        self.assertLessEqual(amb._retry_delay(3, {"Retry-After": "9999"}), 90.0)
        # no header -> jitter only, still >= base
        self.assertGreaterEqual(amb._retry_delay(5, None), 5.0)

    def test_retry_after_reaches_chaterror_for_fanout(self):
        # A 429 whose body carries a server Retry-After must surface on the
        # ChatError so the fan-out loop can honor it.
        orig = amb.stream_completion
        amb.stream_completion = lambda *a, **k: (
            429, {"error": {"message": "rate limited, slow down"},
                  "_retry_after": "7"})
        try:
            args = argparse.Namespace(max_tokens=8192, temperature=0.1, timeout=30,
                                      fallback=False)
            with self.assertRaises(amb.ChatError) as cm:
                amb.complete("k", "https://x", "z-ai/glm-5.2",
                             [{"role": "user", "content": "hi"}], args)
            self.assertEqual(cm.exception.category, "rate")
            self.assertEqual(getattr(cm.exception, "retry_after", None), "7")
        finally:
            amb.stream_completion = orig


class TestUsageMetering(unittest.TestCase):
    def test_estimates_usage_when_backend_omits_it(self):
        # Ambient's stream often omits a usage object, so
        # metering was blind. complete() must estimate + record instead.
        d = tempfile.mkdtemp()
        orig_u, orig_s = amb.USAGE_PATH, amb.stream_completion
        amb.USAGE_PATH = os.path.join(d, "usage.jsonl")
        amb.stream_completion = lambda *a, **k: (
            200, {"content": "hello world answer", "reasoning": "",
                  "usage": None, "finish_reason": "stop"})
        try:
            args = argparse.Namespace(max_tokens=256, temperature=0.1,
                                      timeout=30, fallback=False)
            _c, usage, _b = amb.complete(
                "k", "https://x", "z-ai/glm-5.2",
                [{"role": "user", "content": "hi there"}], args)
            self.assertTrue(usage.get("_estimated"))
            self.assertGreater(usage["completion_tokens"], 0)
            with open(amb.USAGE_PATH, encoding="utf-8") as fh:
                recs = [json.loads(x) for x in fh if x.strip()]
            self.assertEqual(len(recs), 1)
            self.assertGreater(recs[0]["out"], 0)
        finally:
            amb.USAGE_PATH, amb.stream_completion = orig_u, orig_s


class TestDefaults(unittest.TestCase):
    def test_default_model_is_kimi_both_lanes(self):
        # Kimi is the default auditor; every model stays fully selectable.
        self.assertEqual(amb.DEFAULT_MODEL, "moonshotai/kimi-k2.7-code")
        self.assertEqual(amb.DEFAULT_CODE_MODEL, "moonshotai/kimi-k2.7-code")


class TestVersionSync(unittest.TestCase):
    def test_version_matches_plugin_json(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            pj = json.load(fh)
        self.assertEqual(amb.__version__, pj["version"])


if __name__ == "__main__":
    unittest.main()
