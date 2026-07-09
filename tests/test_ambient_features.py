"""Hermetic feature-level tests: onboarding + key validation, model curation,
the native build mode (plan/generate/apply + path firewall), and docs-drift
guards. No network, no live API."""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_f", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_f", loader)
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
def temp_config():
    d = tempfile.mkdtemp()
    cfg = os.path.join(d, "env")
    env_had = os.environ.pop("AMBIENT_API_KEY", None)
    try:
        with patched(amb, CONFIG_PATH=cfg, keychain_read=lambda: None):
            yield cfg
    finally:
        if env_had is not None:
            os.environ["AMBIENT_API_KEY"] = env_had


def setup_ns(**kw):
    base = dict(key_stdin=False, force=False, file=False, remove=False)
    base.update(kw)
    return argparse.Namespace(**base)


class NotATTY(io.StringIO):
    def isatty(self):
        return False


class TestKeyValidation(unittest.TestCase):
    def test_key_paste_problem_matrix(self):
        self.assertIsNotNone(amb.key_paste_problem(""))
        self.assertIsNotNone(amb.key_paste_problem("has space key"))
        self.assertIsNotNone(amb.key_paste_problem("smart’quote-key-000"))
        self.assertIsNotNone(amb.key_paste_problem("short"))
        self.assertIsNotNone(amb.key_paste_problem('quo"te-key-000000000'))
        self.assertIsNotNone(amb.key_paste_problem("back\\slash-key-0000"))
        self.assertIsNone(amb.key_paste_problem("sk-perfectly-fine-key-000"))

    def test_classify_error_full_matrix(self):
        cases = [
            (500, "billing service down", "service"),
            (402, "pay up", "funds"),
            (403, "insufficient credits", "funds"),
            (401, "bad key", "key"),
            (429, "No workers available", "model"),
            (429, "slow down", "rate"),
            (400, "maximum context length exceeded", "context"),
            (400, "max_tokens exceeds limit", "budget"),
            (418, "teapot", "unknown"),
        ]
        for status, msg, want in cases:
            got = amb.classify_error(status, {"error": {"message": msg}}, "k")[0]
            self.assertEqual(got, want, f"{status} {msg}")

    def test_auth_probe_matrix(self):
        models = [{"id": "m", "is_ready": True}]

        def probe(status, msg):
            with patched(amb, api_request=lambda *a, **k: (
                    status, {"error": {"message": msg}} if status != 200
                    else {"choices": [{"message": {"content": "hi"}}]})):
                return amb.auth_probe("https://x", "k", models)

        self.assertTrue(probe(200, "")[0])
        ok, cat, _ = probe(429, "No workers available")
        self.assertTrue(ok)          # no-workers is only reported post-auth
        self.assertEqual(cat, "model")
        self.assertFalse(probe(429, "rate limited")[0])   # indeterminate
        self.assertFalse(probe(503, "outage")[0])          # indeterminate
        self.assertFalse(probe(401, "nope")[0])


