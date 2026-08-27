"""ambient_code.tool_ids: the reversible tool-id codec (the brick fix).

Hermetic. Proves, as a PROPERTY over many generated ids (not just examples):
decode(encode(x)) == x, every emitted id is Anthropic-legal by fullmatch (NOT a
`$`-anchored search that a trailing newline can slip past), passthrough for legal ids,
MAGIC provenance + fail-closed on corruption, and the length/encodability guards.
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from ambient_code import tool_ids as ti  # noqa: E402

# The CORRECT oracle: fullmatch, so a trailing newline cannot masquerade as legal.
_LEGAL = re.compile(r"[A-Za-z0-9_-]+")


def _is_anthropic_legal(s):
    return isinstance(s, str) and _LEGAL.fullmatch(s) is not None


# Real shapes plus adversarial cases incl. the newline that broke v1.
REAL_IDS = [
    "functions.Read:0",            # DeepSeek
    "functions.save_note:0",       # Kimi
    "chatcmpl-tool-deadbeef00",    # GLM (already legal)
    "toolu_01A09q90qw90lq917",     # native-Anthropic-looking (already legal)
    "call_abc123",                 # OpenAI classic (already legal)
    "functions.some_really_long_tool_name_here:12",
    "functions.café_note:3",  # non-ascii tool name
    "amb1_collision",              # collides with our reserved prefix
    "x",                           # single char
    "trailing_newline\n",          # the v1 bug: must be ENCODED, not passed through
    "has space:0",
    "tab\tsep:1",
    "\U0001f600emoji:0",           # astral-plane
]


class TestRoundTripProperty(unittest.TestCase):
    def test_property_over_real_and_generated_ids(self):
        generated = ["fn.%d:%d" % (i, i % 4) for i in range(0, 200)]
        generated += ["a" * n for n in (1, 10, 47, 300, 512)]  # legal, varied lengths
        generated += ["bad.%s:0" % ("z" * n) for n in (1, 20, 100)]  # illegal, varied
        for oid in REAL_IDS + generated:
            enc = ti.encode_tool_id(oid)
            self.assertTrue(_is_anthropic_legal(enc),
                            "encoded id not Anthropic-legal (fullmatch): %r" % enc)
            self.assertEqual(ti.decode_tool_id(enc), oid, "round-trip failed for %r" % oid)

    def test_parallel_ids_stay_distinct(self):
        a = ti.encode_tool_id("functions.Read:0")
        b = ti.encode_tool_id("functions.Read:1")
        self.assertNotEqual(a, b)
        self.assertEqual(ti.decode_tool_id(a), "functions.Read:0")
        self.assertEqual(ti.decode_tool_id(b), "functions.Read:1")

    def test_trailing_newline_is_encoded_not_passed_through(self):
        enc = ti.encode_tool_id("abc\n")
        self.assertTrue(enc.startswith("amb1_"))
        self.assertTrue(_is_anthropic_legal(enc))
        self.assertEqual(ti.decode_tool_id(enc), "abc\n")


class TestPassthrough(unittest.TestCase):
    def test_already_legal_id_passes_through_unchanged(self):
        for legal in ("chatcmpl-tool-deadbeef00", "toolu_01A09q90qw90lq917", "call_abc123", "x"):
            self.assertEqual(ti.encode_tool_id(legal), legal)
            self.assertEqual(ti.decode_tool_id(legal), legal)  # non-prefixed -> passthrough

    def test_illegal_char_id_is_encoded(self):
        enc = ti.encode_tool_id("functions.Read:0")
        self.assertTrue(enc.startswith("amb1_"))
        self.assertNotEqual(enc, "functions.Read:0")


class TestPrefixCollision(unittest.TestCase):
    def test_id_starting_with_reserved_prefix_is_encoded(self):
        enc = ti.encode_tool_id("amb1_collision")
        self.assertTrue(enc.startswith("amb1_"))
        self.assertNotEqual(enc, "amb1_collision")
        self.assertEqual(ti.decode_tool_id(enc), "amb1_collision")


class TestFailClosed(unittest.TestCase):
    def test_empty_and_nonstring_raise(self):
        for bad in ("", None, 123):
            with self.assertRaises(ti.ToolIdError):
                ti.encode_tool_id(bad)  # type: ignore[arg-type]
        with self.assertRaises(ti.ToolIdError):
            ti.decode_tool_id("")

    def test_over_long_id_raises(self):
        with self.assertRaises(ti.ToolIdError):
            ti.encode_tool_id("z" * (ti.MAX_RAW_ID_LEN + 1))

    def test_lone_surrogate_fails_closed(self):
        with self.assertRaises(ti.ToolIdError):
            ti.encode_tool_id("\ud800")  # not UTF-8 encodable

    def test_reserved_prefix_illegal_b64_raises(self):
        with self.assertRaises(ti.ToolIdError):
            ti.decode_tool_id("amb1_@@@")  # not base64url alphabet

    def test_reserved_prefix_without_magic_raises(self):
        # "amb1_Og" decodes to b":" (no MAGIC marker) -> not ours -> fail closed.
        with self.assertRaises(ti.ToolIdError):
            ti.decode_tool_id("amb1_Og")

    def test_corrupted_encoded_id_fails_closed(self):
        # Flip the last base64 char of one of OUR ids: this yields either a changed
        # original (canonical re-encode differs), a non-canonical form (re-encode
        # differs), or invalid UTF-8 — every branch must fail closed, never a wrong id.
        good = ti.encode_tool_id("functions.Read:0")
        flipped = good[:-1] + ("A" if good[-1] != "A" else "B")
        with self.assertRaises(ti.ToolIdError):
            ti.decode_tool_id(flipped)


if __name__ == "__main__":
    unittest.main()
