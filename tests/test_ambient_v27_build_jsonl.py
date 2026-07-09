"""V27 — build-lane record-framing (JSONL). Replaces heuristic byte-level JSON
continuation-stitch with the pattern the real Ambient Desktop product uses: the
generation reply is a STREAM of per-file JSON objects, so a truncated reply only
ever costs its last incomplete record — every complete file before it is
recovered clean, and the missing files requeue (the existing checkpoint loop) as
the continuation. No byte-stitch, no overlap detection, no partial-JSON repair.

Guarantees under test:
  1. _parse_file_records recovers complete objects and DROPS a truncated tail.
  2. It tolerates JSONL, concatenated objects, multi-line objects, prose/fences.
  3. It unwraps a legacy {"files":[...]} wrapper (fallback for non-compliant models).
  4. End-to-end (apply=True, byte-exact on disk): multi-file build; truncation
     mid-file keeps complete files and requeues the cut one; wrapper fallback;
     a single oversized file that always truncates fails SAFE (no corruption).
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "bin", "ambient")


def _load():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_jsonl", _BIN)
    mod = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("ambient_cli_jsonl", loader))
    loader.exec_module(mod)
    return mod


amb = _load()
KEY = "sk-test-key-abcdef1234567890"


@contextlib.contextmanager
def _patched(**attrs):
    old, missing = {}, object()
    for k, v in attrs.items():
        old[k] = getattr(amb, k, missing)
        setattr(amb, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            (delattr if v is missing else setattr)(amb, k, *(() if v is missing else (v,)))


_CAT = [{"id": "reasoner/model", "context_length": 200000,
         "max_output_length": 200000, "is_ready": True,
         "supported_features": ["reasoning", "structured_outputs"],
         "output_modalities": ["text"], "pricing": {"input": 1.0, "output": 4.0}}]


def _args(root, **kw):
    base = dict(task=["make", "a", "thing"], dir=root, context=None,
                apply=True, force=False, plan_only=False, dry_run=False,
                max_files=32, max_file_bytes=200_000, no_resume=True,
                json=True, allow_secrets=False, model="reasoner/model",
                max_tokens=None, temperature=0.1, timeout=30, raw=False,
                fallback=False, allow_partial=True, allow_cost=True, yes=True,
                no_cache=True, cache_ttl=None, parallel=None, system=None,
                response_format=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _run(root, fake):
    buf = io.StringIO()
    with _patched(complete=fake, safe_catalog=lambda *a, **k: _CAT,
                  cost_gate=lambda *a, **k: None, cost_gate_mr=lambda *a, **k: None,
                  _gate_amount=lambda *a, **k: None,
                  warn_if_stdin_ignored=lambda *a, **k: None):
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                amb.cmd_build(_args(root), KEY, "https://x", {})
            except SystemExit:
                pass
    try:
        env = json.loads(buf.getvalue())
    except json.JSONDecodeError:
        env = {"_raw": buf.getvalue()}
    files = {}
    for base, _d, names in os.walk(root):
        for name in names:
            if name.startswith("."):
                continue
            p = os.path.join(base, name)
            with open(p, encoding="utf-8", newline="") as fh:
                files[os.path.relpath(p, root)] = fh.read()
    return env, files


def _plan(*paths):
    return json.dumps({"plan": [{"path": p, "purpose": "p", "est_lines": 3}
                                for p in paths], "notes": "n",
                       "advisory_steps": []})


def _rec(path, content):
    return json.dumps({"path": path, "content": content})


def _trunc_then_good(plan_reply, trunc_reply, good_reply):
    """plan on call 1, a truncated (finish_reason=length) gen on call 2, then the
    SAME complete good_reply on every subsequent call — so however the requeue
    loop splits the batch, each retry can serve the file(s) it asks for."""
    st = {"calls": 0}

    def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
        st["calls"] += 1
        if st["calls"] == 1:
            return plan_reply, {}, {"finish_reason": "stop"}
        if st["calls"] == 2:
            return trunc_reply, {}, {"finish_reason": "length"}
        return good_reply, {}, {"finish_reason": "stop"}

    return fake


def _staged(plan_reply, gen_replies):
    """plan on call 1, then pop (text, finish_reason) gen replies."""
    st = {"calls": 0}
    reps = list(gen_replies)

    def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
        st["calls"] += 1
        if st["calls"] == 1:
            return plan_reply, {}, {"finish_reason": "stop"}
        txt, fr = reps.pop(0) if reps else ("", "stop")
        return txt, {}, {"finish_reason": fr}

    return fake, st


class TestParseFileRecords(unittest.TestCase):
    def _paths(self, txt):
        return [r.get("path") for r in amb._parse_file_records(txt)]

    def test_jsonl_newline_delimited(self):
        txt = _rec("a.py", "A\n") + "\n" + _rec("b.py", "B\n")
        recs = amb._parse_file_records(txt)
        self.assertEqual([r["path"] for r in recs], ["a.py", "b.py"])
        self.assertEqual(recs[1]["content"], "B\n")

    def test_concatenated_no_newlines(self):
        self.assertEqual(self._paths(_rec("a.py", "A") + _rec("b.py", "B")),
                         ["a.py", "b.py"])

    def test_object_with_literal_newlines_between(self):
        txt = _rec("a.py", "A") + "\n\n\n" + _rec("b.py", "B")
        self.assertEqual(self._paths(txt), ["a.py", "b.py"])

    def test_truncated_tail_is_dropped(self):
        txt = _rec("a.py", "A\n") + "\n" + '{"path":"b.py","content":"BB'
        self.assertEqual(self._paths(txt), ["a.py"])            # b dropped

    def test_wrapper_is_unwrapped(self):
        txt = json.dumps({"files": [{"path": "a.py", "content": "A"},
                                    {"path": "b.py", "content": "B"}]})
        self.assertEqual(self._paths(txt), ["a.py", "b.py"])

    def test_leading_prose_and_fence_tolerated(self):
        txt = "Here are the files:\n```json\n" + _rec("a.py", "A") + "\n```"
        self.assertEqual(self._paths(txt), ["a.py"])

    def test_truncated_wrapper_yields_nothing(self):
        # a cut wrapper won't decode; we STOP rather than dig for inner records,
        # so it yields nothing (the files requeue) — never a mis-salvaged record.
        txt = '{"files":[{"path":"a.py","content":"A"},{"path":"b.py","content":"B'
        self.assertEqual(self._paths(txt), [])

    def test_malformed_object_is_not_dug_into(self):
        # a malformed outer object with embedded per-file objects must NOT be
        # mined for fake top-level records (round-5 finding): STOP at the failure.
        txt = ('{"x": {"path":"b.py","content":"BAD_B"}, '
               '{"path":"c.py","content":"CC"}}')
        self.assertEqual(self._paths(txt), [])

    def test_clean_records_before_malformed_line_start_object_are_kept(self):
        # a line-start '{' that is not valid JSON ('{junk') is an open object; the
        # depth scanner will not mis-salvage a following nested-looking object —
        # it yields nothing from that point (files requeue). Records BEFORE it stay.
        txt = _rec("a.py", "A") + "\n{junk\n" + _rec("b.py", "B")
        self.assertEqual(self._paths(txt), ["a.py"])

    def test_wrapper_with_extra_array_unwraps_only_files(self):
        # round-7 repro: a whole object carrying BOTH an "examples" array and a
        # "files" array must decode as ONE top-level object and unwrap ONLY files
        # — never mis-read the nested example as a record.
        txt = json.dumps({"examples": [{"path": "a.py", "content": "BAD"}],
                          "files": [{"path": "a.py", "content": "REAL"}]}, indent=2)
        self.assertEqual([r["content"] for r in amb._parse_file_records(txt)],
                         ["REAL"])

    def test_record_wins_over_files_key(self):
        # round-8 repro: an object with BOTH path/content AND a `files` key is a
        # per-file record — it must NOT be replaced by its nested `files` content.
        txt = ('{"path":"a.py","content":"REAL","files":'
               '[{"path":"a.py","content":"BAD"}]}')
        recs = amb._parse_file_records(txt)
        self.assertEqual([(r.get("path"), r.get("content")) for r in recs
                          if r.get("content") in ("REAL", "BAD")], [("a.py", "REAL")])

    def test_balanced_invalid_prefix_blocks_same_line_concatenation(self):
        # round-8 repro: a balanced-but-INVALID object followed by a real object
        # on the SAME line (prose) must yield NO record (only a valid record may
        # be followed by a same-line concatenated object).
        txt = '{note: example} ' + _rec("a.py", "PLACEHOLDER")
        self.assertEqual(self._paths(txt), [])

    def test_record_with_nested_array_object_is_not_dropped(self):
        # round-7 repro: a valid record whose value is an array-of-objects must be
        # kept (the old fold heuristic dropped it when a nested line began '{').
        txt = json.dumps({"meta": [{"note": "x"}], "path": "a.py",
                          "content": "A"}, indent=2)
        recs = amb._parse_file_records(txt)
        self.assertEqual([(r.get("path"), r.get("content")) for r in recs],
                         [("a.py", "A")])

    def test_object_embedded_in_prose_line_is_ignored(self):
        # round-6 repro: a JSON object that does NOT begin its line (an example
        # inside prose) must NOT be read as a record; the real record on its own
        # line is the only one returned.
        txt = ('Note/example: ' + _rec("a.py", "PLACEHOLDER\n") + "\n"
               + "Actual:\n" + _rec("a.py", "REAL\n"))
        recs = amb._parse_file_records(txt)
        self.assertEqual([r["path"] for r in recs], ["a.py"])
        self.assertEqual(recs[0]["content"], "REAL\n")

    def test_array_wrapped_record_line_is_ignored(self):
        # a line starting with '[' is not a record line (no digging into arrays).
        self.assertEqual(self._paths('[' + _rec("a.py", "A") + ']'), [])

    def test_two_objects_on_one_line(self):
        self.assertEqual(self._paths(_rec("a.py", "A") + " " + _rec("b.py", "B")),
                         ["a.py", "b.py"])

    def test_trailing_bytes_after_valid_record_are_ignored(self):
        # raw_decode ignores bytes after a complete object, so a valid record
        # followed by junk on the same line still yields the record.
        self.assertEqual(self._paths(_rec("a.py", "A") + "  <<garbage"), ["a.py"])

    def test_duplicate_path_in_reply_recovers_both_records(self):
        txt = _rec("a.py", "FIRST") + "\n" + _rec("a.py", "SECOND")
        self.assertEqual(self._paths(txt), ["a.py", "a.py"])

    def test_empty_and_garbage(self):
        self.assertEqual(amb._parse_file_records(""), [])
        self.assertEqual(amb._parse_file_records("not json at all"), [])

    def test_pretty_printed_multiline_object_is_parsed(self):
        pretty = ('{\n  "path": "a.py",\n  "content": "print(\'a\')\\n"\n}\n'
                  '{\n  "path": "b.py",\n  "content": "print(\'b\')\\n"\n}')
        recs = amb._parse_file_records(pretty)
        self.assertEqual([r["path"] for r in recs], ["a.py", "b.py"])
        self.assertEqual(recs[0]["content"], "print('a')\n")

    def test_pretty_printed_wrapper_is_parsed(self):
        pretty = json.dumps({"files": [{"path": "a.py", "content": "A"},
                                       {"path": "b.py", "content": "B"}]}, indent=2)
        self.assertEqual(self._paths(pretty), ["a.py", "b.py"])

    def test_unicode_line_sep_in_content_is_preserved(self):
        # a LITERAL U+2028 inside content is valid JSON; splitting on '\n' only
        # keeps the record on one line (str.splitlines would split it and corrupt).
        raw = '{"path":"a.py","content":"x\u2028y"}'
        self.assertEqual(amb._parse_file_records(raw),
                         [{"path": "a.py", "content": "x\u2028y"}])

    def test_prose_with_unicode_sep_before_object_still_ignored(self):
        # a LITERAL U+2028 immediately before an example object: str.splitlines
        # would make it "begin a line" and mis-accept it; split('\n') keeps it
        # embedded in the prose line, so only the REAL record (own line) is kept.
        txt = 'note example\u2028' + _rec("a.py", "PLACEHOLDER") + "\n" + _rec("a.py", "REAL")
        self.assertEqual([r["content"] for r in amb._parse_file_records(txt)], ["REAL"])

    def test_adversarial_nested_braces_does_not_crash_or_hang(self):
        # deep nesting makes json.raw_decode RecursionError; many failing '{'
        # starts could go O(n^2). The fail cap + RecursionError catch must make
        # this return promptly with no exception.
        for depth in (20000, 200000):
            self.assertEqual(amb._parse_file_records('{"a":' * depth), [],
                             f"depth={depth}")


class TestJsonlBuild(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self._old = amb.CAPABILITY_PATH
        amb.CAPABILITY_PATH = os.path.join(tmp, "caps.json")
        self.addCleanup(setattr, amb, "CAPABILITY_PATH", self._old)
        amb._CAP_CACHE = None
        self.addCleanup(setattr, amb, "_CAP_CACHE", None)

    def test_multifile_jsonl_build_byte_exact(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        gen = _rec("a.py", "print('a')\n") + "\n" + _rec("b.py", "print('b')\n")
        fake, _st = _staged(_plan("a.py", "b.py"), [(gen, "stop")])
        env, files = _run(d, fake)
        self.assertEqual(env.get("failed"), [], env)
        self.assertEqual(files.get("a.py"), "print('a')\n", files)
        self.assertEqual(files.get("b.py"), "print('b')\n", files)

    def test_truncation_requeues_and_completes(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # gen1: a complete, b cut. The conservative guard also drops the last
        # ACCEPTED file, so the truncated batch requeues (splitting into per-file
        # retries); every retry serves complete records. Never a partial; always
        # eventually correct.
        trunc = _rec("a.py", "AAA\n") + "\n" + '{"path":"b.py","content":"BB'
        good = _rec("a.py", "AAA\n") + "\n" + _rec("b.py", "BBB\n")
        fake = _trunc_then_good(_plan("a.py", "b.py"), trunc, good)
        env, files = _run(d, fake)
        self.assertEqual(env.get("failed"), [], env)
        self.assertEqual(files.get("a.py"), "AAA\n", files)
        self.assertEqual(files.get("b.py"), "BBB\n", files)

    def test_wrapper_fallback_from_noncompliant_model(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        gen = json.dumps({"files": [{"path": "a.py", "content": "A\n"}]})
        fake, _st = _staged(_plan("a.py"), [(gen, "stop")])
        env, files = _run(d, fake)
        self.assertEqual(env.get("failed"), [], env)
        self.assertEqual(files.get("a.py"), "A\n", files)

    def test_complete_records_commit_under_length_stop(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # both records decoded as COMPLETE JSON objects; a length stop just means
        # the model was cut BEFORE the next file. Complete records are complete
        # files (the model emitted the closing brace) — commit both, don't drop a
        # deliverable file (dropping would false-fail a budget-filling single file).
        gen = _rec("a.py", "AAA\n") + "\n" + _rec("b.py", "BBB\n")
        fake, st = _staged(_plan("a.py", "b.py"), [(gen, "length")])
        env, files = _run(d, fake)
        self.assertEqual(env.get("failed"), [], env)
        self.assertEqual(files.get("a.py"), "AAA\n", files)
        self.assertEqual(files.get("b.py"), "BBB\n", files)
        self.assertEqual(st["calls"], 2)                          # no needless requeue

    def test_single_complete_file_filling_budget_is_not_dropped(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # a single complete file whose reply ends on finish_reason=length (it
        # filled the budget) must COMMIT — the old drop-on-non-clean guard would
        # requeue it at the same budget forever and false-fail a deliverable file.
        fake, st = _staged(_plan("big.py"), [(_rec("big.py", "X" * 5000), "length")])
        env, files = _run(d, fake)
        self.assertEqual(env.get("failed"), [], env)
        self.assertEqual(files.get("big.py"), "X" * 5000, files)
        self.assertEqual(st["calls"], 2)

    def test_requeue_restart_does_not_overwrite_committed_file(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # gen1: a, b complete + c cut. a and b (complete records) COMMIT; only c
        # (cut by the parser) requeues. gen2 RESTARTS, re-emitting a AND b with
        # WRONG content — both are already done and not in this call's want-set,
        # so the restart is IGNORED. Committed files are never overwritten.
        gen1 = (_rec("a.py", "GOOD_A\n") + "\n" + _rec("b.py", "GOOD_B\n") + "\n"
                + '{"path":"c.py","content":"CC')
        restart = (_rec("a.py", "BAD_A\n") + "\n" + _rec("b.py", "BAD_B\n") + "\n"
                   + _rec("c.py", "GOOD_C\n"))
        fake = _trunc_then_good(_plan("a.py", "b.py", "c.py"), gen1, restart)
        env, files = _run(d, fake)
        self.assertEqual(files.get("a.py"), "GOOD_A\n", files)   # committed, NOT overwritten
        self.assertEqual(files.get("b.py"), "GOOD_B\n", files)   # committed, NOT overwritten
        self.assertEqual(files.get("c.py"), "GOOD_C\n", files)   # cut → requeued

    def test_within_reply_restart_first_content_wins(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # model emits a.py correct, then RESTARTS and re-emits a.py wrong in the
        # same reply. First-wins must keep the correct (in-order) content.
        gen = _rec("a.py", "CORRECT\n") + "\n" + _rec("a.py", "WRONG\n")
        fake, _st = _staged(_plan("a.py"), [(gen, "stop")])
        env, files = _run(d, fake)
        self.assertEqual(files.get("a.py"), "CORRECT\n", files)

    def test_content_filter_stop_commits_complete_records(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # content_filter (or any non-salvaged finish): complete records are still
        # complete files → commit both. No drop.
        gen = _rec("a.py", "AAA\n") + "\n" + _rec("b.py", "BBB\n")
        fake, st = _staged(_plan("a.py", "b.py"), [(gen, "content_filter")])
        env, files = _run(d, fake)
        self.assertEqual(env.get("failed"), [], env)
        self.assertEqual(files.get("a.py"), "AAA\n", files)
        self.assertEqual(files.get("b.py"), "BBB\n", files)
        self.assertEqual(st["calls"], 2)

    def test_plan_phase_rejects_reasoning_draft_plan(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # a reasoning-draft plan reply (reasoning_draft=True) containing a
        # {"plan":[...]} must NOT become the build contract — the plan ladder
        # retries and the REAL plan (a.py) wins; evil.py is never built.
        state = {"calls": 0}

        def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
            state["calls"] += 1
            if state["calls"] == 1:                      # plan attempt 1: reasoning
                return ('[AMBIENT NOTE: reasoning]\n\n{"plan":[{"path":"evil.py",'
                        '"purpose":"x","est_lines":1}],"notes":"n"}',
                        {}, {"salvaged_partial": True, "reasoning_draft": True})
            if state["calls"] == 2:                      # plan attempt 2: real plan
                return _plan("a.py"), {}, {"finish_reason": "stop"}
            return _rec("a.py", "A\n"), {}, {"finish_reason": "stop"}

        env, files = _run(d, fake)
        self.assertIn("a.py", files, files)
        self.assertNotIn("evil.py", files, files)

    def test_plan_phase_rejects_length_truncated_plan(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # a plan reply that PARSES but ended on finish_reason=length may be a
        # truncated (incomplete) contract; the plan has no per-file continuation,
        # so reject it and retry. The clean plan (a.py,b.py) wins; partial.py is
        # never built.
        state = {"calls": 0}

        def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
            state["calls"] += 1
            if state["calls"] == 1:                      # parses but length-cut
                return (json.dumps({"plan": [{"path": "partial.py", "purpose": "x",
                                              "est_lines": 1}], "notes": "n"}),
                        {}, {"finish_reason": "length"})
            if state["calls"] == 2:                      # clean plan
                return _plan("a.py", "b.py"), {}, {"finish_reason": "stop"}
            return (_rec("a.py", "A\n") + "\n" + _rec("b.py", "B\n"),
                    {}, {"finish_reason": "stop"})

        env, files = _run(d, fake)
        self.assertIn("a.py", files, files)
        self.assertIn("b.py", files, files)
        self.assertNotIn("partial.py", files, files)

    def test_plan_phase_rejects_content_filter_plan(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # allow-list: a plan that PARSES but ended on content_filter (not a clean
        # stop) is rejected — no non-stop finish becomes the contract. Retry wins.
        state = {"calls": 0}

        def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
            state["calls"] += 1
            if state["calls"] == 1:
                return (json.dumps({"plan": [{"path": "blocked.py", "purpose": "x",
                                              "est_lines": 1}], "notes": "n"}),
                        {}, {"finish_reason": "content_filter"})
            if state["calls"] == 2:
                return _plan("a.py"), {}, {"finish_reason": "stop"}
            return _rec("a.py", "A\n"), {}, {"finish_reason": "stop"}

        env, files = _run(d, fake)
        self.assertIn("a.py", files, files)
        self.assertNotIn("blocked.py", files, files)

    def test_apply_overwrites_invalid_utf8_file_byte_exact(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # a pre-existing file with INVALID UTF-8 bytes and desired content = the
        # replacement char: a lossy decode-compare would falsely match and report
        # "unchanged", leaving the wrong bytes. The raw-byte compare must detect
        # the difference and (with --force) overwrite to the exact desired bytes.
        with open(os.path.join(d, "a.py"), "wb") as fh:
            fh.write(b"\xff")
        state = {"calls": 0}

        def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
            state["calls"] += 1
            if state["calls"] == 1:
                return _plan("a.py"), {}, {"finish_reason": "stop"}
            return _rec("a.py", "�"), {}, {"finish_reason": "stop"}

        buf = io.StringIO()
        with _patched(complete=fake, safe_catalog=lambda *a, **k: _CAT,
                      cost_gate=lambda *a, **k: None, cost_gate_mr=lambda *a, **k: None,
                      _gate_amount=lambda *a, **k: None,
                      warn_if_stdin_ignored=lambda *a, **k: None):
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                try:
                    amb.cmd_build(_args(d, force=True), KEY, "https://x", {})
                except SystemExit:
                    pass
        with open(os.path.join(d, "a.py"), "rb") as fh:
            self.assertEqual(fh.read(), "�".encode("utf-8"))   # not b"\xff"

    def test_reasoning_draft_salvage_commits_no_records(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # complete() returned the model's REASONING as a draft (reasoning_draft=True)
        # because no final answer came. Any {"path","content"} the model *reasoned
        # about* is NOT a real file — commit nothing; the retry delivers the real
        # files. a.py must be REAL_A, never the reasoned-about REASONED_BAD.
        reasoning = ("[AMBIENT NOTE: reasoning draft]\n\n"
                     + _rec("a.py", "REASONED_BAD") + "\n" + _rec("b.py", "ALSO_BAD"))
        good = _rec("a.py", "REAL_A\n") + "\n" + _rec("b.py", "REAL_B\n")
        state = {"calls": 0}

        def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
            state["calls"] += 1
            if state["calls"] == 1:
                return _plan("a.py", "b.py"), {}, {"finish_reason": "stop"}
            if state["calls"] == 2:
                return (reasoning, {},
                        {"salvaged_partial": True, "reasoning_draft": True})
            return good, {}, {"finish_reason": "stop"}

        env, files = _run(d, fake)
        self.assertEqual(files.get("a.py"), "REAL_A\n", files)   # not REASONED_BAD
        self.assertEqual(files.get("b.py"), "REAL_B\n", files)

    def test_salvaged_partial_reply_drops_its_last_record(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # a SALVAGED-PARTIAL reply (complete() reassembled a best-effort draft
        # from a stalled/cut stream) is the ONE case where its last record may be
        # a file the model was mid-writing — drop it (requeue). a.py commits.
        state = {"calls": 0}

        def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
            state["calls"] += 1
            if state["calls"] == 1:
                return _plan("a.py", "b.py"), {}, {"finish_reason": "stop"}
            if state["calls"] == 2:
                return (_rec("a.py", "AAA\n") + "\n" + _rec("b.py", "MAYBE_CUT\n"),
                        {}, {"finish_reason": "length", "salvaged_partial": True})
            return _rec("b.py", "GOOD_B\n"), {}, {"finish_reason": "stop"}

        env, files = _run(d, fake)
        self.assertEqual(files.get("a.py"), "AAA\n", files)       # kept
        self.assertEqual(files.get("b.py"), "GOOD_B\n", files)    # dropped + requeued
        self.assertEqual(state["calls"], 3)

    def test_cut_wrapper_single_file_requeues_and_delivers_safely(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # legacy wrapper cut at its OUTER brace: last-fail resets on the inner a,
        # so a is conservatively drop-lasted + requeued (safe; rarely wasteful).
        # The requeue delivers a complete a — never a partial, never a hard fail.
        wrapper_cut = json.dumps({"files": [{"path": "a.py",
                                             "content": "A\n"}]})[:-1]  # drop '}'
        gen2 = _rec("a.py", "A\n")
        fake, st = _staged(_plan("a.py"), [(wrapper_cut, "length"), (gen2, "stop")])
        env, files = _run(d, fake)
        self.assertEqual(env.get("failed"), [], env)
        self.assertEqual(files.get("a.py"), "A\n", files)
        self.assertEqual(st["calls"], 3)                        # plan + gen + requeue

    def test_leading_junk_requeues_and_completes_without_false_records(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # a leading malformed object makes the parser STOP (no digging), so the
        # whole reply salvages nothing and requeues — never a mis-salvaged record.
        # The retry serves clean records and the build completes correctly.
        junk = "{junk\n" + _rec("a.py", "AAA\n") + "\n" + _rec("b.py", "BBB\n")
        good = _rec("a.py", "AAA\n") + "\n" + _rec("b.py", "BBB\n")
        fake = _trunc_then_good(_plan("a.py", "b.py"), junk, good)
        env, files = _run(d, fake)
        self.assertEqual(env.get("failed"), [], env)
        self.assertEqual(files.get("a.py"), "AAA\n", files)
        self.assertEqual(files.get("b.py"), "BBB\n", files)

    def test_lone_surrogate_content_fails_record_without_crashing(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # valid JSON, but content is a lone surrogate that cannot UTF-8 encode.
        gen = '{"path":"a.py","content":"\\ud800"}'
        fake, _st = _staged(_plan("a.py"), [(gen, "stop")] * 5)
        env, files = _run(d, fake)              # must not raise
        self.assertIn("a.py", [f["path"] for f in env.get("failed", [])], env)
        self.assertNotIn("a.py", files)

    def test_single_oversized_file_always_truncates_fails_safe(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # every reply is a cut record for the one file → never completes.
        cut = '{"path":"big.py","content":"xxxxxxxx'
        fake, _st = _staged(_plan("big.py"), [(cut, "length")] * 40)
        env, files = _run(d, fake)
        self.assertIn("big.py", [f["path"] for f in env.get("failed", [])], env)
        self.assertNotIn("big.py", files)          # never shipped partial


if __name__ == "__main__":
    unittest.main()