class TestSetupFlow(unittest.TestCase):
    def test_bad_key_saves_nothing(self):
        with temp_config() as cfg:
            with patched(amb, api_request=lambda *a, **k: (
                    401, {"error": {"message": "invalid"}}),
                    keychain_write=lambda k: self.fail("must not store")):
                with patched(amb.sys, stdin=io.StringIO(
                        "sk-a-plausible-key-000000\n")):
                    with self.assertRaises(SystemExit) as cm:
                        amb.cmd_setup(setup_ns(key_stdin=True))
            self.assertIn("Nothing was saved", str(cm.exception))
            self.assertFalse(os.path.exists(cfg))

    def _happy_api(self):
        return dict(
            api_request=lambda *a, **k: (200, {"data": [
                {"id": "m", "is_ready": True}]}),
            auth_probe=lambda *a, **k: (True, "ok", "live completion ok"),
        )

    def test_keychain_store_scrubs_plaintext(self):
        with temp_config() as cfg:
            with patched(amb, CONFIG_PATH=cfg):
                amb.save_config_values({"AMBIENT_API_KEY": "old-plaintext"})
            with patched(amb, keychain_available=lambda: True,
                         keychain_write=lambda k: True,
                         print_welcome_panel=lambda *a, **k: None,
                         **self._happy_api()):
                with patched(amb.sys, stdin=io.StringIO(
                        "sk-a-plausible-key-000000\n")):
                    with contextlib.redirect_stdout(io.StringIO()):
                        amb.cmd_setup(setup_ns(key_stdin=True, force=True))
            body = open(cfg, encoding="utf-8").read()
            self.assertNotIn("AMBIENT_API_KEY", body)
            self.assertIn("AMBIENT_KEY_BACKEND=keychain", body)

    def test_keychain_write_failure_fails_closed(self):
        with temp_config() as cfg:
            with patched(amb, keychain_available=lambda: True,
                         keychain_write=lambda k: False,
                         **self._happy_api()):
                with patched(amb.sys, stdin=io.StringIO(
                        "sk-a-plausible-key-000000\n")):
                    with self.assertRaises(SystemExit) as cm:
                        amb.cmd_setup(setup_ns(key_stdin=True))
            self.assertIn("nothing saved", str(cm.exception))
            self.assertNotIn("AMBIENT_API_KEY",
                             open(cfg, encoding="utf-8").read() if os.path.exists(cfg) else "")

    def test_out_of_funds_key_is_saved_with_topup_path(self):
        with temp_config() as cfg:
            out = io.StringIO()
            with patched(amb, keychain_available=lambda: False,
                         keychain_delete=lambda: True,
                         api_request=lambda *a, **k: (
                             200, {"data": [{"id": "m", "is_ready": True}]}),
                         auth_probe=lambda *a, **k: (
                             False, "funds", "HTTP 402 insufficient")):
                with patched(amb.sys, stdin=io.StringIO(
                        "sk-a-plausible-key-000000\n")):
                    with contextlib.redirect_stdout(out):
                        amb.cmd_setup(setup_ns(key_stdin=True))
            self.assertIn("AMBIENT_API_KEY=sk-a-plausible-key-000000",
                          open(cfg, encoding="utf-8").read())
            self.assertIn("OUT OF FUNDS", out.getvalue())

    def test_remove_fails_loudly_when_keychain_refuses(self):
        with temp_config():
            with patched(amb, keychain_delete=lambda: False):
                with self.assertRaises(SystemExit) as cm:
                    amb.cmd_setup(setup_ns(remove=True))
            self.assertIn("REFUSED", str(cm.exception))

    def test_stdin_key_prevalidated(self):
        with temp_config():
            with patched(amb.sys, stdin=io.StringIO("short\n")):
                with self.assertRaises(SystemExit) as cm:
                    amb.cmd_setup(setup_ns(key_stdin=True))
            self.assertIn("too short", str(cm.exception))

    def test_unconfigured_noninteractive_exits_3(self):
        with temp_config():
            with patched(amb.sys, stdin=NotATTY(), stdout=NotATTY(),
                         stderr=NotATTY()):
                with self.assertRaises(SystemExit) as cm:
                    amb.load_config()
            self.assertEqual(cm.exception.code, 3)

    def test_key_in_argv_refused(self):
        with patched(amb.sys, argv=["ambient", "setup",
                                    "sk-fixture-" + "a" * 24]):
            with self.assertRaises(SystemExit) as cm:
                amb.main()
        self.assertIn("shell history", str(cm.exception))


