"""Regression tests for the v1.0.1 backlog remediation (best-of-3 audit
MED/LOW findings, each independently verified). Phases appended as they land.

No network, no live API. Run: python3 -m pytest tests/test_v101_backlog_fixes.py
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v101", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v101", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = load_module()
KEY = "sk-abcdef1234567890XY"   # 20 chars, key-shaped


# ------------------------------------------------- Phase 1: security/redaction

class TestM16RedactOrder(unittest.TestCase):
    """M16: sanitize BEFORE key-replace so an escape-split key reassembles and
    is then redacted (raw-substring-first would miss it)."""

    def test_escape_split_key_is_redacted(self):
        poisoned = KEY[:6] + "\x1b[0m" + KEY[6:]   # ESC sequence inside the key
        out = amb.redact(poisoned, KEY)
        self.assertNotIn(KEY, out)
        self.assertIn("[AMBIENT_API_KEY]", out)

    def test_c1_split_key_is_redacted(self):
        poisoned = KEY[:8] + "\x9b" + KEY[8:]       # C1 CSI splitter
        self.assertNotIn(KEY, amb.redact(poisoned, KEY))

    def test_plain_text_unchanged(self):
        self.assertEqual(amb.redact("the quick brown fox", KEY),
                         "the quick brown fox")


class TestM43StreamRedactor(unittest.TestCase):
    """M43: a key split across streaming chunk boundaries must never reach the
    terminal — the stateful redactor holds a rolling tail and redacts complete
    keys anywhere in the buffer."""

    def _run(self, pieces):
        sr = amb._StreamRedactor(KEY)
        return "".join(sr.feed(p) for p in pieces) + sr.flush()

    def test_no_boundary_split_leaks_the_key(self):
        full = "hello " + KEY + " world"
        for cut in range(1, len(full)):
            out = self._run([full[:cut], full[cut:]])
            self.assertNotIn(KEY, out, f"leaked at cut={cut}")

    def test_three_way_split_through_key(self):
        out = self._run(["x" + KEY[:4], KEY[4:12], KEY[12:] + "y"])
        self.assertNotIn(KEY, out)

    def test_escape_split_across_feeds(self):
        out = self._run([KEY[:6], "\x1b[0m", KEY[6:]])
        self.assertNotIn(KEY, out)

    def test_normal_stream_passes_through_intact(self):
        self.assertEqual(self._run(["the quick ", "brown fox"]),
                         "the quick brown fox")

    def test_no_key_still_sanitizes(self):
        sr = amb._StreamRedactor("")
        out = "".join(sr.feed(p) for p in ["a\x1b[31m", "b\x9dc"]) + sr.flush()
        self.assertEqual(out, "abc")

    def test_streaming_is_bounded_not_quadratic(self):
        # A long streamed answer, and a hostile never-terminating escape, must
        # both stay ~linear (the redactor compacts its buffers). Generous 5s
        # ceiling: the old O(n^2) took many seconds at these sizes; a healthy
        # linear pass is well under a second, so this can't flake.
        import time
        t = time.perf_counter()
        sr = amb._StreamRedactor(KEY)
        for _ in range(30000):
            sr.feed("token ")
        sr.flush()
        self.assertLess(time.perf_counter() - t, 5.0, "plain stream not linear")
        t = time.perf_counter()
        sr = amb._StreamRedactor(KEY)
        sr.feed("\x1b[")
        for _ in range(30000):            # never-terminating CSI params
            sr.feed("0")
        sr.flush()
        self.assertLess(time.perf_counter() - t, 5.0, "escape hold not bounded")

    def test_over_cap_unterminated_escape_still_redacts_split_key(self):
        # even when a giant unterminated escape forces the hold-cap, a key split
        # across the following pieces must still be redacted.
        pieces = ["\x1b["] + ["9"] * 5000 + [KEY[:6], KEY[6:], " tail"]
        sr = amb._StreamRedactor(KEY)
        got = "".join(sr.feed(p) for p in pieces) + sr.flush()
        self.assertNotIn(KEY, got)

    def test_over_cap_escape_stays_consistent_with_redact_of_whole(self):
        # A >cap escape gets its middle compacted, but the escape stays "open"
        # so it is removed exactly as redact(full) does — no leaked middle text.
        cases = [
            "\x1b]" + "x" * 3000 + "\x07" + KEY + " tail",   # terminated OSC
            "pre \x1b]" + "y" * 3000 + KEY + "z",             # unterminated OSC
            "\x1bP" + "d" * 3000 + "\x1b\\" + KEY + " q",     # terminated DCS
        ]
        for full in cases:
            sr = amb._StreamRedactor(KEY)
            streamed = "".join(sr.feed(c) for c in full) + sr.flush()
            self.assertEqual(streamed, amb.redact(full, KEY), repr(full[:12]))
            self.assertNotIn(KEY, streamed)

    def test_streamed_equals_redact_of_whole_at_every_split(self):
        # The strong invariant: however the provider chunks the bytes, the
        # streamed output is byte-identical to redacting the full text at once
        # (covers escape-in-key splits — Codex's HIGH repro).
        raws = ["hi " + KEY[:5] + "\x1b[0m" + KEY[5:] + " bye",
                "x" + KEY + "y", "a" + KEY + "b" + KEY + "c",
                "plain \x1b[31mred\x1b[0m no key"]
        for raw in raws:
            for i in range(1, len(raw)):
                self.assertEqual(self._run([raw[:i], raw[i:]]),
                                 amb.redact(raw, KEY), f"2-way i={i} raw={raw!r}")
                for j in range(i + 1, len(raw)):
                    self.assertEqual(
                        self._run([raw[:i], raw[i:j], raw[j:]]),
                        amb.redact(raw, KEY), f"3-way {i},{j} raw={raw!r}")


# -------------------------------------------------- Phase 2: crash-hardening

class TestPhase2Hardening(unittest.TestCase):
    def test_M30_as_bool_rejects_false_string(self):
        self.assertFalse(amb._as_bool("false"))
        self.assertFalse(amb._as_bool("0"))
        self.assertFalse(amb._as_bool(""))
        self.assertTrue(amb._as_bool("true"))
        self.assertTrue(amb._as_bool(True))
        self.assertFalse(amb._as_bool(False))

    def test_M30_ready_model_ids_skips_false_string(self):
        cat = [{"id": "a", "is_ready": "false"}, {"id": "b", "is_ready": True},
               {"id": "c", "is_ready": "true"}]
        self.assertEqual(amb.ready_model_ids(cat), ["b", "c"])

    def test_M29_fetch_models_tolerates_non_dict_body(self):
        # body is a list/str/None instead of an object → [] not a crash
        for body in ([], "oops", None, 42):
            with _patch(amb, "api_request", lambda *a, **k: (200, body)):
                self.assertEqual(amb.fetch_models("https://x", "k"), [])

    def test_L12_run_map_reduce_empty_chunks(self):
        self.assertEqual(
            amb.run_map_reduce("k", "u", "m", "sys", [], None, "syn", 1000),
            ("", False, "no input"))


@contextlib.contextmanager
def _patch(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


# ------------------------------------------------ Phase 3: input/path edges

class TestPhase3InputPath(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX filesystem-root '/' semantics")
    def test_M9_within_root_correct_at_filesystem_root(self):
        # The bug this fixes is specific to the POSIX root '/', where
        # startswith(root + os.sep) == startswith('//') wrongly rejected subdirs.
        self.assertTrue(amb._within_root("/foo/bar", "/"))    # subdir of / allowed
        self.assertTrue(amb._within_root("/foo", "/"))         # top-level under /

    def test_M9_within_root_containment_cross_platform(self):
        # OS-native absolute paths (real temp dir) so this holds on Windows too.
        import os as _os
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            root = _os.path.realpath(root)
            self.assertTrue(amb._within_root(root, root))                    # equal
            self.assertTrue(amb._within_root(
                _os.path.join(root, "src", "a.py"), root))                  # subdir
            parent = _os.path.dirname(root)
            self.assertFalse(amb._within_root(parent, root))                # escape
            self.assertFalse(amb._within_root(root + "_sibling", root))     # prefix

    def test_M27_negative_older_than_refused(self):
        import io
        import sys
        args = argparse.Namespace(action="clear", older_than=-1)
        err = io.StringIO()
        with _patch(sys, "stderr", err), _patch(sys, "argv", ["ambient"]):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_cache(args)
        self.assertEqual(cm.exception.code, amb.EXIT_USAGE)
        self.assertIn("non-negative", err.getvalue())

    def test_L18_negative_days_refused(self):
        import io
        import sys
        args = argparse.Namespace(days=-5, json=False)
        err = io.StringIO()
        with _patch(sys, "stderr", err), _patch(sys, "argv", ["ambient"]):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_usage(args)   # fires before any ledger read now
        self.assertEqual(cm.exception.code, amb.EXIT_USAGE)
        self.assertIn("positive", err.getvalue())


def _mk_usage_ledger(body):
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "usage.jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)
    return p


# ------------------------------------------------------- Phase 4: build lane

class TestPhase4Build(unittest.TestCase):
    def _write_state(self, root, st):
        with open(amb._build_state_path(root), "w", encoding="utf-8") as fh:
            fh.write(__import__("json").dumps(st))

    def _base_state(self, **over):
        st = {"version": 1, "task_sha": "SHA", "plan": [{"path": "a.py"}],
              "done": {}, "failed": []}
        st.update(over)
        return st

    def test_M22_poisoned_failed_entries_dropped_not_crash(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            self._write_state(root, self._base_state(
                failed=[None, "x", {"path": "ok.py", "reason": "boom"},
                        {"path": 5, "reason": "y"}]))
            st = amb._load_build_state(root, "SHA")
            self.assertIsNotNone(st)
            # only the well-formed dict survives
            self.assertEqual(st["failed"], [{"path": "ok.py", "reason": "boom"}])

    def test_M10_max_plan_honors_caller_bound(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            big = [{"path": f"f{i}.py"} for i in range(700)]
            self._write_state(root, self._base_state(plan=big))
            # default 512 truncates...
            self.assertEqual(len(amb._load_build_state(root, "SHA")["plan"]), 512)
            # ...but a larger caller bound keeps the whole plan
            self.assertEqual(
                len(amb._load_build_state(root, "SHA", max_plan=1000)["plan"]),
                700)

    def test_M24_stdin_is_tty_survives_closed_stdin(self):
        import io
        import sys
        with _patch(sys, "stdin", io.StringIO()):  # StringIO.isatty()==False, ok
            self.assertFalse(amb._stdin_is_tty())

    def test_cmd_build_rejects_nonpositive_caps(self):
        # whole-branch audit: a negative --max-files is a negative slice that
        # keeps all-but-last — reject up front, before resume/catalog/spend.
        import io
        import sys
        for bad in ({"max_files": -1, "max_file_bytes": 1000},
                    {"max_files": 5, "max_file_bytes": 0}):
            args = argparse.Namespace(task=["build a thing"], dir="out",
                                      allow_secrets=False, apply=False, **bad)
            with _patch(sys, "stderr", io.StringIO()), \
                    _patch(sys, "argv", ["ambient"]):
                with self.assertRaises(SystemExit) as cm:
                    amb.cmd_build(args, "k", "https://x", {})
            self.assertEqual(cm.exception.code, amb.EXIT_USAGE)


# ------------------------------------------------ Phase 5: audit/reporting

class TestPhase5AuditDedupe(unittest.TestCase):
    def test_M36_escalating_severity_keeps_richer_scenario(self):
        rich = ("A very detailed long scenario describing the exact failure "
                "path in depth with inputs and the resulting crash")
        prev = {"file": "a.py", "line": 10, "title": "SQL injection",
                "severity": "MEDIUM", "scenario": rich}
        f = {"file": "a.py", "line": 11, "title": "SQL injection",
             "severity": "HIGH", "scenario": "short"}
        out = amb.dedupe_findings([prev, f])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "HIGH")     # highest severity
        self.assertEqual(out[0]["scenario"], rich)        # richest scenario kept

    def test_L23_case_distinct_paths_not_merged(self):
        out = amb.dedupe_findings([
            {"file": "Foo.py", "line": 1, "title": "bug x", "severity": "LOW",
             "scenario": "s"},
            {"file": "foo.py", "line": 1, "title": "bug x", "severity": "LOW",
             "scenario": "s"}])
        self.assertEqual(len(out), 2)   # distinct files on a case-sensitive FS

    def test_L13_user_brace_i_not_corrupted(self):
        note = amb._map_note("User prompt with {i} literal", "", 5)
        rendered = note.replace(amb._CHUNK_IDX_TOKEN, "3")
        self.assertIn("{i} literal", rendered)     # user's {i} survives
        self.assertIn("chunk 3 of 5", rendered)     # ambient index substituted


# ------------------------------------------------- Phase 6: reliability/misc

class TestPhase6Misc(unittest.TestCase):
    def test_M32_trust_url_prompts_go_to_stderr_not_stdout(self):
        import io
        import sys
        args = argparse.Namespace(url="https://gw.example.com")
        out, err = io.StringIO(), io.StringIO()

        class _TTY(io.StringIO):
            def isatty(self):
                return True
        with _patch(sys, "stdout", out), _patch(sys, "stderr", err), \
                _patch(sys, "stdin", _TTY("nope\n")):
            with self.assertRaises(SystemExit):   # hostname mismatch → no save
                amb.cmd_trust_url(args)
        # the key-exfil warning + prompt must NOT pollute stdout
        self.assertEqual(out.getvalue(), "")
        self.assertIn("Authorization header", err.getvalue())

    def test_catalog_data_normalizes_malformed_bodies(self):
        # whole-branch audit: a malformed /v1/models 200 must not crash a
        # downstream models[0]["id"] / `for m in models`.
        for body in (1, "oops", [], None, {"data": "oops"},
                     {"data": ["bad", 1, None]}, {"nope": 1}):
            out = amb._catalog_data(body)
            self.assertIsInstance(out, list)
            self.assertTrue(all(isinstance(x, dict) for x in out))
        # keeps only dict rows with a non-empty string id (matches fetch_models)
        self.assertEqual(
            amb._catalog_data({"data": [{"id": "x"}, "bad", {"y": 1},
                                        {"id": 5}, {"id": ""}]}),
            [{"id": "x"}])

    def test_M14_shim_is_ours_only_matches_our_template(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ours = os.path.join(d, "a.cmd")
            with open(ours, "w", encoding="utf-8") as fh:
                fh.write('@python "/x/ambient-code/1.0/bin/ambient" %*\r\n')
            self.assertTrue(amb._shim_is_ours(ours))
            foreign = os.path.join(d, "b.cmd")
            with open(foreign, "w", encoding="utf-8") as fh:
                fh.write('@echo off\r\ncall other-tool %*\r\n')
            self.assertFalse(amb._shim_is_ours(foreign))
            # our template but a NON-ambient target → not ours
            other = os.path.join(d, "c.cmd")
            with open(other, "w", encoding="utf-8") as fh:
                fh.write('@python "/x/some-tool/bin/run" %*\r\n')
            self.assertFalse(amb._shim_is_ours(other))


if __name__ == "__main__":
    unittest.main()
