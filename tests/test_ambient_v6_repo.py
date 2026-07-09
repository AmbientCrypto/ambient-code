"""Hermetic tests: repo intelligence.

- 5a: build_code_map speaks more than Python (JS/TS, Go, Rust, Ruby, C-family
  regex signatures at column-0/brace-depth-0), scales its budget with the map
  model's window (code_map_budget = min(single_shot//10, 40k)), and appends an
  explicit "(+N files omitted from the repo map)" marker instead of silently
  truncating.
- 5b: `audit --repo [DIR]` — git-aware walker (git ls-files honors .gitignore;
  the fallback walk prunes .git/node_modules/dist/build/vendor/__pycache__ and
  dotdirs, never follows symlinks), skips binaries/lockfiles/oversized files,
  reports files/chars/est-cost UPFRONT, refuses over ABS_MAX_CHARS unless
  --allow-cost/--allow-partial, and routes through the existing map-reduce so
  --parallel/--reduce-model/cost-gate/--json all apply unchanged.
- 5c: bounded cross-file confirmation — AT MOST one extra gated single-shot
  pass over pass-1 suspects, default-on for --repo only, --no-deep opts out.

Every test patches complete()/run_map_reduce/safe_catalog; no network, no
live API, no writes outside tempdirs.
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
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v6repo", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v6repo", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = load_module()

KEY = "sk-test-key-abcdef1234567890"


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


def repo_catalog():
    """tiny/auditor has a small window so a modest repo forces map-reduce;
    strong/reduce is a valid synthesis target for --reduce-model threading."""
    return [
        {"id": "tiny/auditor", "context_length": 16000,
         "max_output_length": 8000, "is_ready": True,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 0.2, "output": 0.8}},
        {"id": "strong/reduce", "context_length": 200000,
         "max_output_length": 65536, "is_ready": True,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 1.0, "output": 4.0}},
        {"id": "big/auditor", "context_length": 262144,
         "max_output_length": 65536, "is_ready": True,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 0.2, "output": 0.8}},
    ]


def audit_args(**kw):
    base = dict(paths=[], staged=False, diff=None, focus=None,
                allow_secrets=False, format="prose", dry_run=False,
                consensus=None, model="tiny/auditor", max_tokens=None,
                temperature=0.1, timeout=30, raw=False, fallback=False,
                allow_partial=False, allow_cost=True, yes=True, no_cache=True,
                cache_ttl=None, parallel=None, reduce_model=None, json=False,
                repo=None, deep=None)
    base.update(kw)
    return argparse.Namespace(**base)


def make_repo(files, root=None):
    """Write a fake repo tree: {relpath: bytes-or-str}."""
    root = root or tempfile.mkdtemp()
    for rel, content in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(full, mode) as fh:
            fh.write(content)
    return root


def no_git():
    """A subprocess stand-in whose git always fails → the walk lane runs."""
    def run(*a, **k):
        raise OSError("git unavailable in this test")
    return types.SimpleNamespace(run=run,
                                 TimeoutExpired=subprocess.TimeoutExpired)


def fake_git(stdout, returncode=0):
    def run(cmd, **k):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout,
                                     stderr="")
    return types.SimpleNamespace(run=run,
                                 TimeoutExpired=subprocess.TimeoutExpired)


PY_SRC = "def handler(req, res):\n    return res\n\nclass Router:\n    pass\n"
JS_SRC = (
    "export function fooFn(a, b) {\n  return a + b;\n}\n"
    "const barArrow = async (x) => x * 2;\n"
    "export default class BazClass {\n  method() {}\n}\n"
    "export interface QuxIface {\n  id: number;\n}\n"
    "type AliasType = string | number;\n"
)
GO_SRC = (
    "package main\n\n"
    "func Serve(addr string) error {\n\treturn nil\n}\n\n"
    "func (s *Server) HandleReq(w io.Writer) {\n}\n\n"
    "type Server struct {\n\taddr string\n}\n\n"
    "type Reader interface {\n\tRead()\n}\n"
)
RS_SRC = (
    "pub fn run_main(cfg: &Config) -> Result<(), Error> {\n    Ok(())\n}\n\n"
    "pub struct ConfigStruct {\n    path: String,\n}\n\n"
    "enum ModeEnum { A, B }\n\n"
    "pub trait RunnerTrait {\n    fn go(&self);\n}\n\n"
    "impl RunnerTrait for ConfigStruct {\n"
    "    fn inner_method(&self) {}\n"
    "}\n"
)
RB_SRC = (
    "module OuterMod\n"
    "  class InnerClass\n"
    "    def do_work(x)\n      x\n    end\n"
    "  end\nend\n"
)
C_SRC = (
    "#include <stdio.h>\n\n"
    "static int parse_args(int argc, char **argv) {\n"
    "    if (argc < 2) {\n        helper_call(argc);\n    }\n"
    "    return 0;\n}\n"
)


class TestCodeMapMultiLanguage(unittest.TestCase):
    def test_js_ts_signatures(self):
        m = amb.build_code_map([("app.ts", JS_SRC)])
        for name in ("fooFn", "barArrow", "BazClass", "QuxIface", "AliasType"):
            self.assertIn(name, m, m)

    def test_go_signatures(self):
        m = amb.build_code_map([("srv.go", GO_SRC)])
        for name in ("Serve", "HandleReq", "Server", "Reader"):
            self.assertIn(name, m, m)

    def test_rust_signatures(self):
        m = amb.build_code_map([("lib.rs", RS_SRC)])
        for name in ("run_main", "ConfigStruct", "ModeEnum", "RunnerTrait",
                     "impl"):
            self.assertIn(name, m, m)
        self.assertIn("inner_method", m, m)  # fn inside impl (depth 1)

    def test_ruby_and_c_signatures(self):
        m = amb.build_code_map([("job.rb", RB_SRC), ("cli.c", C_SRC)])
        self.assertIn("OuterMod", m)
        self.assertIn("do_work", m)
        self.assertIn("parse_args", m)
        # calls INSIDE a function body must not be reported as signatures
        self.assertNotIn("helper_call", m)

    def test_python_still_ast_based(self):
        m = amb.build_code_map([("api.py", PY_SRC)])
        self.assertIn("def handler(req, res)", m)
        self.assertIn("class Router", m)

    def test_gutter_prefixed_source_still_extracts(self):
        guttered = amb.with_line_gutters([("app.js", JS_SRC)])
        m = amb.build_code_map(guttered)
        self.assertIn("fooFn", m)

    def test_budget_scaling_helper(self):
        self.assertEqual(amb.code_map_budget(None), 4000)
        self.assertEqual(amb.code_map_budget(0), 4000)
        self.assertEqual(amb.code_map_budget(120_000), 12_000)
        self.assertEqual(amb.code_map_budget(400_000), 40_000)
        self.assertEqual(amb.code_map_budget(1_000_000), 40_000)

    def test_omission_marker_when_over_budget(self):
        labeled = [(f"pkg/file_{i:03}.py", "def f%d(): pass\n" % i)
                   for i in range(200)]
        m = amb.build_code_map(labeled, budget=800)
        self.assertLessEqual(len(m), 800)
        self.assertRegex(m, r"\(\+\d+ files omitted from the repo map\)")
        shown = m.count("pkg/file_")
        n = int(__import__("re").search(r"\(\+(\d+) files omitted", m).group(1))
        self.assertEqual(shown + n, 200)

    def test_no_marker_when_everything_fits(self):
        m = amb.build_code_map([("a.py", PY_SRC)], budget=4000)
        self.assertNotIn("omitted from the repo map", m)

    def test_audit_passes_scaled_budget_to_code_map(self):
        catalog = repo_catalog()
        profile = amb.model_profile(catalog, "tiny/auditor")
        root = make_repo({"src/a.py": "x = 1\n" * 8000,
                          "src/b.py": "y = 2\n" * 8000,
                          "src/c.py": "z = 3\n" * 8000})
        seen = {}

        def spy_map(labeled, budget=None):
            seen["budget"] = budget
            return "REPO MAP"

        args = audit_args(repo=root, deep=False)
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(), build_code_map=spy_map,
                     run_map_reduce=lambda *a, **k: ("ok", False, ""),
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, KEY, "https://x", {})
        self.assertEqual(seen["budget"],
                         amb.code_map_budget(profile.single_shot_chars))


class TestRepoWalk(unittest.TestCase):
    def test_walk_lane_skips_vendored_dot_binary_lockfiles(self):
        root = make_repo({
            "src/a.py": "print('a')\n",
            "b.js": "const x = 1;\n",
            "node_modules/dep/x.js": "junk\n",
            "dist/bundle.js": "junk\n",
            "build/out.o.txt": "junk\n",
            "vendor/lib.py": "junk\n",
            "__pycache__/a.pyc.txt": "junk\n",
            ".git/config": "[core]\n",
            ".hidden/z.py": "junk\n",
            "img.dat": b"\x00\x01\x02BINARY",
            "package-lock.json": '{"lockfileVersion": 3}\n',
            "Cargo.lock": "[[package]]\n",
            "empty.txt": "",
        })
        with patched(amb, subprocess=no_git()):
            files, skipped, used_git = amb.repo_walk(root)
        rels = [rel for rel, _full, _sz in files]
        self.assertEqual(rels, ["b.js", "src/a.py"])
        self.assertFalse(used_git)
        self.assertGreaterEqual(skipped["binary"], 1)
        self.assertGreaterEqual(skipped["lockfile"], 2)

    def test_git_lane_uses_ls_files_and_never_escapes(self):
        root = make_repo({
            "src/a.py": "print('a')\n",
            "bin.dat": b"\x00\x00BIN",
            "node_modules/x.js": "junk\n",
        })
        outside = tempfile.mkdtemp()
        with open(os.path.join(outside, "secret.py"), "w") as fh:
            fh.write("leak\n")
        os.symlink(os.path.join(outside, "secret.py"),
                   os.path.join(root, "link.py"))
        listing = ("src/a.py\0bin.dat\0node_modules/x.js\0link.py\0"
                   "../escape.py\0/abs.py\0sub/../src/a.py\0missing.py\0")
        with patched(amb, subprocess=fake_git(listing)):
            files, skipped, used_git = amb.repo_walk(root)
        rels = [rel for rel, _full, _sz in files]
        self.assertEqual(rels, ["src/a.py"])
        self.assertTrue(used_git)
        self.assertGreaterEqual(skipped["binary"], 1)

    def test_per_file_cap_skips_oversized(self):
        root = make_repo({"small.py": "x = 1\n", "huge.py": "y = 2\n" * 100})
        with patched(amb, subprocess=no_git()):
            files, skipped, _g = amb.repo_walk(root, per_file_cap=100)
        rels = [rel for rel, _full, _sz in files]
        self.assertEqual(rels, ["small.py"])
        self.assertEqual(skipped["oversize"], 1)


class TestAuditRepo(unittest.TestCase):
    def _mr_repo(self):
        return make_repo({
            "src/a.py": "x = 1\n" * 8000,
            "src/b.py": "y = 2\n" * 8000,
            "src/c.py": "z = 3\n" * 8000,
            "img.dat": b"\x00\x01BINARY",
            "package-lock.json": '{"v": 3}\n',
            "node_modules/x.js": "junk\n",
        })

    def test_plan_reported_upfront_and_routed_through_map_reduce(self):
        catalog = repo_catalog()
        root = self._mr_repo()
        mr_calls = []
        gated = []

        def fake_mr(api_key, api_url, model, map_system, chunks, args,
                    *a, **k):
            mr_calls.append({"model": model, "chunks": chunks, "args": args,
                             "kwargs": k})
            return ('{"findings": [], "verdict": "SHIP"}', False, "")

        def boom(*a, **k):
            raise AssertionError("complete() must not run (no deep suspects)")

        args = audit_args(repo=root, format="report", parallel=5,
                          reduce_model="strong/reduce")
        err = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(), run_map_reduce=fake_mr,
                     complete=boom,
                     _gate_amount=lambda expected, *a, **k:
                         gated.append(expected)), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            amb.cmd_audit(args, KEY, "https://x", {})
        text = err.getvalue()
        self.assertIn("repo audit plan", text)
        self.assertIn("3 files", text)          # binary/lockfile/vendored out
        self.assertIn("chunk", text)            # strategy shown (no dollar cost)
        self.assertEqual(len(mr_calls), 1)
        self.assertTrue(gated)                   # cost gate fired
        call = mr_calls[0]
        self.assertEqual(call["kwargs"].get("reduce_model"), "strong/reduce")
        self.assertEqual(call["args"].parallel, 5)
        joined = "\n".join(call["chunks"])
        self.assertIn("src/a.py", joined)        # labels are repo-relative
        self.assertNotIn(root, joined)

    def test_json_emits_plan_then_standard_envelope(self):
        catalog = repo_catalog()
        root = self._mr_repo()

        def fake_mr(*a, **k):
            return ('{"findings": [{"file": "src/a.py", "line": 3, '
                    '"severity": "HIGH", "title": "bug", "scenario": "s"}],'
                    ' "verdict": "FIX FIRST"}', False, "")

        args = audit_args(repo=root, format="json", deep=False)
        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(), run_map_reduce=fake_mr,
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, KEY, "https://x", {})
        lines = out.getvalue().strip().splitlines()
        plan = json.loads(lines[0])
        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["status"], "plan")
        self.assertEqual(plan["kind"], "audit")
        self.assertEqual(plan["files"], 3)
        self.assertGreater(plan["chars"], 0)
        self.assertNotIn("est_cost", plan)   # no dollar figures (founder policy)
        env = json.loads("\n".join(lines[1:]))
        self.assertEqual(env["schema_version"], 1)
        self.assertEqual(env["kind"], "audit")
        self.assertEqual(env["verdict"], "FIX FIRST")
        self.assertEqual(len(env["findings"]), 1)

    def test_refuses_over_abs_max_without_override(self):
        catalog = repo_catalog()
        root = make_repo({"a.py": "x = 1\n" * 2000,
                          "b.py": "y = 2\n" * 2000})
        args = audit_args(repo=root, allow_cost=False)
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(), ABS_MAX_CHARS=10_000), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_audit(args, KEY, "https://x", {})
        code = cm.exception.code
        msg = code if isinstance(code, str) else ""
        self.assertTrue(isinstance(code, str) or code == 1)
        if msg:
            self.assertIn("ceiling", msg)

    def test_allow_partial_trims_to_cap_with_explicit_gap(self):
        catalog = repo_catalog()
        root = make_repo({"a.py": "x = 1\n" * 2000,
                          "b.py": "y = 2\n" * 2000})
        mr_calls = []

        def fake_mr(api_key, api_url, model, map_system, chunks, args,
                    *a, **k):
            mr_calls.append(chunks)
            return ('{"findings": [], "verdict": "SHIP"}', False, "")

        args = audit_args(repo=root, allow_cost=False, allow_partial=True,
                          format="report", deep=False)
        err = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(), ABS_MAX_CHARS=10_000,
                     run_map_reduce=fake_mr,
                     complete=lambda *a, **k: (
                         '{"findings": [], "verdict": "SHIP"}',
                         {}, {"finish_reason": "stop"}),
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            amb.cmd_audit(args, KEY, "https://x", {})
        self.assertIn("EXCLUDED", err.getvalue())

    def test_usage_errors(self):
        catalog = repo_catalog()
        # not a directory
        args = audit_args(repo="/nonexistent/dir/xyz")
        with patched(amb, safe_catalog=lambda *a, **k: catalog), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_audit(args, KEY, "https://x", {})
        self.assertEqual(cm.exception.code, amb.EXIT_USAGE)
        # --repo cannot combine with --staged
        root = make_repo({"a.py": "x = 1\n"})
        args = audit_args(repo=root, staged=True)
        with patched(amb, safe_catalog=lambda *a, **k: catalog), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_audit(args, KEY, "https://x", {})
        self.assertEqual(cm.exception.code, amb.EXIT_USAGE)
        # empty repo
        empty = tempfile.mkdtemp()
        args = audit_args(repo=empty)
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git()), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                amb.cmd_audit(args, KEY, "https://x", {})
        self.assertEqual(cm.exception.code, amb.EXIT_USAGE)

    def test_small_repo_single_shot_lane_no_deep(self):
        catalog = repo_catalog()
        root = make_repo({"a.py": "x = 1\n"})
        calls = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            calls.append(messages)
            return ('{"findings": [], "verdict": "SHIP"}', {},
                    {"finish_reason": "stop"})

        args = audit_args(repo=root, format="report", model="big/auditor")
        err = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(), complete=fake_complete,
                     log_usage=lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            amb.cmd_audit(args, KEY, "https://x", {})
        self.assertEqual(len(calls), 1)  # single shot; NO deep second pass
        self.assertIn("repo audit plan", err.getvalue())


CROSS_FILE_FINDING = {
    "file": "src/a.py", "line": 3, "severity": "HIGH",
    "title": "cross-file arity mismatch",
    "scenario": "src/a.py calls helper() defined in src/b.py with 2 args "
                "but src/b.py takes 3 — needs cross-file confirmation",
}


class TestDeepCrossFilePass(unittest.TestCase):
    def _repo(self):
        return make_repo({
            "src/a.py": "x = 1\n" * 8000,
            "src/b.py": "y = 2\n" * 8000,
            "src/c.py": "z = 3\n" * 8000,
        })

    def _pass1(self):
        return (json.dumps({"findings": [CROSS_FILE_FINDING],
                            "verdict": "FIX FIRST",
                            "_unparsed_chunks": 0, "_repaired_chunks": 0}),
                False, "")

    def test_suspect_collection_is_capped(self):
        paths = [f"src/f{i}.py" for i in range(20)]
        findings = [{"file": p, "scenario": f"{p} vs {paths[(i + 1) % 20]}"}
                    for i, p in enumerate(paths)]
        text = json.dumps({"findings": findings, "verdict": "NEEDS WORK"})
        suspects = amb.cross_file_suspects(text, paths)
        self.assertTrue(0 < len(suspects) <= 6)

    def test_deep_runs_exactly_one_gated_bounded_call(self):
        catalog = repo_catalog()
        root = self._repo()
        completes = []
        gates = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            completes.append(messages)
            return (json.dumps({"findings": [
                {"file": "src/b.py", "line": 1, "severity": "CRITICAL",
                 "title": "confirmed arity bug", "scenario": "confirmed"}],
                "verdict": "FIX FIRST"}), {}, {"finish_reason": "stop"})

        def spy_gate(catalog_, model_, input_chars, n_calls, args_, conf_):
            gates.append((input_chars, n_calls))

        args = audit_args(repo=root, format="json")  # deep defaults ON
        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(),
                     run_map_reduce=lambda *a, **k: self._pass1(),
                     complete=fake_complete, cost_gate=spy_gate,
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, KEY, "https://x", {})
        self.assertEqual(len(completes), 1)      # AT MOST one extra pass
        self.assertEqual(len(gates), 1)          # and it was gated
        self.assertEqual(gates[0][1], 1)         # one call
        lines = out.getvalue().strip().splitlines()
        env = json.loads("\n".join(lines[1:]))   # after the plan line
        titles = {f["title"] for f in env["findings"]}
        self.assertIn("cross-file arity mismatch", titles)
        self.assertIn("confirmed arity bug", titles)
        # bounded: suspects clipped to half a chunk (+ summary + overhead)
        profile = amb.model_profile(catalog, "tiny/auditor")
        cap = max(4000, min(profile.chunk_chars,
                            profile.single_shot_chars) // 2)
        user = completes[0][-1]["content"]
        self.assertLessEqual(len(user), cap + 20_000 + 2_000)

    def test_no_deep_opts_out(self):
        catalog = repo_catalog()
        root = self._repo()

        def boom(*a, **k):
            raise AssertionError("complete() must not run with --no-deep")

        args = audit_args(repo=root, format="json", deep=False)
        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(),
                     run_map_reduce=lambda *a, **k: self._pass1(),
                     complete=boom, _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, KEY, "https://x", {})
        lines = out.getvalue().strip().splitlines()
        env = json.loads("\n".join(lines[1:]))
        self.assertEqual(len(env["findings"]), 1)

    def test_deep_defaults_off_without_repo(self):
        catalog = repo_catalog()
        root = self._repo()

        def boom(*a, **k):
            raise AssertionError("no deep pass on a plain paths audit")

        # findings reference the exact labels, so a deep pass WOULD find
        # suspects if it were (wrongly) enabled by default off --repo
        a, b, c = (os.path.join(root, "src", n)
                   for n in ("a.py", "b.py", "c.py"))
        pass1 = (json.dumps({"findings": [dict(
            CROSS_FILE_FINDING, file=a,
            scenario=f"{a} calls helper() defined in {b} — cross-file")],
            "verdict": "FIX FIRST"}), False, "")
        args = audit_args(paths=[a, b, c], format="json")
        with patched(amb, safe_catalog=lambda *a2, **k: catalog,
                     run_map_reduce=lambda *a2, **k: pass1,
                     complete=boom, _gate_amount=lambda *a2, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, KEY, "https://x", {})

    def test_deep_flag_enables_on_plain_paths_audit(self):
        catalog = repo_catalog()
        root = self._repo()
        completes = []

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            completes.append(messages)
            return ('{"findings": [], "verdict": "SHIP"}', {},
                    {"finish_reason": "stop"})

        # findings reference repo-relative names, so pass matching labels
        a, b, c = (os.path.join(root, "src", n)
                   for n in ("a.py", "b.py", "c.py"))
        pass1 = (json.dumps({"findings": [dict(
            CROSS_FILE_FINDING, file=a,
            scenario=f"{a} calls helper() defined in {b} — cross-file")],
            "verdict": "FIX FIRST"}), False, "")
        args = audit_args(paths=[a, b, c], format="json", deep=True)
        with patched(amb, safe_catalog=lambda *a2, **k: catalog,
                     run_map_reduce=lambda *a2, **k: pass1,
                     complete=fake_complete, cost_gate=lambda *a2, **k: None,
                     _gate_amount=lambda *a2, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, KEY, "https://x", {})
        self.assertEqual(len(completes), 1)

    def test_unparseable_second_pass_keeps_pass1_result(self):
        catalog = repo_catalog()
        root = self._repo()

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            return ("total garbage, not json", {}, {"finish_reason": "stop"})

        args = audit_args(repo=root, format="json")
        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(),
                     run_map_reduce=lambda *a, **k: self._pass1(),
                     complete=fake_complete, cost_gate=lambda *a, **k: None,
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, KEY, "https://x", {})
        lines = out.getvalue().strip().splitlines()
        env = json.loads("\n".join(lines[1:]))
        self.assertEqual(env["findings"][0]["title"],
                         "cross-file arity mismatch")

    def test_partial_pass1_never_laundered_by_merge(self):
        catalog = repo_catalog()
        root = self._repo()
        pass1 = (json.dumps({"findings": [CROSS_FILE_FINDING],
                             "verdict": "NEEDS WORK",
                             "_unparsed_chunks": 1, "_repaired_chunks": 0}),
                 False, "")

        def fake_complete(api_key, api_url, model, messages, args, **kw):
            return ('{"findings": [], "verdict": "SHIP"}', {},
                    {"finish_reason": "stop"})

        args = audit_args(repo=root, format="json", allow_partial=True)
        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(),
                     run_map_reduce=lambda *a, **k: pass1,
                     complete=fake_complete, cost_gate=lambda *a, **k: None,
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, KEY, "https://x", {})
        lines = out.getvalue().strip().splitlines()
        env = json.loads("\n".join(lines[1:]))
        self.assertEqual(env["status"], "partial")
        self.assertFalse(env["coverage_complete"])


if __name__ == "__main__":
    unittest.main()
