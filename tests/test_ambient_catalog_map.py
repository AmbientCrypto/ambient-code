"""ambient_code.catalog_map: model resolution, profiles, local count_tokens."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from ambient_code import catalog_map as cm  # noqa: E402

KIMI = "moonshotai/kimi-k2.7-code"
GLM = "z-ai/glm-5.2"
CATALOG = [
    {"id": KIMI, "context_length": 128000, "max_output_length": 8192, "supported_features": ["reasoning"]},
    {"id": GLM, "context_length": 200000, "max_output_length": 4096, "supported_features": ["reasoning"]},
]


class TestResolve(unittest.TestCase):
    def test_exact_id(self):
        self.assertEqual(cm.resolve_model(GLM, CATALOG, KIMI), GLM)

    def test_claude_alias_to_default(self):
        self.assertEqual(cm.resolve_model("claude-sonnet-4-5-20250929", CATALOG, KIMI), KIMI)

    def test_unknown_to_default(self):
        self.assertEqual(cm.resolve_model("deepseek/nonexistent", CATALOG, KIMI), KIMI)

    def test_user_map(self):
        self.assertEqual(cm.resolve_model("claude-x", CATALOG, KIMI, {"claude-x": GLM}), GLM)

    def test_none_to_default(self):
        self.assertEqual(cm.resolve_model(None, CATALOG, KIMI), KIMI)


class TestProfile(unittest.TestCase):
    def test_kimi_measured_floor(self):
        p = cm.profile_for(KIMI, CATALOG)
        self.assertEqual(p.window, 128000)
        self.assertEqual(p.ceiling, 8192)
        self.assertEqual(p.floor, 256)   # Kimi measured override
        self.assertTrue(p.supports_images)  # moonshotai family

    def test_glm_reasoning_floor(self):
        p = cm.profile_for(GLM, CATALOG)
        self.assertEqual(p.floor, 2048)  # reasoning default
        self.assertFalse(p.supports_images)

    def test_unknown_model_conservative(self):
        p = cm.profile_for("brand/new-model-tomorrow", CATALOG)
        self.assertIsNone(p.window)
        self.assertEqual(p.floor, 2048)

    def test_floor_never_above_ceiling(self):
        p = cm.profile_from_entry({"id": "x", "context_length": 1000, "max_output_length": 100,
                                   "supported_features": ["reasoning"]})
        self.assertLessEqual(p.floor, p.ceiling)


class TestCountTokens(unittest.TestCase):
    def test_counts_system_messages_tools(self):
        body = {"system": "you are helpful",
                "messages": [{"role": "user", "content": "hello world this is a test"}],
                "tools": [{"name": "Read", "input_schema": {"type": "object"}}]}
        n = cm.count_input_tokens(body)
        self.assertGreater(n, 0)

    def test_image_flat_cost(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "d" * 100000}}]}]}
        self.assertLess(cm.count_input_tokens(body), 2000)  # flat ~1024, not 100k/3

    def test_ceil_bytes_over_3(self):
        body = {"messages": [{"role": "user", "content": "a" * 30}]}
        # 30 chars /3 = 10, + per-message overhead
        self.assertGreaterEqual(cm.count_input_tokens(body), 10)


class TestColdSubstitution(unittest.TestCase):
    CAT = [
        {"id": "moonshotai/kimi-k2.7-code", "is_ready": False, "context_length": 128000},
        {"id": "moonshotai/kimi-lite", "is_ready": True, "context_length": 64000},
        {"id": "z-ai/glm-5.2", "is_ready": True, "context_length": 200000},
    ]

    def test_cold_default_substitutes_same_vendor(self):
        # default Kimi is cold -> serve the warm same-vendor Kimi instead
        self.assertEqual(cm.resolve_model(None, self.CAT, "moonshotai/kimi-k2.7-code"),
                         "moonshotai/kimi-lite")

    def test_gone_model_substitutes_to_default_then_warm(self):
        self.assertEqual(cm.ready_substitute("deepseek/gone", self.CAT, "z-ai/glm-5.2"),
                         "z-ai/glm-5.2")

    def test_warm_model_served_as_is(self):
        self.assertEqual(cm.resolve_model("z-ai/glm-5.2", self.CAT, "moonshotai/kimi-lite"),
                         "z-ai/glm-5.2")

    def test_nothing_warm_serves_as_is(self):
        allcold = [{"id": "a/b", "is_ready": False}, {"id": "c/d", "is_ready": False}]
        self.assertIsNone(cm.ready_substitute("a/b", allcold, "a/b"))

    def test_no_readiness_field_no_substitution(self):
        cat = [{"id": "a/b", "context_length": 1000}, {"id": "c/d", "context_length": 1000}]
        self.assertEqual(cm.resolve_model("a/b", cat, "c/d"), "a/b")

    def test_string_false_readiness_is_cold(self):
        # A JSON string "false" is truthy to bool() — it must decode as COLD, so
        # a stringly-typed catalog can't serve a down model (or hide a warm one).
        cat = [{"id": "a/b", "is_ready": "false", "context_length": 1000},
               {"id": "c/d", "is_ready": "true", "context_length": 1000}]
        self.assertEqual(cm.resolve_model("a/b", cat, "c/d"), "c/d")  # a/b cold -> warm c/d
        self.assertEqual(cm.resolve_model("c/d", cat, "a/b"), "c/d")  # c/d warm -> served


class TestCatalogEdgeCases(unittest.TestCase):
    def test_explicit_text_only_modality_overrides_family(self):
        # A moonshotai model that EXPLICITLY declares text-only must not be forced to
        # images by the family heuristic.
        p = cm.profile_from_entry({"id": "moonshotai/text-only-model",
                                   "context_length": 1000, "max_output_length": 500,
                                   "input_modalities": ["text"]})
        self.assertFalse(p.supports_images)

    def test_cjk_counted_by_bytes_not_chars(self):
        # 4 CJK chars = 12 UTF-8 bytes -> ceil(12/3)=4, not char-based ceil(4/3)=2.
        body = {"messages": [{"role": "user", "content": "你好世界"}]}
        self.assertGreaterEqual(cm.count_input_tokens(body), 4 + cm._PER_MESSAGE)


if __name__ == "__main__":
    unittest.main()