class TestCuration(unittest.TestCase):
    def test_is_hidden_globs_and_show_override(self):
        allow, hide, show = [], ["qwen/*"], []
        self.assertTrue(amb.is_hidden("qwen/foo", allow, hide, show))
        self.assertFalse(amb.is_hidden("kimi/x", allow, hide, show))
        # SHOW override beats a glob hide
        self.assertFalse(amb.is_hidden("qwen/foo", allow, hide, ["qwen/foo"]))
        # strict allow-mode
        self.assertTrue(amb.is_hidden("other/m", ["kimi/*"], [], []))
        self.assertFalse(amb.is_hidden("kimi/x", ["kimi/*"], [], []))
        # SHOW override beats strict allow too
        self.assertFalse(amb.is_hidden("other/m", ["kimi/*"], [], ["other/m"]))

    def test_curate_show_surfaces_glob_hidden_model(self):
        with temp_config():
            amb.save_config_values({"AMBIENT_MODELS_HIDE": "qwen/*"})
            with patched(amb, safe_catalog=lambda *a: []):
                with contextlib.redirect_stdout(io.StringIO()):
                    amb.cmd_curate(argparse.Namespace(
                        verb="show", ids=["qwen/foo"]))
            conf = amb.read_config_file()
            allow, hide, show, _ = amb.curation(conf)
            self.assertFalse(amb.is_hidden("qwen/foo", allow, hide, show))
            self.assertTrue(amb.is_hidden("qwen/bar", allow, hide, show))

    def test_curate_only_clears_hide(self):
        with temp_config():
            amb.save_config_values({"AMBIENT_MODELS_HIDE": "kimi/x"})
            with patched(amb, safe_catalog=lambda *a: []):
                with contextlib.redirect_stdout(io.StringIO()):
                    amb.cmd_curate(argparse.Namespace(
                        verb="only", ids=["kimi/x"]))
            conf = amb.read_config_file()
            allow, hide, show, _ = amb.curation(conf)
            self.assertFalse(amb.is_hidden("kimi/x", allow, hide, show))

    def test_notes_default_and_clear(self):
        _a, _h, _s, notes = amb.curation({})
        self.assertIn("z-ai/glm-5.2", notes)  # built-in ramp-up note
        _a, _h, _s, notes = amb.curation(
            {"AMBIENT_MODEL_NOTES": json.dumps({"z-ai/glm-5.2": ""})})
        self.assertFalse(notes["z-ai/glm-5.2"])  # the user cleared it

    def test_malformed_notes_degrade(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _a, _h, _s, notes = amb.curation({"AMBIENT_MODEL_NOTES": "{bad"})
        self.assertIn("malformed", err.getvalue())


class TestSafeRelpath(unittest.TestCase):
    def test_reject_corpus(self):
        root = tempfile.mkdtemp()
        bad = ["../x", "a/../../x", "/etc/cron.d/x", "C:/Windows/x",
               "//server/share", ".git/hooks/post-checkout", "a/.git/config",
               "~/x", "a\x00b", ".env", "src/.env.production", "id_rsa",
               "keys/server.pem", "creds/credentials.json", "", "   ",
               "a/" + "b" * 2000]
        for p in bad:
            with self.assertRaises(ValueError, msg=p):
                amb.safe_relpath(p, root)

    def test_accept_corpus(self):
        root = tempfile.mkdtemp()
        for p in ("src/app.py", "deep/n/e/s/t/mod.rs", "README.md",
                  "static/env-config.js", "envelope.py", "a\\b\\c.txt"):
            amb.safe_relpath(p, root)

    def test_symlinked_dir_escape_blocked(self):
        root = tempfile.mkdtemp()
        outside = tempfile.mkdtemp()
        os.symlink(outside, os.path.join(root, "sub"))
        with self.assertRaises(ValueError):
            amb.safe_relpath("sub/evil.py", root)


def build_ns(root, **kw):
    base = dict(task=["make", "a", "thing"], dir=root, context=None,
                apply=False, force=False, plan_only=False, dry_run=False,
                max_files=32, max_file_bytes=200_000, no_resume=False,
                json=True, allow_secrets=False, model=None, max_tokens=None,
                temperature=0.1, timeout=30, raw=False, fallback=False,
                allow_partial=True, allow_cost=True, yes=True, no_cache=True,
                cache_ttl=None)
    base.update(kw)
    return argparse.Namespace(**base)


def fake_build_complete(plan_files, gen_batches):
    """complete() stub: first call returns the plan, later calls pop canned
    generation results (each {"files":[...]} + optional finish_reason)."""
    state = {"calls": 0}
    batches = list(gen_batches)

    def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
        state["calls"] += 1
        if state["calls"] == 1:
            plan = {"plan": plan_files, "notes": "n", "advisory_steps": ["run tests"]}
            return json.dumps(plan), {}, {"finish_reason": "stop"}
        resp = batches.pop(0)
        body = {"finish_reason": resp.get("finish_reason", "stop")}
        return json.dumps({"files": resp["files"]}), {}, body

    return fake, state


CAT = [{"id": "moonshotai/kimi-k2.7-code", "context_length": 262144,
        "max_output_length": 262144, "is_ready": True,
        "supported_features": ["reasoning", "structured_outputs"],
        "output_modalities": ["text"],
        "pricing": {"input": 1.0, "output": 3.83}}]


class TestBuildMode(unittest.TestCase):
    def _run(self, args, fake):
        buf = io.StringIO()
        with patched(amb, complete=fake, safe_catalog=lambda *a: CAT,
                     cost_gate=lambda *a, **k: None,
                     warn_if_stdin_ignored=lambda *a: None):
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(io.StringIO()):
                amb.cmd_build(args, "k", "https://x", {})
        return json.loads(buf.getvalue())

    def test_unsafe_plan_paths_fail_closed(self):
        root = tempfile.mkdtemp()
        fake, _ = fake_build_complete(
            [{"path": "../evil.py", "purpose": "p", "est_lines": 5},
             {"path": "ok.py", "purpose": "p", "est_lines": 5}],
            [{"files": [{"path": "ok.py", "content": "x = 1\n"}]}])
        env = self._run(build_ns(root), fake)
        self.assertEqual([f["path"] for f in env["files"]], ["ok.py"])
        self.assertTrue(any("unsafe path" in f["reason"]
                            for f in env["failed"]))
        self.assertEqual(env["status"], "partial")

    def test_truncated_batch_requeues_and_completes(self):
        # Record-framed contract: the gen reply is per-file JSON objects. a.py's
        # object is complete; b.py's is cut mid-content (decoder drops it), and on
        # the length finish the conservative guard also drops the last accepted
        # file — so the truncated batch requeues and the retry delivers both.
        root = tempfile.mkdtemp()
        plan = [{"path": "a.py", "purpose": "p", "est_lines": 5},
                {"path": "b.py", "purpose": "p", "est_lines": 5}]
        state = {"calls": 0}

        def fake(api_key, api_url, model, messages, args, on_delta=None, **kw):
            state["calls"] += 1
            if state["calls"] == 1:
                return (json.dumps({"plan": plan, "notes": "n",
                                    "advisory_steps": []}),
                        {}, {"finish_reason": "stop"})
            if state["calls"] == 2:
                return ('{"path":"a.py","content":"A\\n"}\n'
                        '{"path":"b.py","content":"BB',
                        {}, {"finish_reason": "length"})
            return ('{"path":"a.py","content":"A\\n"}\n'
                    '{"path":"b.py","content":"BBB\\n"}',
                    {}, {"finish_reason": "stop"})

        env = self._run(build_ns(root), fake)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(sorted(f["path"] for f in env["files"]),
                         ["a.py", "b.py"])
        bpy = next(f for f in env["files"] if f["path"] == "b.py")
        self.assertEqual(bpy["bytes"], len("BBB\n".encode()))
        # plan + truncated gen + per-file requeue(s); exact count depends on how
        # the requeue splits the batch, so just assert it took a retry.
        self.assertGreaterEqual(state["calls"], 3)

    def test_apply_writes_inside_root_only(self):
        root = tempfile.mkdtemp()
        plan = [{"path": "pkg/mod.py", "purpose": "p", "est_lines": 3}]
        fake, _ = fake_build_complete(plan, [
            {"files": [{"path": "pkg/mod.py", "content": "V = 7\n"}]}])
        env = self._run(build_ns(root, apply=True, yes=True), fake)
        dest = os.path.join(root, "pkg", "mod.py")
        self.assertTrue(os.path.exists(dest))
        self.assertEqual(open(dest, encoding="utf-8").read(), "V = 7\n")
        self.assertTrue(env["written"])

    def test_apply_refuses_overwrite_without_force(self):
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "keep.py"), "w") as fh:
            fh.write("original\n")
        plan = [{"path": "keep.py", "purpose": "p", "est_lines": 3}]
        fake, _ = fake_build_complete(plan, [
            {"files": [{"path": "keep.py", "content": "clobber\n"}]}])
        env = self._run(build_ns(root, apply=True, yes=True), fake)
        self.assertEqual(open(os.path.join(root, "keep.py")).read(),
                         "original\n")
        self.assertEqual(env["files"][0]["action"], "skip-exists")
        self.assertEqual(env["status"], "partial")

    def test_dry_run_makes_zero_calls_even_with_large_context(self):
        # HIGH #1: --dry-run must spend + egress NOTHING, even when a big -f
        # context would otherwise trigger a distillation map-reduce first.
        root = tempfile.mkdtemp()
        ctxf = os.path.join(root, "big.py")
        with open(ctxf, "w", encoding="utf-8") as fh:
            fh.write("x = 1  # padding line\n" * 200_000)  # ~4MB → would distill

        def boom(*a, **k):
            self.fail("--dry-run must make ZERO API calls")

        buf = io.StringIO()
        with patched(amb, complete=boom, safe_catalog=lambda *a: CAT,
                     run_map_reduce=boom, cost_gate=lambda *a, **k: None,
                     warn_if_stdin_ignored=lambda *a: None):
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(io.StringIO()):
                amb.cmd_build(build_ns(root, dry_run=True, context=[ctxf]),
                              "k", "https://x", {})
        out = buf.getvalue()
        self.assertIn("dry run", out.lower())
        self.assertIn("distills it first", out)  # discloses the live-run cost

    def test_force_backup_failure_refuses_to_destroy_original(self):
        # HIGH #2: if the --force backup move fails, the original file must NOT
        # be overwritten (silent data loss is disqualifying).
        root = tempfile.mkdtemp()
        keep = os.path.join(root, "keep.py")
        with open(keep, "w", encoding="utf-8") as fh:
            fh.write("ORIGINAL\n")
        plan = [{"path": "keep.py", "purpose": "p", "est_lines": 3}]
        fake, _ = fake_build_complete(plan, [
            {"files": [{"path": "keep.py", "content": "CLOBBER\n"}]}])
        real_replace = os.replace

        def replace_fail_on_backup(src, dst, *a, **k):
            if ".ambient-build.bak" in str(dst):
                raise OSError("backup target unwritable")
            return real_replace(src, dst, *a, **k)

        buf = io.StringIO()
        with patched(amb, complete=fake, safe_catalog=lambda *a: CAT,
                     cost_gate=lambda *a, **k: None,
                     warn_if_stdin_ignored=lambda *a: None):
            with patched(amb.os, replace=replace_fail_on_backup):
                with contextlib.redirect_stdout(buf), \
                        contextlib.redirect_stderr(io.StringIO()):
                    amb.cmd_build(
                        build_ns(root, apply=True, force=True, yes=True),
                        "k", "https://x", {})
        env = json.loads(buf.getvalue())
        self.assertEqual(open(keep, encoding="utf-8").read(), "ORIGINAL\n")  # preserved
        self.assertEqual(env["files"][0]["action"], "write-failed")
        self.assertTrue(any("backup failed" in f["reason"]
                            for f in env["failed"]))
        self.assertEqual(env["status"], "partial")

    def test_generation_drops_files_not_in_the_plan(self):
        # MED #6 (+ re-verify): an unplanned file the model slips in must be
        # dropped + never written, but a fully-delivered plan must still be
        # status ok — the drop is surfaced in `dropped`, NOT counted as a
        # failure (so a chatty model can't turn a complete build into exit 2).
        root = tempfile.mkdtemp()
        plan = [{"path": "a.py", "purpose": "p", "est_lines": 3}]
        fake, _ = fake_build_complete(plan, [
            {"files": [{"path": "a.py", "content": "A\n"},
                       {"path": "sneaky.py", "content": "SNEAK\n"}]}])
        env = self._run(build_ns(root, apply=True, yes=True), fake)
        self.assertEqual([f["path"] for f in env["files"]], ["a.py"])
        self.assertFalse(os.path.exists(os.path.join(root, "sneaky.py")))
        self.assertIn("sneaky.py", env["dropped"])
        self.assertEqual(env["failed"], [])
        self.assertEqual(env["status"], "ok")

    def test_completed_apply_rerun_is_idempotent_and_not_rebilled(self):
        # MED #7 (+ re-verify): re-running a finished --apply build is an
        # "unchanged" no-op (status ok / exit 0), AND it resumes from state
        # without re-billing a single API call.
        root = tempfile.mkdtemp()
        plan = [{"path": "a.py", "purpose": "p", "est_lines": 3}]
        fake1, _ = fake_build_complete(plan, [
            {"files": [{"path": "a.py", "content": "A\n"}]}])
        env1 = self._run(build_ns(root, apply=True, yes=True), fake1)
        self.assertEqual(env1["files"][0]["action"], "create")
        fake2, s2 = fake_build_complete(plan, [
            {"files": [{"path": "a.py", "content": "A\n"}]}])
        env2 = self._run(build_ns(root, apply=True, yes=True), fake2)
        self.assertEqual(env2["files"][0]["action"], "unchanged")
        self.assertEqual(env2["status"], "ok")
        self.assertEqual(s2["calls"], 0)  # resumed from state — zero re-bill

    def test_unchanged_context_still_resumes_without_rebilling(self):
        # MED #5 converse: an UNCHANGED -f context must still resume (0 calls).
        # An over-invalidating raw_context_sha would re-bill every -f resume.
        root = tempfile.mkdtemp()
        ctxf = os.path.join(root, "ctx.py")
        with open(ctxf, "w", encoding="utf-8") as fh:
            fh.write("SHARED = 1\n")
        plan = [{"path": "a.py", "purpose": "p", "est_lines": 3}]
        fake1, s1 = fake_build_complete(plan, [
            {"files": [{"path": "a.py", "content": "A\n"}]}])
        self._run(build_ns(root, context=[ctxf]), fake1)
        self.assertEqual(s1["calls"], 2)
        fake2, s2 = fake_build_complete(plan, [
            {"files": [{"path": "a.py", "content": "A\n"}]}])
        self._run(build_ns(root, context=[ctxf]), fake2)  # same content
        self.assertEqual(s2["calls"], 0)  # sha stable → resumed, no re-bill

    def test_crlf_content_reruns_as_unchanged(self):
        # re-verify: CRLF content must round-trip byte-faithfully so a completed
        # build re-runs as "unchanged", not skip-exists/partial.
        root = tempfile.mkdtemp()
        plan = [{"path": "a.py", "purpose": "p", "est_lines": 3}]
        fake1, _ = fake_build_complete(plan, [
            {"files": [{"path": "a.py", "content": "line1\r\nline2\r\n"}]}])
        env1 = self._run(build_ns(root, apply=True, yes=True), fake1)
        self.assertEqual(env1["files"][0]["action"], "create")
        fake2, _ = fake_build_complete(plan, [
            {"files": [{"path": "a.py", "content": "line1\r\nline2\r\n"}]}])
        env2 = self._run(build_ns(root, apply=True, yes=True), fake2)
        self.assertEqual(env2["files"][0]["action"], "unchanged")
        self.assertEqual(env2["status"], "ok")

    def test_editing_context_invalidates_stale_resume(self):
        # MED #5: task_sha must depend on -f CONTENT, so editing a context file
        # forces a re-plan instead of serving a stale cached plan/files.
        root = tempfile.mkdtemp()
        ctxf = os.path.join(root, "ctx.py")
        with open(ctxf, "w", encoding="utf-8") as fh:
            fh.write("VERSION = 1\n")
        plan = [{"path": "a.py", "purpose": "p", "est_lines": 3}]
        fake1, s1 = fake_build_complete(plan, [
            {"files": [{"path": "a.py", "content": "A1\n"}]}])
        self._run(build_ns(root, context=[ctxf]), fake1)
        self.assertEqual(s1["calls"], 2)         # plan + 1 generation
        with open(ctxf, "w", encoding="utf-8") as fh:
            fh.write("VERSION = 2\n")            # edit the context
        fake2, s2 = fake_build_complete(plan, [
            {"files": [{"path": "a.py", "content": "A2\n"}]}])
        self._run(build_ns(root, context=[ctxf]), fake2)
        # Re-planned (2 calls). With the old path-only sha this would be 0
        # (stale resume: state found, todo empty).
        self.assertEqual(s2["calls"], 2)

    def test_headless_apply_needs_dir_and_yes(self):
        # Misuse of headless --apply is a USAGE error: EX_USAGE (64) with the
        # message on stderr, not a runtime failure (exit 1) with the message
        # buried in the exception.
        fake, _ = fake_build_complete([], [])
        with patched(amb.sys, stdin=NotATTY()):
            with patched(amb, complete=fake, safe_catalog=lambda *a: CAT,
                         warn_if_stdin_ignored=lambda *a: None):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as cm:
                        amb.cmd_build(build_ns(None, dir=None, apply=True),
                                      "k", "https://x", {})
                self.assertEqual(cm.exception.code, amb.EXIT_USAGE)
                self.assertIn("--dir", err.getvalue())
                err2 = io.StringIO()
                with contextlib.redirect_stderr(err2):
                    with self.assertRaises(SystemExit) as cm2:
                        amb.cmd_build(
                            build_ns(tempfile.mkdtemp(), apply=True, yes=False),
                            "k", "https://x", {})
                self.assertEqual(cm2.exception.code, amb.EXIT_USAGE)
                self.assertIn("--yes", err2.getvalue())

    def test_resume_skips_plan_and_done_files(self):
        root = tempfile.mkdtemp()
        plan = [{"path": "a.py", "purpose": "p", "est_lines": 3},
                {"path": "b.py", "purpose": "p", "est_lines": 3}]
        fake1, _ = fake_build_complete(plan, [
            {"files": [{"path": "a.py", "content": "A\n"},
                       {"path": "b.py", "content": "B\n"}]}])
        self._run(build_ns(root), fake1)  # first full run seeds the state

        def fail_if_called(*a, **k):
            self.fail("resume must not re-bill anything")

        buf = io.StringIO()
        with patched(amb, complete=fail_if_called,
                     safe_catalog=lambda *a: CAT,
                     cost_gate=lambda *a, **k: None,
                     warn_if_stdin_ignored=lambda *a: None):
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(io.StringIO()):
                amb.cmd_build(build_ns(root), "k", "https://x", {})
        env = json.loads(buf.getvalue())
        self.assertEqual(sorted(f["path"] for f in env["files"]),
                         ["a.py", "b.py"])


