"""Hermetic tests: cross-process FLEET spend enforcement.

4a) AMBIENT_MAX_SPEND becomes a true AGGREGATE ceiling across concurrent
    ambient processes via a reservations store (~/.config/ambient/
    reservations.jsonl) — concurrency is simulated by pre-seeding the store,
    no real second process needed.
4b) the usage-ledger trim (read-trim-write) runs under a cross-process lock
    so concurrent fan-out writers can't corrupt or lose ledger lines.

Everything is fail-open: the machinery must NEVER block or crash a
legitimate call due to its own failure. No network, no live API.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v4", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v4", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = load_module()


@contextlib.contextmanager
def patched(obj, **attrs):
    old = {}
    missing = object()
    for k, v in attrs.items():
        old[k] = getattr(obj, k, missing)
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is missing:
                delattr(obj, k)
            else:
                setattr(obj, k, v)


@contextlib.contextmanager
def env_var(name, value):
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


def ns(**kw):
    kw.setdefault("yes", True)  # never hit the TTY confirm in tests
    return argparse.Namespace(**kw)


@contextlib.contextmanager
def fleet_dir():
    """Point the whole fleet machinery (reservations + usage) at a tmpdir and
    reset per-process reservation state, so tests never touch the real
    ~/.config/ambient."""
    with tempfile.TemporaryDirectory() as d:
        with patched(amb, USAGE_PATH=os.path.join(d, "usage.jsonl"),
                     _FLEET_RES_IDS=[]), \
                env_var("AMBIENT_FLEET_BUDGET", None), \
                env_var("AMBIENT_MAX_SPEND", None), \
                env_var("AMBIENT_RESERVATION_TTL", None):
            yield d


def seed(d, records):
    with open(os.path.join(d, "reservations.jsonl"), "w",
              encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def store(d):
    path = os.path.join(d, "reservations.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def rec(amount, pid=None, age=0.0, rid=None):
    return {"pid": pid if pid is not None else os.getpid(),
            "ts": time.time() - age, "amount": amount,
            "id": rid or f"seed-{amount}-{age}"}


class TestFleetCeiling(unittest.TestCase):
    """4a: the aggregate ceiling across concurrent invocations."""

    def test_second_invocation_refused_when_fleet_total_exceeds_ceiling(self):
        with fleet_dir() as d:
            seed(d, [rec(4.0)])  # a live sibling already reserved 4
            err = io.StringIO()
            with contextlib.redirect_stderr(err), \
                    self.assertRaises(SystemExit) as cm:
                amb._gate_amount(2.0, ns(allow_cost=False), {})
            msg = str(cm.exception)
            self.assertIn("already reserved", msg)
            self.assertIn("budget", msg)  # default ceiling: generic, no dollar/ceiling named

    def test_refusal_names_fleet_total_and_count(self):
        with fleet_dir() as d:
            seed(d, [rec(3.0, rid="a"), rec(2.5, rid="b")])
            with self.assertRaises(SystemExit) as cm:
                amb._gate_amount(1.0, ns(allow_cost=False), {})
            msg = str(cm.exception)
            self.assertIn("already reserved", msg)  # default ceiling: no $
            self.assertIn("2 running", msg)

    def test_fits_under_ceiling_alongside_existing_reservation(self):
        with fleet_dir() as d:
            seed(d, [rec(1.0, rid="a")])
            amb._gate_amount(2.0, ns(allow_cost=False), {})  # no exit
            recs = store(d)
            self.assertEqual(len(recs), 2)
            self.assertIn(2.0, [r["amount"] for r in recs])
            amb._fleet_release_all()

    def test_lone_invocation_reserves_then_releases(self):
        with fleet_dir() as d:
            amb._gate_amount(2.0, ns(allow_cost=False), {})
            recs = store(d)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["pid"], os.getpid())
            self.assertEqual(recs[0]["amount"], 2.0)
            amb._fleet_release_all()
            self.assertEqual(store(d), [])

    def test_json_refusal_emits_error_envelope(self):
        with fleet_dir() as d:
            seed(d, [rec(4.9)])
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    self.assertRaises(SystemExit) as cm:
                amb._gate_amount(1.0, ns(allow_cost=False, json=True), {})
            self.assertEqual(cm.exception.code, 1)
            env = json.loads(out.getvalue())
            self.assertEqual(env["status"], "error")
            self.assertEqual(env["category"], "cost")
            self.assertEqual(env["schema_version"], 1)

    def test_per_invocation_ceiling_still_fires_first(self):
        with fleet_dir():
            with self.assertRaises(SystemExit) as cm:
                amb._gate_amount(9.0, ns(allow_cost=False), {})
            self.assertIn("ceiling", str(cm.exception))
            self.assertNotIn("already reserved", str(cm.exception))

    def test_worst_case_3x_bound_still_fires(self):
        with fleet_dir():
            with self.assertRaises(SystemExit) as cm:
                amb._gate_amount(0.4, ns(allow_cost=False), {}, bound=16.0)
            self.assertIn("worst-case", str(cm.exception).lower())

    def test_allow_cost_bypasses_fleet_refusal(self):
        with fleet_dir() as d:
            seed(d, [rec(100.0)])
            amb._gate_amount(2.0, ns(allow_cost=True), {})  # no exit


class TestPruning(unittest.TestCase):
    """Stale reservations (dead pid / expired TTL) never wedge the budget."""

    def test_dead_pid_reservation_is_pruned(self):
        with fleet_dir() as d:
            seed(d, [rec(100.0, pid=1234567)])
            with patched(amb, _pid_alive=lambda pid: False):
                amb._gate_amount(1.0, ns(allow_cost=False), {})  # no exit
            pids = [r["pid"] for r in store(d)]
            self.assertNotIn(1234567, pids)
            amb._fleet_release_all()

    def test_real_dead_pid_detected_on_posix(self):
        if os.name != "posix":
            self.skipTest("POSIX pid liveness only")
        p = subprocess.Popen(["/bin/sleep", "0"])
        p.wait()
        self.assertIs(amb._pid_alive(p.pid), False)
        self.assertIs(amb._pid_alive(os.getpid()), True)

    def test_ttl_expired_reservation_is_pruned(self):
        """TTL reclaims records whose liveness is UNKNOWABLE (Windows-style);
        a provably-alive pid is never TTL-pruned."""
        with fleet_dir() as d:
            seed(d, [rec(100.0, age=100000.0)])  # way past the default TTL
            with patched(amb, _pid_alive=lambda pid: None):
                amb._gate_amount(1.0, ns(allow_cost=False), {})  # no exit
            amb._fleet_release_all()
            self.assertEqual(store(d), [])

    def test_custom_ttl_env(self):
        with fleet_dir() as d:
            seed(d, [rec(100.0, age=60.0)])
            with env_var("AMBIENT_RESERVATION_TTL", "50"), \
                    patched(amb, _pid_alive=lambda pid: None):
                amb._gate_amount(1.0, ns(allow_cost=False), {})  # pruned
            amb._fleet_release_all()
        with fleet_dir() as d:
            seed(d, [rec(100.0, age=10.0)])
            with env_var("AMBIENT_RESERVATION_TTL", "50"), \
                    patched(amb, _pid_alive=lambda pid: None), \
                    self.assertRaises(SystemExit):
                amb._gate_amount(1.0, ns(allow_cost=False), {})  # kept

    def test_unknown_pid_liveness_degrades_to_ttl_only(self):
        """Windows-style: _pid_alive can't tell → TTL alone decides."""
        with fleet_dir() as d:
            seed(d, [rec(100.0, pid=1234567, age=1.0)])
            with patched(amb, _pid_alive=lambda pid: None), \
                    self.assertRaises(SystemExit):
                amb._gate_amount(1.0, ns(allow_cost=False), {})  # kept: fresh
        with fleet_dir() as d:
            seed(d, [rec(100.0, pid=1234567, age=5000.0)])  # past the 1h TTL
            with patched(amb, _pid_alive=lambda pid: None):
                amb._gate_amount(1.0, ns(allow_cost=False), {})  # pruned: old
            amb._fleet_release_all()


