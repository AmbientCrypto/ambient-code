"""Hermetic REMEDIATION tests.

H1: prune is LIVENESS-FIRST — a provably-alive pid is never TTL-pruned (a
    slow long call must keep its reservation counted); TTL reclaims only
    dead-pid or unknown-liveness records. Same-process re-gates REFRESH the
    existing reservation (max amount, fresh ts) instead of double-counting.
H2: an over-budget DECISION always refuses — a store-persist failure may
    only ever fail open on the allow path, never turn a refusal into allow.
a ledger append under lock-timeout goes to a per-pid SPOOL file merged
    back under the lock later — never an unlocked append a concurrent trim
    can truncate away.
the no-fcntl lock path never break-and-enters a lock whose owner might
    be alive; it breaks only a PROVABLY-dead owner's lock, else fails open.
release clears reservation ids only after the removal actually lands,
    so a failed release can be retried.
LOW: a REAL two-process race on the shared store — exactly one refused,
    no leaked reservation.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from test_ambient_v4_fleet import (
    BIN,
    amb,
    fleet_dir,
    ns,
    patched,
    rec,
    seed,
    store,
)


def dead_pid():
    p = subprocess.Popen(["/bin/sleep", "0"])
    p.wait()
    return p.pid


class TestLivenessFirstPrune(unittest.TestCase):
    """H1: TTL must never prune a LIVE pid — it can still spend."""

    def test_live_pid_past_ttl_is_kept_and_still_counts(self):
        with fleet_dir() as d:
            # provably alive, but WAY past any TTL (patched so the test also
            # holds on Windows, where real liveness is unknowable; real
            # POSIX probing is covered by test_real_dead_pid_detected_on_posix)
            seed(d, [rec(4.5, age=100000.0)])
            with patched(amb, _pid_alive=lambda pid: True), \
                    self.assertRaises(SystemExit) as cm:
                amb._gate_amount(1.0, ns(allow_cost=False), {})
            self.assertIn("already reserved", str(cm.exception))

    def test_dead_pid_pruned_even_when_fresh(self):
        with fleet_dir() as d:
            seed(d, [rec(4.5, pid=1234567, age=1.0)])
            with patched(amb, _pid_alive=lambda pid: False):
                amb._gate_amount(1.0, ns(allow_cost=False), {})  # no exit
            amb._fleet_release_all()
            self.assertEqual(store(d), [])

    def test_permission_error_means_unknown_liveness(self):
        if os.name != "posix":
            self.skipTest("POSIX pid probe only")

        def denied(pid, sig):
            raise PermissionError

        with patched(os, kill=denied):
            self.assertIsNone(amb._pid_alive(12345))

    def test_unknown_liveness_past_ttl_still_pruned(self):
        with fleet_dir() as d:
            seed(d, [rec(4.5, pid=1234567, age=100000.0)])
            with patched(amb, _pid_alive=lambda pid: None):
                amb._gate_amount(1.0, ns(allow_cost=False), {})  # no exit
            amb._fleet_release_all()


class TestSameProcessRefresh(unittest.TestCase):
    """H1: re-gating in the SAME process refreshes, never double-counts."""

    def test_regate_refreshes_ts_and_keeps_max_amount(self):
        with fleet_dir() as d:
            amb._gate_amount(2.0, ns(allow_cost=False), {})
            recs = store(d)
            self.assertEqual(len(recs), 1)
            # age our own record far past nothing-in-particular
            recs[0]["ts"] = time.time() - 800.0
            seed(d, recs)
            amb._gate_amount(1.0, ns(allow_cost=False), {})
            recs = store(d)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["amount"], 2.0)  # max, not 3.0
            self.assertLess(time.time() - recs[0]["ts"], 60.0)  # refreshed
            amb._fleet_release_all()
            self.assertEqual(store(d), [])

    def test_regate_does_not_double_count_against_ceiling(self):
        with fleet_dir() as d:
            seed(d, [rec(3.0, rid="sib")])
            amb._gate_amount(1.5, ns(allow_cost=False), {})
            # double-counting would see 3.0 + 1.5 + 1.0 = 5.5 > 5 and refuse;
            # the refreshed max is 3.0 + max(1.5, 1.0) = 4.5 <= 5 -> allowed
            amb._gate_amount(1.0, ns(allow_cost=False), {})
            recs = store(d)
            self.assertEqual(len(recs), 2)
            ours = [r for r in recs if r["id"] != "sib"]
            self.assertEqual(len(ours), 1)
            self.assertEqual(ours[0]["amount"], 1.5)
            amb._fleet_release_all()
            self.assertEqual([r["id"] for r in store(d)], ["sib"])

    def test_regate_grows_reservation_to_new_max(self):
        with fleet_dir() as d:
            amb._gate_amount(1.0, ns(allow_cost=False), {})
            amb._gate_amount(2.0, ns(allow_cost=False), {})
            recs = store(d)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["amount"], 2.0)
            self.assertEqual(len(amb._FLEET_RES_IDS), 1)
            amb._fleet_release_all()

    def test_regate_refused_when_new_max_blows_ceiling(self):
        with fleet_dir() as d:
            seed(d, [rec(3.0, rid="sib")])
            amb._gate_amount(1.5, ns(allow_cost=False), {})
            with self.assertRaises(SystemExit) as cm:
                amb._gate_amount(2.1, ns(allow_cost=False), {})  # 3+2.1 > 5
            self.assertIn("already reserved", str(cm.exception))


class TestRefusalNeverSwallowed(unittest.TestCase):
    """H2: machinery failure may fail open ONLY on the allow path."""

    def test_over_budget_refusal_survives_store_write_failure(self):
        def boom(*a, **k):
            raise OSError("disk full")

        with fleet_dir() as d:
            seed(d, [rec(4.9)])
            with patched(amb, _write_reservations=boom), \
                    self.assertRaises(SystemExit) as cm:
                amb._gate_amount(1.0, ns(allow_cost=False), {})
            self.assertIn("already reserved", str(cm.exception))

    def test_reserve_path_write_failure_still_fails_open(self):
        def boom(*a, **k):
            raise OSError("disk full")

        with fleet_dir() as d:
            seed(d, [rec(1.0)])
            err = io.StringIO()
            with patched(amb, _write_reservations=boom), \
                    contextlib.redirect_stderr(err):
                amb._gate_amount(2.0, ns(allow_cost=False), {})  # no exit
            self.assertIn("fleet", err.getvalue().lower())


class TestLedgerSpool(unittest.TestCase):
    """no unlocked append that a concurrent trim can truncate away."""

    def _held_lock(self, d):
        lock_path = os.path.join(d, ".usage.lock")
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        amb.fcntl.flock(fd, amb.fcntl.LOCK_EX)
        return fd

    def test_lock_timeout_appends_to_spool_not_main(self):
        if amb.fcntl is None:
            self.skipTest("flock semantics")
        with fleet_dir() as d:
            up = os.path.join(d, "usage.jsonl")
            with open(up, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": 1, "model": "old",
                                     "in": 1, "out": 1}) + "\n")
            fd = self._held_lock(d)
            try:
                with patched(amb, _LEDGER_LOCK_WAIT_S=0.2):
                    amb.log_usage("m", {"prompt_tokens": 1,
                                        "completion_tokens": 1})
            finally:
                amb.fcntl.flock(fd, amb.fcntl.LOCK_UN)
                os.close(fd)
            with open(up, encoding="utf-8") as fh:
                self.assertEqual(len(fh.readlines()), 1)  # main untouched
            spool = f"{up}.spool.{os.getpid()}"
            self.assertTrue(os.path.exists(spool))
            with open(spool, encoding="utf-8") as fh:
                rows = [json.loads(x) for x in fh]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["model"], "m")
            # a later UNCONTENDED write merges the spool back
            amb.log_usage("m2", {"prompt_tokens": 2, "completion_tokens": 2})
            self.assertFalse(os.path.exists(spool))
            with open(up, encoding="utf-8") as fh:
                models = [json.loads(x)["model"] for x in fh]
            self.assertEqual(models, ["old", "m", "m2"])  # nothing lost

    def test_foreign_spools_merged_only_when_owner_provably_dead(self):
        with fleet_dir() as d:
            up = os.path.join(d, "usage.jsonl")
            row = json.dumps({"ts": 2, "model": "spooled",
                              "in": 1, "out": 1}) + "\n"
            live = f"{up}.spool.999991"
            dead = f"{up}.spool.999992"
            for p in (live, dead):
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(row)
            liveness = {999991: True, 999992: False}
            with patched(amb, _pid_alive=lambda pid: liveness.get(pid)):
                amb.log_usage("m", {"prompt_tokens": 1,
                                    "completion_tokens": 1})
            self.assertTrue(os.path.exists(live))    # may still be appending
            self.assertFalse(os.path.exists(dead))   # safe to reclaim
            with open(up, encoding="utf-8") as fh:
                models = [json.loads(x)["model"] for x in fh]
            self.assertIn("spooled", models)
            self.assertIn("m", models)


class TestNoFcntlLockSafety(unittest.TestCase):
    """never break-and-enter a possibly-live owner's lock."""

    def test_stale_mtime_alone_never_breaks_a_live_owner(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".x.lock")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))  # provably-ALIVE owner
            old = time.time() - 120
            os.utime(path, (old, old))  # way past the old 30s heuristic
            with patched(amb, fcntl=None):
                with amb._fs_lock(path, 0.3) as locked:
                    self.assertFalse(locked)  # fail open, no break-in
            self.assertTrue(os.path.exists(path))  # owner's lock intact

    def test_garbage_owner_token_never_breaks(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".x.lock")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("crashed")  # unknowable owner
            old = time.time() - 120
            os.utime(path, (old, old))
            with patched(amb, fcntl=None):
                with amb._fs_lock(path, 0.3) as locked:
                    self.assertFalse(locked)
            self.assertTrue(os.path.exists(path))

    def test_provably_dead_owner_lock_is_broken(self):
        if os.name != "posix":
            self.skipTest("needs provable pid death (POSIX probe)")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".x.lock")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(str(dead_pid()))
            with patched(amb, fcntl=None):
                with amb._fs_lock(path, 1.0) as locked:
                    self.assertTrue(locked)  # crashed owner reclaimed


