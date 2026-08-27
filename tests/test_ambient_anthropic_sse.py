"""ambient_code.anthropic_sse: synthesized Anthropic SSE stream.

Parses the byte stream back and asserts exact event order, text/tool reassembly, the
tool_use id round-trip, stop_reason/usage, keepalive, and the absence of `[DONE]`.
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from ambient_code import anthropic_sse as sse  # noqa: E402
from ambient_code import anthropic_response as arsp  # noqa: E402
from ambient_code import tool_ids as ti  # noqa: E402


def parse_sse(blob):
    """-> list of (event_name, data_dict)."""
    out = []
    for frame in blob.decode("utf-8").split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        name = None
        data = None
        for line in frame.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        out.append((name, data))
    return out


class TestSettledStream(unittest.TestCase):
    def _message(self):
        # A settled Anthropic Message with text + one tool_use (id encoded from OpenAI).
        enc = ti.encode_tool_id("functions.Read:0")
        return {
            "id": "msg_bridge_x", "type": "message", "role": "assistant",
            "model": "claude-sonnet-4-5", "stop_reason": "tool_use", "stop_sequence": None,
            "content": [
                {"type": "text", "text": "I'll read it."},
                {"type": "tool_use", "id": enc, "name": "Read", "input": {"path": "a.py"}},
            ],
            "usage": {"input_tokens": 30, "output_tokens": 12},
        }

    def test_event_order(self):
        events = parse_sse(sse.settled_message_to_sse(self._message()))
        names = [n for n, _ in events]
        self.assertEqual(names, [
            "message_start", "ping",
            "content_block_start", "content_block_delta", "content_block_stop",   # text
            "content_block_start", "content_block_delta", "content_block_stop",   # tool_use
            "message_delta", "message_stop"])

    def test_no_openai_done_frame(self):
        blob = sse.settled_message_to_sse(self._message())
        self.assertNotIn(b"[DONE]", blob)

    def test_message_start_shape(self):
        events = dict(parse_sse(sse.settled_message_to_sse(self._message())))
        ms = events["message_start"]["message"]
        self.assertEqual(ms["content"], [])
        self.assertIsNone(ms["stop_reason"])
        self.assertEqual(ms["usage"]["output_tokens"], 0)
        self.assertEqual(ms["usage"]["input_tokens"], 30)

    def test_text_reassembles(self):
        events = parse_sse(sse.settled_message_to_sse(self._message()))
        text = "".join(d["delta"]["text"] for n, d in events
                        if n == "content_block_delta" and d["delta"]["type"] == "text_delta")
        self.assertEqual(text, "I'll read it.")

    def test_tool_use_id_and_input_roundtrip(self):
        events = parse_sse(sse.settled_message_to_sse(self._message()))
        start = [d for n, d in events if n == "content_block_start"
                 and d["content_block"]["type"] == "tool_use"][0]
        self.assertTrue(ti.is_anthropic_valid(start["content_block"]["id"]))
        self.assertEqual(ti.decode_tool_id(start["content_block"]["id"]), "functions.Read:0")
        partial = [d["delta"]["partial_json"] for n, d in events
                   if n == "content_block_delta" and d["delta"]["type"] == "input_json_delta"][0]
        self.assertEqual(json.loads(partial), {"path": "a.py"})

    def test_message_delta_stop_and_usage(self):
        events = dict(parse_sse(sse.settled_message_to_sse(self._message())))
        self.assertEqual(events["message_delta"]["delta"]["stop_reason"], "tool_use")
        self.assertEqual(events["message_delta"]["usage"]["output_tokens"], 12)


class TestComposeFromOpenAI(unittest.TestCase):
    def test_openai_response_to_sse_end_to_end(self):
        # Full compose: OpenAI settled body -> Anthropic message -> SSE -> parse back.
        openai_resp = {"id": "chatcmpl-1", "choices": [{"index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": "ok", "tool_calls": [
                {"id": "functions.save_note:0", "type": "function",
                 "function": {"name": "save_note", "arguments": '{"text":"hi"}'}}]}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 9}}
        msg = arsp.openai_to_anthropic(openai_resp, "claude-3-5-sonnet")
        events = parse_sse(sse.settled_message_to_sse(msg))
        names = [n for n, _ in events]
        self.assertEqual(names[0], "message_start")
        self.assertEqual(names[-1], "message_stop")
        tool_start = [d for n, d in events if n == "content_block_start"
                      and d["content_block"]["type"] == "tool_use"][0]
        self.assertEqual(ti.decode_tool_id(tool_start["content_block"]["id"]), "functions.save_note:0")


class TestKeepaliveAndError(unittest.TestCase):
    def test_message_start_and_ping_available_early(self):
        ms = sse.message_start_event("msg_1", "claude-x", input_tokens=99)
        self.assertEqual(ms[0], "message_start")
        self.assertEqual(ms[1]["message"]["usage"]["input_tokens"], 99)
        self.assertEqual(sse.ping_event()[0], "ping")

    def test_error_event_shape(self):
        name, data = sse.error_event("overloaded_error", "no workers")
        self.assertEqual(name, "error")
        self.assertEqual(data["error"]["type"], "overloaded_error")


if __name__ == "__main__":
    unittest.main()