class TestDocsDrift(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([sys.executable, BIN, "definitely-not-a-cmd"],
                              capture_output=True, text=True)
        m = re.search(r"choose from (.+?)\)", proc.stderr)
        cls.commands = {c.strip().strip("'") for c in m.group(1).split(",")} \
            if m else set()
        cls.usage_exit = proc.returncode

    def test_usage_error_exits_64_with_suggestion(self):
        self.assertEqual(self.usage_exit, 64)
        proc = subprocess.run([sys.executable, BIN, "audti"],
                              capture_output=True, text=True)
        self.assertIn("did you mean: audit", proc.stderr)
        self.assertEqual(proc.returncode, 64)

    def test_banner_names_only_real_commands(self):
        self.assertTrue(self.commands)
        with temp_config():
            banner = amb.build_banner()
        for cmd in re.findall(r"^\s{2}ambient ([a-z-]+)", banner, re.M):
            self.assertIn(cmd, self.commands, f"banner advertises '{cmd}'")

    def test_skill_and_readme_commands_exist(self):
        self.assertTrue(self.commands)
        ok_words = self.commands | {"xyz", "code…", "code:"}
        for rel in ("skills/ambient/SKILL.md", "README.md"):
            # encoding="utf-8" is REQUIRED: these docs contain emoji/em-dashes, and
            # Windows' default cp1252 raises UnicodeDecodeError on bytes like 0x8f
            # (e.g. the ⚠️ variation selector) — a hermetic test must not depend on
            # the platform locale.
            text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
            for cmd in re.findall(r"`ambient ([a-z][a-z-]+)\b", text):
                self.assertIn(cmd, ok_words, f"{rel} references 'ambient {cmd}'")

    def test_version_quad_sync(self):
        # encoding="utf-8" on every text read — CHANGELOG/pyproject can carry
        # non-ASCII, and Windows' cp1252 default would otherwise break this.
        with open(os.path.join(ROOT, ".claude-plugin/plugin.json"),
                  encoding="utf-8") as fh:
            pj = json.load(fh)
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
            py = re.search(r'version\s*=\s*"([^"]+)"', fh.read())
        self.assertEqual(amb.__version__, pj["version"])
        self.assertEqual(amb.__version__, py.group(1))
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as fh:
            head = fh.read().split("\n##")[1]
        self.assertIn(amb.__version__, head)

    def test_welcome_panel_commands_exist(self):
        self.assertTrue(self.commands)
        buf = io.StringIO()
        with temp_config(), contextlib.redirect_stdout(buf):
            amb.print_welcome_panel([{"id": "m", "is_ready": True}],
                                    "keychain", "ok", {})
        for cmd in re.findall(r"^\s+ambient ([a-z-]+)", buf.getvalue(), re.M):
            self.assertIn(cmd, self.commands, f"panel advertises '{cmd}'")


if __name__ == "__main__":
    unittest.main()