class TestOptOutAndFailOpen(unittest.TestCase):
    def test_fleet_budget_off_falls_back_to_per_invocation(self):
        with fleet_dir() as d:
            seed(d, [rec(100.0)])
            with env_var("AMBIENT_FLEET_BUDGET", "off"):
                amb._gate_amount(2.0, ns(allow_cost=False), {})  # no exit
            self.assertEqual(len(store(d)), 1)  # nothing written either

    def test_per_invocation_ceiling_still_binds_when_fleet_off(self):
        with fleet_dir():
            with env_var("AMBIENT_FLEET_BUDGET", "off"), \
                    self.assertRaises(SystemExit):
                amb._gate_amount(9.0, ns(allow_cost=False), {})

    def test_corrupt_store_is_reset_not_fatal(self):
        with fleet_dir() as d:
            path = os.path.join(d, "reservations.jsonl")
            with open(path, "wb") as fh:
                fh.write(b"\x00\x01 not json at all {{{\n[1,2\n")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                amb._gate_amount(1.0, ns(allow_cost=False), {})  # no crash
            recs = store(d)  # reset: only our fresh reservation survives
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["pid"], os.getpid())
            self.assertIn("fleet", err.getvalue().lower())
            amb._fleet_release_all()

    def test_unreadable_store_degrades_with_warning(self):
        if os.name != "posix" or os.geteuid() == 0:
            self.skipTest("permission semantics")
        with fleet_dir() as d:
            path = os.path.join(d, "reservations.jsonl")
            seed(d, [rec(100.0)])
            os.chmod(path, 0)
            try:
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    amb._gate_amount(1.0, ns(allow_cost=False), {})  # no exit
                self.assertIn("fleet", err.getvalue().lower())
            finally:
                os.chmod(path, 0o600)

    def test_lock_unavailable_degrades_with_warning(self):
        @contextlib.contextmanager
        def no_lock(path, wait_s):
            yield False
        with fleet_dir() as d:
            seed(d, [rec(100.0)])
            err = io.StringIO()
            with patched(amb, _fs_lock=no_lock), \
                    contextlib.redirect_stderr(err):
                amb._gate_amount(1.0, ns(allow_cost=False), {})  # no exit
            self.assertIn("fleet", err.getvalue().lower())
            self.assertEqual(len(store(d)), 1)  # nothing written

    def test_machinery_exception_degrades_with_warning(self):
        def boom(*a, **k):
            raise RuntimeError("disk on fire")
        with fleet_dir() as d:
            seed(d, [rec(100.0)])
            err = io.StringIO()
            with patched(amb, _load_reservations=boom), \
                    contextlib.redirect_stderr(err):
                amb._gate_amount(1.0, ns(allow_cost=False), {})  # no exit
            self.assertIn("fleet", err.getvalue().lower())

    def test_release_is_idempotent_and_never_raises(self):
        with fleet_dir():
            amb._fleet_release_all()
            amb._fleet_release_all()  # nothing reserved, nothing to do


