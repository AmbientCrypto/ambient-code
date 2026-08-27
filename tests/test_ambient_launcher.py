"""ambient_code.launcher: pure launcher helpers (token, state file, ports, health)."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from ambient_code import launcher as lm  # noqa: E402


class TestToken(unittest.TestCase):
    def test_token_is_random_and_prefixed(self):
        a, b = lm.generate_local_token(), lm.generate_local_token()
        self.assertTrue(a.startswith("ambr_"))
        self.assertNotEqual(a, b)
        self.assertGreater(len(a), 20)


class TestStateFile(unittest.TestCase):
    def test_write_read_roundtrip_and_perms(self):
        d = tempfile.mkdtemp()
        try:
            path = lm.bridge_state_path(d)
            lm.write_bridge_state(path, "127.0.0.1", 4521, 999, "ambr_tok")
            state = lm.read_bridge_state(path)
            self.assertEqual(state["port"], 4521)
            self.assertEqual(state["token"], "ambr_tok")
            # 0600 (owner-only) — the token must never be world-readable.
            # POSIX perm bits only; Windows enforces access via ACLs, not st_mode.
            if os.name == "posix":
                self.assertEqual(os.stat(path).st_mode & 0o077, 0)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_read_missing_or_corrupt_returns_none(self):
        d = tempfile.mkdtemp()
        try:
            self.assertIsNone(lm.read_bridge_state(os.path.join(d, "nope.json")))
            bad = os.path.join(d, "bad.json")
            with open(bad, "w") as fh:
                fh.write("{not json")
            self.assertIsNone(lm.read_bridge_state(bad))
            nostate = os.path.join(d, "nostate.json")
            with open(nostate, "w") as fh:
                json.dump({"host": "x"}, fh)  # no int port
            self.assertIsNone(lm.read_bridge_state(nostate))
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class TestPortsAndHealth(unittest.TestCase):
    def test_find_free_port(self):
        p = lm.find_free_port()
        self.assertTrue(1024 <= p <= 65535)

    def test_health_false_when_nothing_listening(self):
        self.assertFalse(lm.health_ok("127.0.0.1", lm.find_free_port(), timeout=0.5))
        self.assertFalse(lm.bridge_is_live(None))
        self.assertFalse(lm.bridge_is_live(
            {"host": "127.0.0.1", "port": lm.find_free_port(), "token": "t"}, timeout=0.5))

    def test_read_state_rejects_non_loopback_host(self):
        d = tempfile.mkdtemp()
        try:
            path = lm.bridge_state_path(d)
            lm.write_bridge_state(path, "10.0.0.5", 4521, 1, "ambr_tok")  # routable host!
            self.assertIsNone(lm.read_bridge_state(path))  # rejected -> never target it
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_bridge_is_ours_false_without_token_or_off_loopback(self):
        self.assertFalse(lm.bridge_is_ours("127.0.0.1", lm.find_free_port(), "", timeout=0.5))
        self.assertFalse(lm.bridge_is_ours("10.0.0.5", 4521, "tok", timeout=0.5))


if __name__ == "__main__":
    unittest.main()
