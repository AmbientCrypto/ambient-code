"""SH1 — ambient_code.learned: the transparent per-model window self-heal."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from ambient_code.learned import LearnedWindows  # noqa: E402


class TestLearned(unittest.TestCase):
    def test_learned_wins_when_smaller_the_deepseek_case(self):
        lw = LearnedWindows()
        self.assertEqual(lw.effective("m", 50000), 50000)  # nothing learned -> catalog
        lw.record("m", 40000)                              # model 400'd at 40k
        self.assertEqual(lw.effective("m", 50000), 40000)  # now plan against 40k

    def test_catalog_wins_when_smaller(self):
        lw = LearnedWindows()
        lw.record("m", 40000)
        self.assertEqual(lw.effective("m", 30000), 30000)  # min(catalog, learned)

    def test_monotone_down_within_ttl(self):
        lw = LearnedWindows()
        lw.record("m", 40000, now=1000.0)
        lw.record("m", 45000, now=1001.0)   # a LARGER failure doesn't raise the ceiling...
        self.assertEqual(lw.effective("m", 50000, now=1002.0), 40000)
        lw.record("m", 38000, now=1003.0)   # ...but a smaller one lowers it
        self.assertEqual(lw.effective("m", 50000, now=1004.0), 38000)

    def test_ttl_expiry_recovers_to_catalog(self):
        lw = LearnedWindows(ttl_s=100)
        lw.record("m", 40000, now=1000.0)
        self.assertEqual(lw.effective("m", 50000, now=1050.0), 40000)   # still fresh
        self.assertEqual(lw.effective("m", 50000, now=1200.0), 50000)   # expired -> recover

    def test_catalog_none_returns_learned(self):
        lw = LearnedWindows()
        lw.record("m", 40000)
        self.assertEqual(lw.effective("m", None), 40000)

    def test_persistence_roundtrip_and_0600(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "learned.json")
            LearnedWindows(path=path).record("deepseek/x", 40000, now=1000.0)
            if os.name == "posix":   # POSIX perm bits only; Windows uses ACLs
                self.assertEqual(os.stat(path).st_mode & 0o077, 0)   # owner-only
            reloaded = LearnedWindows(path=path)
            self.assertEqual(reloaded.effective("deepseek/x", 50000, now=1001.0), 40000)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_bad_state_file_is_ignored(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "learned.json")
            with open(path, "w") as fh:
                fh.write("{garbage")
            lw = LearnedWindows(path=path)  # must not raise
            self.assertEqual(lw.effective("m", 50000), 50000)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_poisoned_rows_are_dropped(self):
        # Non-positive ceilings, NaN/inf timestamps, and huge overflowing
        # timestamps must be rejected on load — never crash, never stick a bad ceiling.
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "learned.json")
            with open(path, "w") as fh:
                json.dump({
                    "neg": [-5, 1000.0],            # non-positive ceiling
                    "zero": [0, 1000.0],            # zero ceiling
                    "nan": [40000, float("nan")],   # NaN ts (dumped as NaN by json)
                    "inf": [40000, float("inf")],   # inf ts
                    "boolwin": [True, 1000.0],       # bool posing as a ceiling
                    "good": [40000, 1000.0],         # the one valid row
                }, fh)
            lw = LearnedWindows(path=path)          # must not raise
            self.assertEqual(lw.effective("neg", 50000, now=1001.0), 50000)
            self.assertEqual(lw.effective("zero", 50000, now=1001.0), 50000)
            self.assertEqual(lw.effective("nan", 50000, now=1001.0), 50000)
            self.assertEqual(lw.effective("inf", 50000, now=1001.0), 50000)
            self.assertEqual(lw.effective("boolwin", 50000, now=1001.0), 50000)
            self.assertEqual(lw.effective("good", 50000, now=1001.0), 40000)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_non_finite_now_does_not_poison_ttl(self):
        # A NaN `now` must fall back to the clock, not store a never-expiring row.
        lw = LearnedWindows()
        lw.record("m", 40000, now=float("nan"))
        self.assertEqual(lw.effective("m", 50000), 40000)  # learned, and TTL still works


if __name__ == "__main__":
    unittest.main()
