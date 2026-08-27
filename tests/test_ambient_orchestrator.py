"""ambient_code.orchestrator + upstream accumulator (no network)."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from ambient_code import orchestrator as orch  # noqa: E402
from ambient_code import upstream as up  # noqa: E402
from ambient_code.orchestrator import ModelProfile, UpstreamResult, ContextOverflowError, UpstreamError  # noqa: E402


def prof(window=100000, floor=2048, ceiling=8192, images=True):
    return ModelProfile(window=window, floor=floor, ceiling=ceiling, supports_images=images)


def resolve(_):
    return "moonshotai/kimi-k2.7-code"


class Seq:
    """A FakeUpstream returning canned results in order, recording sent payloads."""
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        return self.results.pop(0)


def ok(content="hi", finish="stop", usage=None):
    return UpstreamResult(200, {"choices": [{"message": {"role": "assistant", "content": content},
                          "finish_reason": finish}], "usage": usage or {"prompt_tokens": 3, "completion_tokens": 2}})


class TestPrepare(unittest.TestCase):
    def test_translates_and_sets_model(self):
        p = orch.prepare({"model": "claude-x", "messages": [{"role": "user", "content": "hi"}]},
                         resolve, lambda m: prof())
        self.assertEqual(p.served_model, "moonshotai/kimi-k2.7-code")
        self.assertEqual(p.requested_model, "claude-x")
        self.assertEqual(p.openai_payload["messages"][-1], {"role": "user", "content": "hi"})

    def test_overflow_precheck_raises(self):
        # window=100 with floor 2048 -> any request overflows.
        with self.assertRaises(ContextOverflowError) as cm:
            orch.prepare({"messages": [{"role": "user", "content": "hi"}]},
                         resolve, lambda m: prof(window=100))
        self.assertIn("prompt is too long", str(cm.exception))

    def test_compaction_headroom_fires_before_hard_window(self):
        # A prompt that FITS the real window but exceeds the soft (headroom) window must
        # still overflow, so Claude Code compacts with room to spare (no thrashing).
        big = "x " * 180000  # ~90k est tokens -> fits 100k window but not the soft window
        with self.assertRaises(ContextOverflowError) as cm:
            orch.prepare({"messages": [{"role": "user", "content": big}]},
                         resolve, lambda m: prof(window=100000, floor=2048))
        import re as _re
        mm = _re.search(r"(\d+) tokens > (\d+) maximum", str(cm.exception))
        n, m = int(mm.group(1)), int(mm.group(2))
        self.assertGreater(n, m)
        self.assertLess(m, 100000, "M must be the SOFT window (below the real one)")
        self.assertGreaterEqual(m, 100000 - 16384, "headroom is capped at 16k")

    def test_soft_window_reserves_headroom(self):
        self.assertEqual(orch._soft_window(100000, 2048), 100000 - 12500)   # window//8
        self.assertEqual(orch._soft_window(1000000, 2048), 1000000 - 16384)  # capped at 16k
        self.assertEqual(orch._soft_window(1000, 2048), 2048 + orch._MARGIN)  # never below a floored turn

    def test_image_skips_precheck(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}}]}]}
        # tiny window but image -> pre-check skipped, no raise
        p = orch.prepare(body, resolve, lambda m: prof(window=100, images=True))
        self.assertTrue(p.has_image)


class TestRunUpstream(unittest.TestCase):
    def _prepared(self, **profkw):
        return orch.prepare({"messages": [{"role": "user", "content": "hi"}]},
                            resolve, lambda m: prof(**profkw))

    def test_clean_response(self):
        seq = Seq(ok("hello"))
        msg = orch.run_upstream(self._prepared(), seq)
        self.assertEqual(msg["content"], [{"type": "text", "text": "hello"}])
        self.assertEqual(msg["stop_reason"], "end_turn")

    def test_floor_applied_to_first_call(self):
        seq = Seq(ok())
        orch.run_upstream(self._prepared(floor=2048), seq)
        self.assertEqual(seq.calls[0]["max_tokens"], 2048)

    def test_escalate_on_empty_then_recover(self):
        empty = UpstreamResult(200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                                     "usage": {"completion_tokens": 2048}})
        seq = Seq(empty, ok("recovered"))
        msg = orch.run_upstream(self._prepared(), seq)
        self.assertEqual(msg["content"], [{"type": "text", "text": "recovered"}])
        self.assertEqual(len(seq.calls), 2)
        self.assertGreater(seq.calls[1]["max_tokens"], seq.calls[0]["max_tokens"])

    def test_400_with_overflow_marker_classified(self):
        seq = Seq(UpstreamResult(400, None, "context length exceeded"))
        with self.assertRaises(ContextOverflowError):
            orch.run_upstream(self._prepared(), seq)

    def test_400_generic_is_upstream_error_not_overflow(self):
        seq = Seq(UpstreamResult(400, None, "Upstream request failed"))
        with self.assertRaises(UpstreamError) as cm:
            orch.run_upstream(self._prepared(window=100000), seq)
        self.assertNotIsInstance(cm.exception, ContextOverflowError)

    def test_400_modality_not_overflow(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}}]}]}
        p = orch.prepare(body, resolve, lambda m: prof(window=100000, images=False))
        seq = Seq(UpstreamResult(400, None, "Upstream request failed"))
        with self.assertRaises(UpstreamError) as cm:
            orch.run_upstream(p, seq)
        self.assertEqual(cm.exception.http_status, 400)
        self.assertNotIsInstance(cm.exception, ContextOverflowError)

    def test_non_200_raises_upstream_error(self):
        seq = Seq(UpstreamResult(529, None, "No workers are currently available."))
        with self.assertRaises(UpstreamError):
            orch.run_upstream(self._prepared(), seq)


class TestAccumulateStream(unittest.TestCase):
    def test_content_concat(self):
        chunks = [{"choices": [{"delta": {"role": "assistant"}}]},
                  {"choices": [{"delta": {"content": "Hel"}}]},
                  {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
                  {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}]
        body = up.accumulate_stream(chunks)
        self.assertEqual(body["choices"][0]["message"]["content"], "Hello")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(body["usage"]["completion_tokens"], 2)

    def test_tool_call_arguments_accumulate(self):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "functions.Read:0", "type": "function",
                 "function": {"name": "Read", "arguments": ""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]},
                         "finish_reason": "tool_calls"}]},
        ]
        body = up.accumulate_stream(chunks)
        tc = body["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tc["id"], "functions.Read:0")
        self.assertEqual(tc["function"]["name"], "Read")
        self.assertEqual(tc["function"]["arguments"], '{"path":"a.py"}')
        self.assertIsNone(body["choices"][0]["message"]["content"])

    def test_iter_sse_data_stops_at_done(self):
        lines = [b'data: {"a":1}', b'', b'data: [DONE]', b'data: {"b":2}']
        got = list(up.iter_sse_data(lines))
        self.assertEqual(got, [{"a": 1}])


def malformed():
    return UpstreamResult(200, {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "function": {"name": "R", "arguments": "{bad json"}}]}}],
        "usage": {"completion_tokens": 5}})


class TestOrchestratorEdgeCases(unittest.TestCase):
    def _prepared(self, **pk):
        return orch.prepare({"messages": [{"role": "user", "content": "hi"}]},
                            resolve, lambda m: prof(**pk))

    def test_overflow_reports_n_greater_than_m(self):
        import re
        with self.assertRaises(ContextOverflowError) as cm:
            orch.prepare({"messages": [{"role": "user", "content": "hi"}]},
                         resolve, lambda m: prof(window=100))
        mm = re.search(r"(\d+) tokens > (\d+) maximum", str(cm.exception))
        self.assertIsNotNone(mm)
        self.assertGreater(int(mm.group(1)), int(mm.group(2)), "N must be > M for compaction")

    def test_malformed_args_escalates_then_clean_error(self):
        seq = Seq(malformed(), malformed())
        with self.assertRaises(UpstreamError):
            orch.run_upstream(self._prepared(), seq)
        self.assertEqual(len(seq.calls), 2)  # escalated once

    def test_malformed_args_retry_400_is_reclassified(self):
        seq = Seq(malformed(), UpstreamResult(400, None, "context length exceeded"))
        with self.assertRaises(ContextOverflowError):
            orch.run_upstream(self._prepared(), seq)

    def test_fraction_400_treated_as_overflow_shrunk_window(self):
        # An opaque 400 where est >= 75% of the window (but est+sent still < window) means
        # the real window shrank below the catalog's claim -> overflow (self-heal).
        big = "x " * 156000  # ~78k est tokens; window 100k -> est >= 75k
        p = orch.prepare({"messages": [{"role": "user", "content": big}]},
                         resolve, lambda m: prof(window=100000, floor=2048))
        seq = Seq(UpstreamResult(400, None, "Upstream request failed"))  # opaque, no markers
        with self.assertRaises(ContextOverflowError):
            orch.run_upstream(p, seq)

    def test_fraction_400_specific_cause_is_not_overflow(self):
        # A big prompt whose 400 NAMES a specific non-overflow cause (param/schema/
        # modality) must surface honestly, NOT fire a compaction + poison the learned window.
        big = "x " * 156000  # ~78k est tokens; window 100k -> past the 75% fraction
        p = orch.prepare({"messages": [{"role": "user", "content": big}]},
                         resolve, lambda m: prof(window=100000, floor=2048))
        seq = Seq(UpstreamResult(400, None, "invalid tool schema: parameter 'x' must be a string"))
        with self.assertRaises(UpstreamError) as cm:
            orch.run_upstream(p, seq)
        self.assertEqual(cm.exception.anthropic_type, "invalid_request_error")

    def test_weak_marker_with_specific_cause_is_not_overflow(self):
        # "exceeded" sits near "prompt", but the body ALSO names a schema/tool cause —
        # the weak overflow hint must be suppressed, not compacted.
        big = "x " * 156000
        p = orch.prepare({"messages": [{"role": "user", "content": big}]},
                         resolve, lambda m: prof(window=100000, floor=2048))
        seq = Seq(UpstreamResult(400, None, "tool calls exceeded; prompt schema is valid"))
        with self.assertRaises(UpstreamError):
            orch.run_upstream(p, seq)

    def test_strong_token_bound_marker_is_overflow_even_below_fraction(self):
        # "total tokens must be <= N" is an UNAMBIGUOUS overflow, so it self-heals even
        # when the estimate is below the 75% fraction and the body says "invalid".
        small = "x " * 2000  # ~1k est, far below 75% of a 100k window
        p = orch.prepare({"messages": [{"role": "user", "content": small}]},
                         resolve, lambda m: prof(window=100000, floor=2048))
        seq = Seq(UpstreamResult(400, None, "invalid request: total tokens must be <= 80000"))
        with self.assertRaises(ContextOverflowError):
            orch.run_upstream(p, seq)

    def test_max_tokens_param_400_does_not_compact(self):
        # A small-prompt max_tokens param error must stay an honest 400.
        small = "x " * 2000
        p = orch.prepare({"messages": [{"role": "user", "content": small}]},
                         resolve, lambda m: prof(window=100000, floor=2048))
        seq = Seq(UpstreamResult(400, None, "max_tokens must be less than or equal to 8192"))
        with self.assertRaises(UpstreamError) as cm:
            orch.run_upstream(p, seq)
        self.assertEqual(cm.exception.anthropic_type, "invalid_request_error")

    def test_max_tokens_exceeds_room_400_self_heals(self):
        # A body with a weak overflow signal that also mentions max_tokens
        # must still self-heal (max_tokens is no longer a suppression marker).
        small = "x " * 2000
        p = orch.prepare({"messages": [{"role": "user", "content": small}]},
                         resolve, lambda m: prof(window=100000, floor=2048))
        seq = Seq(UpstreamResult(
            400, None, "max_tokens requested exceeds the tokens available after the prompt"))
        with self.assertRaises(ContextOverflowError):
            orch.run_upstream(p, seq)

    def test_required_field_400_at_high_fill_does_not_compact(self):
        # A missing-required-field 400 at 76% of the window must NOT fire the
        # fraction heuristic — "required" is a specific cause.
        big = "x " * 152000  # ~76k est, past 75% of a 100k window
        p = orch.prepare({"messages": [{"role": "user", "content": big}]},
                         resolve, lambda m: prof(window=100000, floor=2048))
        seq = Seq(UpstreamResult(400, None, "messages.1.content is required"))
        with self.assertRaises(UpstreamError):
            orch.run_upstream(p, seq)

    def test_overflow_error_carries_observed_ceiling(self):
        # The learned ceiling must cover input+output (est+sent_max), not input alone.
        big = "x " * 156000
        p = orch.prepare({"messages": [{"role": "user", "content": big}]},
                         resolve, lambda m: prof(window=100000, floor=2048))
        seq = Seq(UpstreamResult(400, None, "Upstream request failed"))
        with self.assertRaises(ContextOverflowError) as cm:
            orch.run_upstream(p, seq)
        self.assertGreater(cm.exception.observed_ceiling, p.est_prompt_tokens)


class TestEnvRobustness(unittest.TestCase):
    def test_bad_env_does_not_crash_import(self):
        import importlib
        os.environ["AMBIENT_BRIDGE_UPSTREAM_TIMEOUT_S"] = "not-a-number"
        try:
            importlib.reload(up)
            self.assertEqual(up._UPSTREAM_NOPROGRESS_S, 300)  # fell back to default
        finally:
            del os.environ["AMBIENT_BRIDGE_UPSTREAM_TIMEOUT_S"]
            importlib.reload(up)


if __name__ == "__main__":
    unittest.main()