class TestReleaseRetry(unittest.TestCase):
    """ids survive a failed release so it can be retried."""

    def test_release_keeps_ids_when_write_fails_then_retries(self):
        def boom(*a, **k):
            raise OSError("disk full")

        with fleet_dir() as d:
            amb._gate_amount(2.0, ns(allow_cost=False), {})
            self.assertEqual(len(amb._FLEET_RES_IDS), 1)
            with patched(amb, _write_reservations=boom):
                amb._fleet_release_all()  # must not raise
            self.assertEqual(len(amb._FLEET_RES_IDS), 1)  # retryable
            self.assertEqual(len(store(d)), 1)            # still reserved
            amb._fleet_release_all()  # retry succeeds
            self.assertEqual(amb._FLEET_RES_IDS, [])
            self.assertEqual(store(d), [])

    def test_release_keeps_ids_when_lock_unavailable(self):
        @contextlib.contextmanager
        def no_lock(path, wait_s):
            yield False

        with fleet_dir() as d:
            amb._gate_amount(2.0, ns(allow_cost=False), {})
            with patched(amb, _fs_lock=no_lock):
                amb._fleet_release_all()
            self.assertEqual(len(amb._FLEET_RES_IDS), 1)
            amb._fleet_release_all()
            self.assertEqual(amb._FLEET_RES_IDS, [])
            self.assertEqual(store(d), [])


