"""P6 — polish batch: F05a (unknown-model classification), F05b (near-duplicate
finding merge), F05d (savings receipt under --json), F04 (catalog count
consistency). See docs/plans/2026-07-06-stress-test-remediation.md."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "bin", "ambient")


def _load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_polish", _BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_polish", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = _load_module()


class TestV25Polish(unittest.TestCase):
    # --- F05a: a mistyped -m is a MODEL error, not opaque 'unknown' -------
    def test_unknown_model_400_classifies_as_model(self):
        body = json.dumps({"error": {"message": "Unknown model: 'foo/bar'"}})
        cat, diag = amb.classify_error(400, body, "")
        self.assertEqual(cat, "model")
        self.assertIn("ambient models", diag)

    def test_other_400_still_unknown(self):
        cat, _ = amb.classify_error(
            400, json.dumps({"error": {"message": "weird"}}), ""
        )
        self.assertEqual(cat, "unknown")

    # --- F05b: conservative title match — NEVER false-merge distinct
    # findings (the fuzzy-overlap version was reverted; Codex showed it
    # dropped a distinct SQL-injection finding, which is worse than a
    # cosmetic duplicate.)
    def test_distinct_bugs_stay_separate(self):
        self.assertIs(
            amb._titles_match(("missing", "null", "check"),
                              ("missing", "rate", "limit")),
            False,
        )

    def test_distinct_injection_sites_do_not_false_merge(self):
        # Codex's counterexample: two DIFFERENT injection sites must stay
        # separate.
        a = ("sql", "injection", "in", "search")
        b = ("sql", "injection", "in", "login")
        self.assertIs(amb._titles_match(a, b), False)

    # --- F05d: savings receipt appears on stderr in --json mode -----------
    def test_json_emits_savings_receipt_on_stderr(self):
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            amb.emit_json("ask", model="z-ai/glm-5.2", api_key="", content="hi",
                          usage={"prompt_tokens": 10, "completion_tokens": 5},
                          exit_now=False)
        # stdout is clean JSON; the receipt rode stderr
        json.loads(out.getvalue())
        self.assertIn("[ambient z-ai/glm-5.2", err.getvalue())

    def test_receipt_redacts_key_if_it_ever_appears(self):
        # Codex: the receipt printed `model` unredacted; if model somehow
        # carries a key it must be scrubbed. redact() is applied now.
        key = "sk-secretkey-abcdef1234567890"
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            amb.emit_json("ask", model=key, api_key=key, content="hi",
                          usage={"prompt_tokens": 1, "completion_tokens": 1},
                          exit_now=False)
        self.assertNotIn(key, err.getvalue())

    def test_json_without_usage_has_no_receipt(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            amb.emit_json("ask", model="z-ai/glm-5.2", api_key="", content="hi",
                          exit_now=False)
        self.assertNotIn("[ambient", err.getvalue())

    # --- F04: catalog count is consistent across surfaces -----------------
    def test_alias_id_deduped_from_count(self):
        ids = ["ambient/large", "zai-org/GLM-5.1-FP8",
               "moonshotai/kimi-k2.7-code"]
        deduped = amb._dedupe_catalog_ids(ids)
        self.assertNotIn("zai-org/GLM-5.1-FP8", deduped)
        self.assertEqual(len(deduped), 2)

    def test_alias_kept_when_primary_absent(self):
        ids = ["zai-org/GLM-5.1-FP8", "moonshotai/kimi-k2.7-code"]
        self.assertIn("zai-org/GLM-5.1-FP8", amb._dedupe_catalog_ids(ids))


if __name__ == "__main__":
    unittest.main()