class TestFsLock(unittest.TestCase):
    """The cross-platform fail-open lock primitive."""

    def test_lock_acquires_and_releases(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".x.lock")
            with amb._fs_lock(path, 1.0) as locked:
                self.assertTrue(locked)
            with amb._fs_lock(path, 1.0) as locked:  # reusable
                self.assertTrue(locked)

    def test_windows_fallback_acquires_without_fcntl(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".x.lock")
            with patched(amb, fcntl=None):
                with amb._fs_lock(path, 1.0) as locked:
                    self.assertTrue(locked)
                    self.assertTrue(os.path.exists(path))
                self.assertFalse(os.path.exists(path))  # released

    def test_windows_fallback_contended_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".x.lock")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("held")
            with patched(amb, fcntl=None):
                t0 = time.time()
                with amb._fs_lock(path, 0.3) as locked:
                    self.assertFalse(locked)  # fail-open, never deadlock
                self.assertLess(time.time() - t0, 5.0)
            self.assertTrue(os.path.exists(path))  # not ours to remove

    def test_windows_fallback_breaks_lock_of_provably_dead_owner(self):
        """a lock is broken ONLY when its recorded owner pid is
        provably dead — never on mtime age alone (the owner may be alive
        and slow, and breaking in would allow two concurrent store
        rewrites). Unprovable owners fail open (see v5 hardening tests)."""
        if os.name != "posix":
            self.skipTest("needs provable pid death (POSIX probe)")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".x.lock")
            p = subprocess.Popen(["/bin/sleep", "0"])
            p.wait()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(str(p.pid))
            with patched(amb, fcntl=None):
                with amb._fs_lock(path, 1.0) as locked:
                    self.assertTrue(locked)

    def test_unwritable_dir_fails_open(self):
        with patched(amb, fcntl=None):
            with amb._fs_lock("/nonexistent-dir-xyz/.x.lock", 0.2) as locked:
                self.assertFalse(locked)


