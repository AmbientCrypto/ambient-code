"""ambient_code.anthropic_response: OpenAI chat.completion -> Anthropic Message."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from ambient_code import anthropic_response as arsp  # noqa: E402
from ambient_code import tool_ids as ti  # noqa: E402
from ambient_code.errors import MalformedToolArgumentsError  # noqa: E402

REQ_MODEL = "claude-sonnet-4-5-20250929"


def _resp(message, finish_reason="stop", usage=None):
    return {"id": "chatcmpl-abc", "choices": [{"index": 0, "message": message,
            "finish_reason": finish_reason}], "usage": usage or {}}


class TestText(unittest.TestCase):
    def test_text_content(self):
        out = arsp.openai_to_anthropic(_resp({"role": "assistant", "content": "hello"}), REQ_MODEL)
        self.assertEqual(out["type"], "message")
        self.assertEqual(out["role"], "assistant")
        self.assertEqual(out["model"], REQ_MODEL)  # requested model, not served
        self.assertEqual(out["content"], [{"type": "text", "text": "hello"}])
        self.assertEqual(out["stop_reason"], "end_turn")
        self.assertTrue(out["id"].startswith("msg_"))

    def test_usage_mapping(self):
        out = arsp.openai_to_anthropic(
            _resp({"content": "x"}, usage={"prompt_tokens": 12, "completion_tokens": 7}), REQ_MODEL)
        self.assertEqual(out["usage"]["input_tokens"], 12)
        self.assertEqual(out["usage"]["output_tokens"], 7)


class TestToolUse(unittest.TestCase):
    def test_tool_call_becomes_tool_use_with_encoded_id(self):
        msg = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "functions.Read:0", "type": "function",
             "function": {"name": "Read", "arguments": '{"path": "a.py"}'}}]}
        out = arsp.openai_to_anthropic(_resp(msg, finish_reason="tool_calls"), REQ_MODEL)
        block = out["content"][0]
        self.assertEqual(block["type"], "tool_use")
        self.assertEqual(block["name"], "Read")
        self.assertEqual(block["input"], {"path": "a.py"})
        # id is Anthropic-legal AND decodes back to the original OpenAI id (compose w/ P3)
        self.assertTrue(ti.is_anthropic_valid(block["id"]))
        self.assertEqual(ti.decode_tool_id(block["id"]), "functions.Read:0")
        self.assertEqual(out["stop_reason"], "tool_use")

    def test_stop_reason_forced_tool_use_even_if_finish_stop(self):
        msg = {"content": "let me read", "tool_calls": [
            {"id": "call_1", "function": {"name": "Read", "arguments": "{}"}}]}
        out = arsp.openai_to_anthropic(_resp(msg, finish_reason="stop"), REQ_MODEL)
        self.assertEqual(out["stop_reason"], "tool_use")  # forced, not end_turn
        # text block first, then tool_use
        self.assertEqual(out["content"][0]["type"], "text")
        self.assertEqual(out["content"][1]["type"], "tool_use")

    def test_missing_upstream_id_synthesized_and_roundtrips(self):
        msg = {"tool_calls": [{"function": {"name": "Read", "arguments": "{}"}, "index": 3}]}
        out = arsp.openai_to_anthropic(_resp(msg, finish_reason="tool_calls"), REQ_MODEL)
        block = out["content"][0]
        self.assertTrue(ti.is_anthropic_valid(block["id"]))
        self.assertIn("bridge:", ti.decode_tool_id(block["id"]))

    def test_empty_arguments_become_empty_object(self):
        msg = {"tool_calls": [{"id": "call_1", "function": {"name": "Now", "arguments": ""}}]}
        out = arsp.openai_to_anthropic(_resp(msg, finish_reason="tool_calls"), REQ_MODEL)
        self.assertEqual(out["content"][0]["input"], {})


class TestFinishReasons(unittest.TestCase):
    def test_mapping(self):
        for fr, expected in (("stop", "end_turn"), ("length", "max_tokens"),
                             ("content_filter", "end_turn"), (None, "end_turn"),
                             ("weird", "end_turn")):
            out = arsp.openai_to_anthropic(_resp({"content": "x"}, finish_reason=fr), REQ_MODEL)
            self.assertEqual(out["stop_reason"], expected, "finish_reason=%r" % fr)


class TestReasoningDropped(unittest.TestCase):
    def test_reasoning_not_emitted_as_thinking(self):
        msg = {"content": "answer", "reasoning": "chain of thought"}
        out = arsp.openai_to_anthropic(_resp(msg), REQ_MODEL)
        types = [b["type"] for b in out["content"]]
        self.assertNotIn("thinking", types)
        self.assertEqual(out["content"], [{"type": "text", "text": "answer"}])


class TestMalformedArguments(unittest.TestCase):
    def test_invalid_json_raises(self):
        msg = {"tool_calls": [{"id": "call_1", "function": {"name": "R", "arguments": "{not json"}}]}
        with self.assertRaises(MalformedToolArgumentsError):
            arsp.openai_to_anthropic(_resp(msg, finish_reason="tool_calls"), REQ_MODEL)

    def test_non_object_json_raises(self):
        msg = {"tool_calls": [{"id": "c", "function": {"name": "R", "arguments": "[1,2]"}}]}
        with self.assertRaises(MalformedToolArgumentsError):
            arsp.openai_to_anthropic(_resp(msg, finish_reason="tool_calls"), REQ_MODEL)


class TestResponseTranslationFixes(unittest.TestCase):
    def test_parallel_tool_calls_without_ids_get_distinct_ids(self):
        # Both lack id AND index -> must NOT collide on bridge:<resp>:0.
        msg = {"tool_calls": [
            {"function": {"name": "A", "arguments": "{}"}},
            {"function": {"name": "B", "arguments": "{}"}}]}
        out = arsp.openai_to_anthropic(_resp(msg, finish_reason="tool_calls"), REQ_MODEL)
        ids = [b["id"] for b in out["content"]]
        self.assertEqual(len(set(ids)), 2, "parallel synth ids collided: %r" % ids)
        self.assertNotEqual(ti.decode_tool_id(ids[0]), ti.decode_tool_id(ids[1]))

    def test_content_filter_with_tools_does_not_execute(self):
        msg = {"content": None, "tool_calls": [
            {"id": "call_1", "function": {"name": "rm", "arguments": "{}"}}]}
        out = arsp.openai_to_anthropic(_resp(msg, finish_reason="content_filter"), REQ_MODEL)
        self.assertEqual(out["stop_reason"], "end_turn")  # NOT tool_use

    def test_malformed_entry_does_not_force_tool_use(self):
        msg = {"content": "hi", "tool_calls": ["not-a-dict"]}
        out = arsp.openai_to_anthropic(_resp(msg, finish_reason="stop"), REQ_MODEL)
        self.assertEqual(out["stop_reason"], "end_turn")  # no emitted tool block

    def test_nan_infinity_rejected(self):
        for bad in ('{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}'):
            msg = {"tool_calls": [{"id": "c", "function": {"name": "R", "arguments": bad}}]}
            with self.assertRaises(MalformedToolArgumentsError):
                arsp.openai_to_anthropic(_resp(msg, finish_reason="tool_calls"), REQ_MODEL)


if __name__ == "__main__":
    unittest.main()
