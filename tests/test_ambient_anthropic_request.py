"""ambient_code.anthropic_request: Anthropic Messages -> OpenAI request.

Golden tests for the block-aware state machine (multi tool_result -> multiple tool
messages, results-first), assistant tool_use -> tool_calls, tools/tool_choice/system,
images, and the FULL tool-id round-trip proving uniform decode.
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from ambient_code import anthropic_request as ar  # noqa: E402
from ambient_code import tool_ids as ti  # noqa: E402
from ambient_code.errors import TranslationError, UnsupportedFeatureError  # noqa: E402

M = "moonshotai/kimi-k2.7-code"


class TestSystemAndSimple(unittest.TestCase):
    def test_system_string(self):
        out = ar.anthropic_to_openai({"system": "be brief", "messages": []}, M)
        self.assertEqual(out["messages"][0], {"role": "system", "content": "be brief"})
        self.assertEqual(out["model"], M)

    def test_system_blocks_joined(self):
        body = {"system": [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}},
                           {"type": "text", "text": "b"}], "messages": []}
        out = ar.anthropic_to_openai(body, M)
        self.assertEqual(out["messages"][0], {"role": "system", "content": "a\n\nb"})

    def test_simple_string_messages(self):
        body = {"messages": [{"role": "user", "content": "hi"},
                             {"role": "assistant", "content": "hello"}]}
        out = ar.anthropic_to_openai(body, M)
        self.assertEqual(out["messages"],
                         [{"role": "user", "content": "hi"},
                          {"role": "assistant", "content": "hello"}])


class TestAssistantToolUse(unittest.TestCase):
    def test_text_plus_tool_use(self):
        enc = ti.encode_tool_id("functions.Read:0")
        body = {"messages": [{"role": "assistant", "content": [
            {"type": "text", "text": "reading"},
            {"type": "tool_use", "id": enc, "name": "Read", "input": {"path": "a.py"}},
        ]}]}
        out = ar.anthropic_to_openai(body, M)
        msg = out["messages"][0]
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["content"], "reading")
        self.assertEqual(msg["tool_calls"][0]["id"], "functions.Read:0")  # DECODED
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "Read")
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"]), {"path": "a.py"})

    def test_thinking_block_dropped(self):
        body = {"messages": [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "secret", "signature": "sig"},
            {"type": "text", "text": "answer"},
        ]}]}
        out = ar.anthropic_to_openai(body, M)
        self.assertEqual(out["messages"][0]["content"], "answer")
        self.assertNotIn("tool_calls", out["messages"][0])


class TestUserToolResultStateMachine(unittest.TestCase):
    def test_multiple_tool_results_fan_out_results_first(self):
        id_a = ti.encode_tool_id("functions.Read:0")
        id_b = ti.encode_tool_id("functions.Read:1")
        body = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": id_a, "content": "AAA"},
            {"type": "tool_result", "tool_use_id": id_b, "content": [{"type": "text", "text": "BBB"}]},
            {"type": "text", "text": "now continue"},
        ]}]}
        out = ar.anthropic_to_openai(body, M)
        msgs = out["messages"]
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0], {"role": "tool", "tool_call_id": "functions.Read:0", "content": "AAA"})
        self.assertEqual(msgs[1], {"role": "tool", "tool_call_id": "functions.Read:1", "content": "BBB"})
        self.assertEqual(msgs[2], {"role": "user", "content": "now continue"})

    def test_is_error_marked(self):
        idc = ti.encode_tool_id("call_x")
        body = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": idc, "content": "boom", "is_error": True}]}]}
        out = ar.anthropic_to_openai(body, M)
        self.assertTrue(out["messages"][0]["content"].startswith("[tool error] "))


class TestFullIdRoundTrip(unittest.TestCase):
    def test_assistant_id_and_tool_result_id_decode_identically(self):
        # The correctness property: Ambient emitted `functions.Read:0`; we encoded it so
        # Claude Code saw `amb1_...`; Claude Code replays it in BOTH the assistant tool_use
        # AND the tool_result. Both must decode to the SAME OpenAI id so OpenAI links them.
        original = "functions.Read:0"
        enc = ti.encode_tool_id(original)
        body = {"messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": enc, "name": "Read", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": enc, "content": "ok"}]},
        ]}
        out = ar.anthropic_to_openai(body, M)
        assistant_id = out["messages"][0]["tool_calls"][0]["id"]
        tool_msg_id = out["messages"][1]["tool_call_id"]
        self.assertEqual(assistant_id, original)
        self.assertEqual(tool_msg_id, original)
        self.assertEqual(assistant_id, tool_msg_id)


class TestToolsAndChoice(unittest.TestCase):
    def test_custom_tool_translation(self):
        body = {"messages": [], "tools": [
            {"name": "Read", "description": "read a file",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}]}
        out = ar.anthropic_to_openai(body, M)
        self.assertEqual(out["tools"][0]["type"], "function")
        self.assertEqual(out["tools"][0]["function"]["name"], "Read")
        self.assertIn("parameters", out["tools"][0]["function"])

    def test_server_tool_rejected(self):
        body = {"messages": [], "tools": [{"type": "web_search_20250305", "name": "web_search"}]}
        with self.assertRaises(UnsupportedFeatureError):
            ar.anthropic_to_openai(body, M)

    def test_tool_choice_variants(self):
        base = {"messages": [], "tools": [
            {"name": "Read", "input_schema": {"type": "object"}}]}
        self.assertEqual(ar.anthropic_to_openai(dict(base, tool_choice={"type": "auto"}), M)["tool_choice"], "auto")
        self.assertEqual(ar.anthropic_to_openai(dict(base, tool_choice={"type": "any"}), M)["tool_choice"], "required")
        self.assertEqual(ar.anthropic_to_openai(dict(base, tool_choice={"type": "none"}), M)["tool_choice"], "none")
        out = ar.anthropic_to_openai(dict(base, tool_choice={"type": "tool", "name": "Read"}), M)
        self.assertEqual(out["tool_choice"], {"type": "function", "function": {"name": "Read"}})

    def test_disable_parallel(self):
        body = {"messages": [], "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "auto", "disable_parallel_tool_use": True}}
        out = ar.anthropic_to_openai(body, M)
        self.assertFalse(out["parallel_tool_calls"])


class TestSamplingAndImages(unittest.TestCase):
    def test_sampling_passthrough_and_drops(self):
        body = {"messages": [], "max_tokens": 512, "stop_sequences": ["X"],
                "temperature": 0.4, "top_p": 0.9, "metadata": {"user_id": "u"},
                "thinking": {"type": "enabled", "budget_tokens": 1000}}
        out = ar.anthropic_to_openai(body, M)
        self.assertEqual(out["max_tokens"], 512)
        self.assertEqual(out["stop"], ["X"])
        self.assertEqual(out["temperature"], 0.4)
        self.assertEqual(out["top_p"], 0.9)
        self.assertNotIn("metadata", out)
        self.assertNotIn("thinking", out)

    def test_image_block(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]}]}
        out = ar.anthropic_to_openai(body, M)
        content = out["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "what is this"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,AAAA"))


class TestBadShapes(unittest.TestCase):
    def test_non_object_body(self):
        with self.assertRaises(TranslationError):
            ar.anthropic_to_openai([], M)

    def test_bad_role(self):
        with self.assertRaises(TranslationError):
            ar.anthropic_to_openai({"messages": [{"role": "system", "content": "x"}]}, M)


class TestRequestTranslationFixes(unittest.TestCase):
    def test_tool_result_image_forwarded_not_dropped(self):
        idc = ti.encode_tool_id("call_x")
        body = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": idc, "content": [
                {"type": "text", "text": "screenshot:"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QQ"}}]}]}]}
        out = ar.anthropic_to_openai(body, M)
        # tool message carries the text; a trailing user message carries the image.
        self.assertEqual(out["messages"][0]["role"], "tool")
        self.assertEqual(out["messages"][0]["content"], "screenshot:")
        self.assertEqual(out["messages"][1]["role"], "user")
        img = out["messages"][1]["content"][0]
        self.assertEqual(img["type"], "image_url")
        self.assertTrue(img["image_url"]["url"].startswith("data:image/png;base64,QQ"))

    def test_empty_assistant_content_is_empty_string_not_null(self):
        body = {"messages": [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "x", "signature": "s"}]}]}
        out = ar.anthropic_to_openai(body, M)
        self.assertEqual(out["messages"][0]["content"], "")
        self.assertNotIn("tool_calls", out["messages"][0])

    def test_malformed_tool_id_raises_translation_error(self):
        # ToolIdError is now a TranslationError -> the server renders a clean 400.
        body = {"messages": [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "amb1_Og", "name": "R", "input": {}}]}]}
        with self.assertRaises(TranslationError):
            ar.anthropic_to_openai(body, M)

    def test_non_list_tools_raises(self):
        with self.assertRaises(TranslationError):
            ar.anthropic_to_openai({"messages": [], "tools": 0}, M)

    def test_empty_tools_omitted(self):
        out = ar.anthropic_to_openai({"messages": [], "tools": []}, M)
        self.assertNotIn("tools", out)


if __name__ == "__main__":
    unittest.main()