class TestLedgerLock(unittest.TestCase):
    """4b: the usage-ledger trim can't corrupt/lose lines under concurrency."""

    def test_concurrent_writers_never_corrupt_the_ledger(self):
        with fleet_dir() as d:
            up = os.path.join(d, "usage.jsonl")
            with patched(amb, USAGE_PATH=up, USAGE_MAX_BYTES=400,
                         USAGE_TRIM_KEEP_LINES=5):
                def writer():
                    for i in range(40):
                        amb.log_usage("m", {"prompt_tokens": i,
                                            "completion_tokens": i})
                threads = [threading.Thread(target=writer) for _ in range(4)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            with open(up, encoding="utf-8") as fh:
                lines = fh.readlines()
            self.assertTrue(lines)
            for line in lines:  # every line intact — no torn/interleaved rows
                row = json.loads(line)
                self.assertEqual(row["model"], "m")

    def test_lock_timeout_spools_instead_of_unlocked_append(self):
        """A sibling holding the ledger lock means WE must not touch the
        main ledger at all (an unlocked append can be truncated
        away by the sibling's read-trim-write) — the line goes to a per-pid
        SPOOL file and is merged back under the lock on a later write."""
        if amb.fcntl is None:
            self.skipTest("flock semantics")
        with fleet_dir() as d:
            up = os.path.join(d, "usage.jsonl")
            seeded = [json.dumps({"ts": 1, "model": "old", "in": 1,
                                  "out": 1}) + "\n"] * 6
            with open(up, "w", encoding="utf-8") as fh:
                fh.writelines(seeded)
            lock_path = os.path.join(d, ".usage.lock")
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
            amb.fcntl.flock(fd, amb.fcntl.LOCK_EX)
            try:
                with patched(amb, USAGE_PATH=up, USAGE_MAX_BYTES=10,
                             USAGE_TRIM_KEEP_LINES=2,
                             _LEDGER_LOCK_WAIT_S=0.2):
                    amb.log_usage("m", {"prompt_tokens": 1,
                                        "completion_tokens": 1})
            finally:
                amb.fcntl.flock(fd, amb.fcntl.LOCK_UN)
                os.close(fd)
            with open(up, encoding="utf-8") as fh:
                lines = fh.readlines()
            self.assertEqual(len(lines), 6)  # main untouched, NO trim
            spool = f"{up}.spool.{os.getpid()}"
            self.assertTrue(os.path.exists(spool))  # line spooled, not lost
            with patched(amb, USAGE_PATH=up, USAGE_MAX_BYTES=10,
                         USAGE_TRIM_KEEP_LINES=2, _LEDGER_LOCK_WAIT_S=0.2):
                amb.log_usage("m", {"prompt_tokens": 1,
                                    "completion_tokens": 1})
            self.assertFalse(os.path.exists(spool))  # merged back
            with open(up, encoding="utf-8") as fh:
                lines = fh.readlines()
            # 6 old + 1 merged spool line, trimmed to 2 + 1 appended
            self.assertEqual(len(lines), 3)

    def test_trim_keeps_the_newest_lines(self):
        with fleet_dir() as d:
            up = os.path.join(d, "usage.jsonl")
            with patched(amb, USAGE_PATH=up, USAGE_MAX_BYTES=10,
                         USAGE_TRIM_KEEP_LINES=2):
                for i in range(5):
                    amb.log_usage("m", {"prompt_tokens": i,
                                        "completion_tokens": 0})
            with open(up, encoding="utf-8") as fh:
                rows = [json.loads(x) for x in fh]
            self.assertLessEqual(len(rows), 3)
            self.assertEqual(rows[-1]["in"], 4)  # newest survives


class TestConfigKnobs(unittest.TestCase):
    def test_fleet_enabled_defaults_on_and_parses_off(self):
        with env_var("AMBIENT_FLEET_BUDGET", None):
            self.assertTrue(amb._fleet_enabled({}))
            self.assertFalse(amb._fleet_enabled({"AMBIENT_FLEET_BUDGET": "off"}))
        for off in ("off", "0", "false", "no", "OFF"):
            with env_var("AMBIENT_FLEET_BUDGET", off):
                self.assertFalse(amb._fleet_enabled({}))
        with env_var("AMBIENT_FLEET_BUDGET", "on"):
            self.assertTrue(amb._fleet_enabled({}))

    def test_reservation_ttl_default_and_override(self):
        # default is generous (1h): on Windows liveness is unknowable, so a
        # long single job that never re-gates must rarely hit the TTL
        with env_var("AMBIENT_RESERVATION_TTL", None):
            self.assertEqual(amb._reservation_ttl({}), 3600.0)
            self.assertEqual(amb._reservation_ttl(
                {"AMBIENT_RESERVATION_TTL": "120"}), 120.0)
        with env_var("AMBIENT_RESERVATION_TTL", "60"):
            self.assertEqual(amb._reservation_ttl({}), 60.0)
        with env_var("AMBIENT_RESERVATION_TTL", "junk"):
            self.assertEqual(amb._reservation_ttl({}), 3600.0)
        with env_var("AMBIENT_RESERVATION_TTL", "-5"):
            self.assertEqual(amb._reservation_ttl({}), 3600.0)

    def test_docs_mention_new_knobs(self):
        for rel in (("README.md",), ("skills", "ambient", "SKILL.md")):
            with open(os.path.join(ROOT, *rel), encoding="utf-8") as fh:
                doc = fh.read()
            self.assertIn("AMBIENT_FLEET_BUDGET", doc)
            self.assertIn("AMBIENT_RESERVATION_TTL", doc)


if __name__ == "__main__":
    unittest.main()