_RACE_CHILD = r"""
import argparse, importlib.machinery, importlib.util, os, sys, time
BIN, D, TAG = sys.argv[1], sys.argv[2], sys.argv[3]
loader = importlib.machinery.SourceFileLoader("amb_child", BIN)
spec = importlib.util.spec_from_loader("amb_child", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
mod.USAGE_PATH = os.path.join(D, "usage.jsonl")
args = argparse.Namespace(allow_cost=False, yes=True)
try:
    mod._gate_amount(3.0, args, {})
except SystemExit:
    open(os.path.join(D, TAG + ".refused"), "w").close()
    raise
open(os.path.join(D, TAG + ".ok"), "w").close()
deadline = time.time() + 20
while time.time() < deadline and \
        not os.path.exists(os.path.join(D, "release.go")):
    time.sleep(0.05)
"""


class TestTwoProcessRace(unittest.TestCase):
    """LOW: a REAL cross-process race on _fs_lock/_gate_amount."""

    def test_exactly_one_refused_and_no_reservation_leaked(self):
        if os.name != "posix":
            self.skipTest("aggregate guarantee needs flock (POSIX)")
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, AMBIENT_MAX_SPEND="5")
            for var in ("AMBIENT_FLEET_BUDGET", "AMBIENT_RESERVATION_TTL"):
                env.pop(var, None)
            procs = [
                subprocess.Popen(
                    [sys.executable, "-c", _RACE_CHILD, BIN, d, tag],
                    env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for tag in ("a", "b")
            ]
            flags = lambda: [f for f in os.listdir(d)  # noqa: E731
                             if f.endswith((".ok", ".refused"))]
            deadline = time.time() + 20
            while time.time() < deadline and len(flags()) < 2:
                time.sleep(0.05)
            got = sorted(flags())
            with open(os.path.join(d, "release.go"), "w"):
                pass
            outs = [p.communicate(timeout=20) for p in procs]
            codes = [p.returncode for p in procs]
            oks = [f for f in got if f.endswith(".ok")]
            refused = [f for f in got if f.endswith(".refused")]
            self.assertEqual(len(oks), 1, (got, codes, outs))
            self.assertEqual(len(refused), 1, (got, codes, outs))
            self.assertEqual(sorted(codes), [0, 1])
            loser_err = outs[codes.index(1)][1].decode()
            self.assertIn("already reserved", loser_err)
            # no leaked reservation after both processes exit
            res = os.path.join(d, "reservations.jsonl")
            leftover = []
            if os.path.exists(res):
                with open(res, encoding="utf-8") as fh:
                    leftover = [x for x in fh if x.strip()]
            self.assertEqual(leftover, [])


if __name__ == "__main__":
    unittest.main()
