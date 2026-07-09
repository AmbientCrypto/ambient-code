"""P1 — adaptive capability core: learn per-model behavior from real outcomes,
recover on later success, honor AMBIENT_TELEMETRY=off. See
docs/plans/2026-07-06-stress-test-remediation.md."""
import importlib.machinery
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "bin", "ambient")


def _load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_adaptive", _BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_adaptive", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = _load_module()

_MISSING = object()


class AdaptiveCapabilityTests(unittest.TestCase):
    def setUp(self):
        """Point the capability store at a temp file and reset the process memo
        so each test starts from a clean, isolated store."""
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        self.store = Path(tmp_dir) / "capabilities.json"
        self._orig_capability_path = amb.CAPABILITY_PATH
        self._orig_cap_cache = amb._CAP_CACHE
        self._orig_telemetry = os.environ.pop("AMBIENT_TELEMETRY", _MISSING)
        amb.CAPABILITY_PATH = str(self.store)
        amb._CAP_CACHE = None

    def tearDown(self):
        amb._CAP_CACHE = None  # reset after, as before (fixture post-yield)
        amb.CAPABILITY_PATH = self._orig_capability_path
        amb._CAP_CACHE = self._orig_cap_cache
        if self._orig_telemetry is _MISSING:
            os.environ.pop("AMBIENT_TELEMETRY", None)
        else:
            os.environ["AMBIENT_TELEMETRY"] = self._orig_telemetry

    def test_unknown_before_any_history(self):
        self.assertEqual(amb.cap_state("z-ai/glm-5.2", "structured_json"), "unknown")

    def test_becomes_unreliable_after_repeated_failures(self):
        for _ in range(amb.CAP_FAIL_THRESHOLD):
            amb.record_cap("z-ai/glm-5.2", "structured_json", False)
        self.assertEqual(amb.cap_state("z-ai/glm-5.2", "structured_json"), "unreliable")

    def test_single_failure_is_not_yet_unreliable(self):
        amb.record_cap("z-ai/glm-5.2", "structured_json", False)
        self.assertNotEqual(amb.cap_state("z-ai/glm-5.2", "structured_json"), "unreliable")

    def test_recovers_to_ok_on_later_success(self):
        for _ in range(amb.CAP_FAIL_THRESHOLD + 1):
            amb.record_cap("z-ai/glm-5.2", "structured_json", False)
        self.assertEqual(amb.cap_state("z-ai/glm-5.2", "structured_json"), "unreliable")
        amb.record_cap("z-ai/glm-5.2", "structured_json", True)  # model improved
        self.assertEqual(amb.cap_state("z-ai/glm-5.2", "structured_json"), "ok")

    def test_dimensions_are_independent(self):
        for _ in range(amb.CAP_FAIL_THRESHOLD):
            amb.record_cap("z-ai/glm-5.2", "build_plan", False)
        self.assertEqual(amb.cap_state("z-ai/glm-5.2", "build_plan"), "unreliable")
        self.assertEqual(amb.cap_state("z-ai/glm-5.2", "structured_json"), "unknown")

    def test_models_are_independent(self):
        for _ in range(amb.CAP_FAIL_THRESHOLD):
            amb.record_cap("z-ai/glm-5.2", "structured_json", False)
        self.assertEqual(
            amb.cap_state("moonshotai/kimi-k2.7-code", "structured_json"), "unknown"
        )

    def test_persists_across_process_memo_reset(self):
        for _ in range(amb.CAP_FAIL_THRESHOLD):
            amb.record_cap("z-ai/glm-5.2", "structured_json", False)
        self.assertTrue(os.path.exists(str(self.store)))
        amb._CAP_CACHE = None  # simulate a fresh process
        self.assertEqual(amb.cap_state("z-ai/glm-5.2", "structured_json"), "unreliable")

    def test_telemetry_off_disables_learning(self):
        os.environ["AMBIENT_TELEMETRY"] = "off"
        amb._CAP_CACHE = None
        for _ in range(amb.CAP_FAIL_THRESHOLD + 2):
            amb.record_cap("z-ai/glm-5.2", "structured_json", False)
        self.assertEqual(amb.cap_state("z-ai/glm-5.2", "structured_json"), "unknown")

    def test_corrupt_store_is_not_fatal(self):
        self.store.write_text("{ this is not json", encoding="utf-8")
        amb._CAP_CACHE = None
        self.assertEqual(amb.cap_state("z-ai/glm-5.2", "structured_json"), "unknown")
        amb.record_cap("z-ai/glm-5.2", "structured_json", False)  # must not raise

    def test_store_written_0600(self):
        amb.record_cap("z-ai/glm-5.2", "structured_json", False)
        if os.name != "nt":  # Windows has no POSIX owner-only mode bits
            mode = os.stat(str(self.store)).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_stale_success_does_not_mask_fresh_failures(self):
        # Codex: [ok, fail, fail] must be 'unreliable', not 'ok' (hysteresis keyed
        # on the most-recent outcomes, not "any success ever").
        amb.record_cap("m", "structured_json", True)
        amb.record_cap("m", "structured_json", False)
        amb.record_cap("m", "structured_json", False)
        self.assertEqual(amb.cap_state("m", "structured_json"), "unreliable")

    def test_malformed_model_entry_does_not_crash(self):
        # Codex: a valid-JSON but structurally-wrong entry ({"m": "bad"}) must not
        # raise AttributeError on the audit path.
        self.store.write_text('{"m": "bad"}', encoding="utf-8")
        amb._CAP_CACHE = None
        self.assertEqual(amb.cap_state("m", "structured_json"), "unknown")
        amb.record_cap("m", "structured_json", False)  # must not raise

    def test_concurrent_writers_do_not_lose_outcomes(self):
        # Codex: unlocked read-modify-write lost outcomes. Two sequential record_cap
        # calls (memo refreshed each time) must both persist.
        amb.record_cap("m", "structured_json", False)
        amb.record_cap("m", "structured_json", False)
        self.assertEqual(amb.cap_state("m", "structured_json"), "unreliable")

    def test_adaptive_response_format_skips_schema_when_unreliable(self):
        class _Prof:
            features = ["structured_outputs"]

        amb._CAP_CACHE = None
        for _ in range(amb.CAP_FAIL_THRESHOLD):
            amb.record_cap("z-ai/glm-5.2", "structured_json", False)
        # unreliable => no response_format (go straight to prose+parser)
        self.assertIsNone(
            amb.adaptive_response_format("z-ai/glm-5.2", _Prof(), {"type": "object"})
        )
        # a capable/unknown model still gets the strict schema (optimistic)
        rf = amb.adaptive_response_format(
            "moonshotai/kimi-k2.7-code", _Prof(), {"type": "object"}
        )
        self.assertTrue(rf)
        self.assertEqual(rf.get("type"), "json_schema")


if __name__ == "__main__":
    unittest.main()
