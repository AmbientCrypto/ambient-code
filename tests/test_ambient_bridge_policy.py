"""ambient_code.bridge_policy: ported pure reliability decisions.

Hermetic, no network. Also proves the import SEAM that bin/ambient will use to load
the package (realpath from the script dir -> repo/plugin root -> import ambient_code).
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from ambient_code import bridge_policy as bp  # noqa: E402


class TestImportSeam(unittest.TestCase):
    def test_bin_ambient_imports_bridge_package_as_subprocess(self):
        # GENUINE wire-in: run bin/ambient in a FRESH interpreter from a NEUTRAL cwd,
        # so nothing but its own realpath shim can locate the package. This is the
        # exact path `ambient serve`/`ambient claude` take, incl. the installed symlink.
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)  # don't let the harness path mask the shim
        proc = subprocess.run([sys.executable, BIN, "__bridge_import_check"],
                              capture_output=True, text=True,
                              cwd=tempfile.gettempdir(), env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("bridge-import-ok", proc.stdout)


class TestDefaults(unittest.TestCase):
    def test_fallback_constants_match_source(self):
        self.assertEqual(bp.DEFAULT_MIN_OUTPUT_TOKENS, 2048)   # assume-reasoning
        self.assertEqual(bp.SAFE_MAX_OUTPUT_TOKENS, 65536)
        self.assertEqual(bp.floor_max_tokens(None), 2048)      # unknown model -> reasoning floor

    def test_next_max_tokens_uses_safe_default_hard_cap(self):
        self.assertEqual(bp.next_max_tokens(40000, 0, 100000), 65536)


class TestEmptiness(unittest.TestCase):
    def test_empty_when_no_content_no_tools(self):
        self.assertTrue(bp.is_empty_completion({"choices": [{"message": {"content": ""}}]}))
        self.assertTrue(bp.is_empty_completion({"choices": [{"message": {"content": "  \n"}}]}))
        self.assertTrue(bp.is_empty_completion({"choices": [{"message": {}}]}))

    def test_not_empty_with_text_or_tool(self):
        self.assertFalse(bp.is_empty_completion({"choices": [{"message": {"content": "hi"}}]}))
        self.assertFalse(bp.is_empty_completion(
            {"choices": [{"message": {"content": None, "tool_calls": [{"id": "x"}]}}]}))


class TestTruncationEscalation(unittest.TestCase):
    def test_truncated_by_finish_reason(self):
        self.assertTrue(bp.is_truncated({"choices": [{"finish_reason": "length"}]}, 100))

    def test_truncated_by_token_count(self):
        self.assertTrue(bp.is_truncated(
            {"choices": [{"finish_reason": "stop"}], "usage": {"completion_tokens": 100}}, 100))
        self.assertFalse(bp.is_truncated(
            {"choices": [{"finish_reason": "stop"}], "usage": {"completion_tokens": 10}}, 100))

    def test_should_escalate_only_when_empty_and_truncated(self):
        empty_trunc = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                       "usage": {"completion_tokens": 256}}
        self.assertTrue(bp.should_escalate(empty_trunc, 256))
        empty_notrunc = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                         "usage": {"completion_tokens": 3}}
        self.assertFalse(bp.should_escalate(empty_notrunc, 256))

    def test_next_max_tokens_doubles_and_caps(self):
        # room = 100000 - 1000 - 512 huge; ceiling = hard_cap; doubles to floor 2048.
        self.assertEqual(bp.next_max_tokens(256, 1000, 100000, hard_cap=8192), 2048)
        # already big -> doubles to 4096.
        self.assertEqual(bp.next_max_tokens(2048, 1000, 100000, hard_cap=8192), 4096)
        # no room -> None.
        self.assertIsNone(bp.next_max_tokens(2048, 99900, 100000, hard_cap=8192))


class TestFinishReasonRewrite(unittest.TestCase):
    def test_rewrite_stop_to_length_only_on_truncation_evidence(self):
        resp = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 1024}}
        fixed = bp.rewrite_finish_reason(resp, 1024)
        self.assertEqual(fixed["choices"][0]["finish_reason"], "length")
        # original not mutated
        self.assertEqual(resp["choices"][0]["finish_reason"], "stop")

    def test_valid_short_stop_unchanged(self):
        resp = {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 32}}
        self.assertIs(bp.rewrite_finish_reason(resp, 1024), resp)


class TestOverflow(unittest.TestCase):
    def test_is_context_overflow(self):
        self.assertTrue(bp.is_context_overflow(90000, 20000, 100000))
        self.assertFalse(bp.is_context_overflow(50000, 20000, 100000))

    def test_synthesize_overflow_body_exact_string(self):
        self.assertEqual(bp.synthesize_overflow_body(101888, 101376),
                         "prompt is too long: 101888 tokens > 101376 maximum")

    def test_error_looks_like_overflow(self):
        self.assertTrue(bp.error_looks_like_overflow("context length exceeded"))
        self.assertFalse(bp.error_looks_like_overflow("Upstream request failed"))
        self.assertFalse(bp.error_looks_like_overflow(None))


class TestImagesAndEstimate(unittest.TestCase):
    def test_messages_have_image(self):
        self.assertTrue(bp.messages_have_image(
            [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]))
        self.assertFalse(bp.messages_have_image([{"role": "user", "content": "hi"}]))

    def test_estimate_prompt_tokens_counts_and_flat_image(self):
        payload = {"messages": [{"role": "user", "content": "a" * 40}], "tools": []}
        self.assertGreaterEqual(bp.estimate_prompt_tokens(payload), 10)
        img = {"messages": [{"role": "user",
               "content": [{"type": "image_url", "image_url": {"url": "d" * 100000}}]}]}
        # image counted as flat ~1024, not its 100k length / 4
        self.assertLess(bp.estimate_prompt_tokens(img), 2000)


class Test429AndFloor(unittest.TestCase):
    def test_classify_429(self):
        self.assertEqual(bp.classify_429("No workers are currently available."), "cold")
        self.assertEqual(bp.classify_429("Too Many Requests"), "rate_limit")

    def test_backoff_bounded_and_jittered(self):
        for attempt in range(0, 8):
            b = bp.backoff_seconds(attempt)
            self.assertGreater(b, 0)
            self.assertLessEqual(b, bp.MAX_BACKOFF_S)

    def test_floor_max_tokens(self):
        self.assertEqual(bp.floor_max_tokens(None, 256), 256)
        self.assertEqual(bp.floor_max_tokens(10, 256), 256)
        self.assertEqual(bp.floor_max_tokens("junk", 256), 256)  # type: ignore[arg-type]
        self.assertEqual(bp.floor_max_tokens(4096, 256), 4096)


class TestOverflowMarkers(unittest.TestCase):
    def test_strong_markers_match(self):
        for t in ["prompt is too long", "maximum context length", "context window exceeded",
                  "too many tokens", "request_too_large"]:
            self.assertTrue(bp.error_looks_like_overflow(t), t)

    def test_bare_exceeds_without_size_word_is_not_overflow(self):
        # "exceeds" alone (tool-call cap, quota) must NOT read as a context overflow.
        self.assertFalse(bp.error_looks_like_overflow("exceeds maximum number of tool calls"))
        self.assertFalse(bp.error_looks_like_overflow("rate limit exceeded"))

    def test_exceeds_with_size_word_is_overflow(self):
        self.assertTrue(bp.error_looks_like_overflow("input exceeds 8192 tokens"))
        self.assertTrue(bp.error_looks_like_overflow("prompt length exceeded"))

    def test_opaque_body_names_no_specific_cause(self):
        self.assertFalse(bp.error_names_specific_cause("Upstream request failed"))
        self.assertFalse(bp.error_names_specific_cause(None))
        self.assertFalse(bp.error_names_specific_cause(""))

    def test_specific_cause_detected(self):
        for t in ["invalid parameter foo", "tool schema error", "image not supported",
                  "role must be user", "malformed request"]:
            self.assertTrue(bp.error_names_specific_cause(t), t)

    def test_overflow_phrasing_is_not_a_specific_cause(self):
        # A real overflow phrased with GENERIC 400 words ("invalid", "must be") must
        # NOT count as a specific non-overflow cause — else the self-heal is suppressed.
        self.assertFalse(bp.error_names_specific_cause(
            "invalid request: total tokens must be <= 80000"))
        self.assertFalse(bp.error_names_specific_cause("400 invalid request"))

    def test_context_token_bound_forms_read_as_overflow(self):
        # Real overflow phrasings that the marker set must catch.
        for t in ["invalid request: total tokens must be <= 80000",
                  "Request tokens must be <= 8192",
                  "Request length is 9000 tokens; maximum length is 8192",
                  "input is too long"]:
            self.assertTrue(bp.error_strongly_overflow(t), t)

    def test_max_tokens_param_error_is_not_strong_overflow(self):
        # The OUTPUT-param error must NOT read as a context overflow just
        # because it contains "tokens must be" (the max_tokens exclusion).
        self.assertFalse(bp.error_strongly_overflow(
            "max_tokens must be less than or equal to 8192"))
        self.assertFalse(bp.error_weakly_overflow(
            "max_tokens must be less than or equal to 8192"))

    def test_bare_prompt_tokens_capability_error_is_not_overflow(self):
        # "prompt tokens are not supported" is a capability error, not overflow.
        self.assertFalse(bp.error_strongly_overflow(
            "prompt tokens are not supported for this endpoint"))
        self.assertTrue(bp.error_names_specific_cause(
            "prompt tokens are not supported for this endpoint"))

    def test_max_tokens_exceeds_room_is_weak_overflow_not_suppressed(self):
        # A body with a weak overflow signal that also mentions max_tokens is
        # NOT a specific cause (max_tokens is no longer a suppression marker).
        t = "max_tokens requested exceeds the tokens available after the prompt"
        self.assertTrue(bp.error_weakly_overflow(t))
        self.assertFalse(bp.error_names_specific_cause(t))

    def test_required_field_is_a_specific_cause(self):
        # A missing-required-field 400 must suppress the fraction heuristic.
        self.assertTrue(bp.error_names_specific_cause("messages.1.content is required"))


if __name__ == "__main__":
    unittest.main()
