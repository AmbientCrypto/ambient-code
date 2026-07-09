"""Hermetic REMEDIATION tests.

- C1: the multi-language signature regexes must be backtracking-safe — a
  400k-char pathological C line or a 100k all-spaces line must never hang
  build_code_map() (which runs on EVERY audit input); lines longer than
  SIG_SCAN_LINE_MAX are never regex-scanned at all.
- H1: a spend-gate refusal at the OPTIONAL deep cross-file pass must never
  discard the already-paid pass-1 result or emit an error envelope — the
  pass is skipped with a stderr note and pass-1 renders unchanged.
- H2: an intermediate symlinked directory pointing outside --repo root must
  be rejected (realpath containment), even when the leaf is a regular file.
- H3: the ABS_MAX_CHARS ceiling bounds the POST-GUTTER size (what is
  actually sent), not the raw byte sizes.
- the plan/gate input estimate includes the repo map injected into
  every chunk (map size x chunk count).
- --repo --consensus explicitly SKIPS the deep pass and says so;
  --deep/--no-deep are documented no-ops there.

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
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v7rem", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v7rem", loader)
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
    return [
        {"id": "tiny/auditor", "context_length": 16000,
         "max_output_length": 8000, "is_ready": True,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 0.2, "output": 0.8}},
        {"id": "strong/reduce", "context_length": 200000,
         "max_output_length": 65536, "is_ready": True,
         "supported_features": [], "output_modalities": ["text"],
         "pricing": {"input": 1.0, "output": 4.0}},
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
    root = root or tempfile.mkdtemp()
    for rel, content in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(full, mode) as fh:
            fh.write(content)
    return root


def no_git():
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


CROSS_FILE_FINDING = {
    "file": "src/a.py", "line": 3, "severity": "HIGH",
    "title": "cross-file arity mismatch",
    "scenario": "src/a.py calls helper() defined in src/b.py with 2 args "
                "but src/b.py takes 3 — needs cross-file confirmation",
}


# --------------------------------------------------------------------------
# C1 — ReDoS in the signature regexes
# --------------------------------------------------------------------------

class TestC1SignatureRegexSafety(unittest.TestCase):
    # Report the measured times so a remediation report can cite them.
    measured = {}

    def _timed(self, name, fn, budget=1.0):
        t0 = time.perf_counter()
        out = fn()
        dt = time.perf_counter() - t0
        type(self).measured[name] = dt
        self.assertLess(
            dt, budget,
            f"{name} took {dt:.3f}s — signature scan must be near-instant")
        return out

    def test_pathological_400k_c_line_is_fast(self):
        # Old pattern: [\w:<>\[\],\.\s]+ then \s+ then [\s\*&]+ all eat the
        # same whitespace → catastrophic backtracking on a long space run.
        evil = "int" + " " * 400_000 + "x = 1;\n"
        self._timed("400k_c_line",
                    lambda: amb.build_code_map([("evil.c", evil)]),
                    budget=0.3)

    def test_all_spaces_100k_line_is_fast(self):
        evil = " " * 100_000 + "\nclass Ok {}\n"
        m = self._timed("100k_spaces_line",
                        lambda: amb.build_code_map([("evil.java", evil)]),
                        budget=0.3)
        self.assertIn("Ok", m)  # the sane line is still extracted

    def test_adversarial_line_under_scan_cap_is_fast(self):
        # Below SIG_SCAN_LINE_MAX the regexes DO run — the rewritten
        # patterns themselves must be linear, not just capped away.
        lines = []
        for _ in range(100):
            lines.append("int" + " " * 4000 + "y")
            lines.append(" " * 4000)
            lines.append("foo bar" + " " * 3900 + "baz qux")
        text = "\n".join(lines)
        self._timed("4k_adversarial_x300",
                    lambda: amb._sigs_regex(text, "c"),
                    budget=0.3)

    def test_sig_scan_line_max_exists_and_caps_scanning(self):
        cap = amb.SIG_SCAN_LINE_MAX
        self.assertGreaterEqual(cap, 1024)
        self.assertLessEqual(cap, 16384)
        # a signature buried in an over-cap minified line is NOT scanned…
        minified = "function evilName(a){return a}" + "x" * (cap + 100)
        sigs = amb._sigs_regex(minified + "\n", "js")
        self.assertNotIn("function evilName", sigs)
        # …but a normal line still is.
        sigs = amb._sigs_regex("function goodName(a) {\n}\n", "js")
        self.assertTrue(any("goodName" in s for s in sigs), sigs)

    def test_long_line_still_counts_braces_for_depth(self):
        cap = amb.SIG_SCAN_LINE_MAX
        # An over-cap line that OPENS a brace pushes depth to 1: a later
        # column-0 function must still be seen (depth 1 allowed for c),
        # while depth tracking itself must not be skipped.
        text = ("void outer(void) {" + " " * (cap + 10) + "\n"
                "}\n"
                "int later_fn(int a) {\n"
                "    return a;\n"
                "}\n")
        sigs = amb._sigs_regex(text, "c")
        self.assertTrue(any("later_fn" in s for s in sigs), sigs)

    def test_c_family_signatures_still_extracted_after_rewrite(self):
        src = (
            "static int parse_args(int argc, char **argv) {\n"
            "    return 0;\n}\n"
            "std::vector<int> *get_items(void) {\n"
            "    return items;\n}\n"
            "unsigned long long count_bits(int x) {\n"
            "    return 1;\n}\n"
            "class Widget {\n"
            "    public void render(int w) {}\n"
            "}\n"
        )
        m = amb.build_code_map([("x.cpp", src)])
        for name in ("parse_args", "get_items", "count_bits",
                     "Widget", "render"):
            self.assertIn(name, m, m)
        self.assertNotIn("return", m)

    def test_no_whitespace_class_overlap_in_sig_patterns(self):
        # No character class in any signature pattern may include \s (or a
        # literal space) next to an adjacent \s quantifier — that overlap IS
        # the ReDoS (C1). Character classes with \w and \s together are the
        # canonical offender; ban them outright.
        for lang, pats in amb._SIG_PATTERNS.items():
            for _depth, rx, _tpl in pats:
                self.assertNotRegex(
                    rx.pattern, r"\[[^\]]*\\w[^\]]*\\s[^\]]*\]",
                    f"{lang}: {rx.pattern!r} mixes \\w and \\s in one class")
                self.assertNotRegex(
                    rx.pattern, r"\[[^\]]*\\s[^\]]*\\w[^\]]*\]",
                    f"{lang}: {rx.pattern!r} mixes \\s and \\w in one class")


# --------------------------------------------------------------------------
# H1 — deep-pass gate must never discard pass-1
# --------------------------------------------------------------------------

class TestH1DeepGateNonFatal(unittest.TestCase):
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

    def test_gate_refusal_keeps_pass1_and_emits_no_error_envelope(self):
        catalog = repo_catalog()
        root = self._repo()

        def refusing_gate(*a, **k):
            # Exactly what a real ceiling/fleet refusal does under --json:
            # prints an error envelope to stdout, then SystemExit.
            amb.emit_json_error("audit", "cost", "fleet ceiling hit", KEY)

        def boom(*a, **k):
            raise AssertionError("complete() must not run after a refusal")

        args = audit_args(repo=root, format="json")  # deep defaults ON
        out, err = io.StringIO(), io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(),
                     run_map_reduce=lambda *a, **k: self._pass1(),
                     complete=boom, cost_gate=refusing_gate,
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            amb.cmd_audit(args, KEY, "https://x", {})  # must NOT SystemExit
        lines = out.getvalue().strip().splitlines()
        # plan line + ONE audit envelope; no {"status": "error"} anywhere
        env = json.loads("\n".join(lines[1:]))
        self.assertNotEqual(env.get("status"), "error")
        self.assertEqual(env["findings"][0]["title"],
                         "cross-file arity mismatch")
        self.assertNotIn('"status": "error"', out.getvalue())
        self.assertIn("skip", err.getvalue().lower())

    def test_prose_gate_refusal_keeps_pass1(self):
        catalog = repo_catalog()
        root = self._repo()

        def refusing_gate(*a, **k):
            amb._fail_exit(None, "audit", "cost", "over the fleet ceiling")

        args = audit_args(repo=root, format="prose")
        out, err = io.StringIO(), io.StringIO()
        pass1 = ("pass-1 prose findings: src/a.py vs src/b.py — cross-file "
                 "arity mismatch (defined in src/b.py)", False, "")
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(),
                     run_map_reduce=lambda *a, **k: pass1,
                     complete=lambda *a, **k: (_ for _ in ()).throw(
                         AssertionError("no deep call after refusal")),
                     cost_gate=refusing_gate,
                     cost_gate_mr=lambda *a, **k: None,  # pass-1 gate allows
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            amb.cmd_audit(args, KEY, "https://x", {})
        self.assertIn("pass-1 prose findings", out.getvalue())
        self.assertIn("skip", err.getvalue().lower())

    def test_cost_gate_soft_true_on_allow_false_on_refusal(self):
        args = audit_args()
        with patched(amb, cost_gate=lambda *a, **k: None):
            self.assertTrue(amb.cost_gate_soft(
                [], "m", 1000, 1, args, {}))

        def refuse(*a, **k):
            amb.emit_json_error("audit", "cost", "nope", KEY)

        out = io.StringIO()
        with patched(amb, cost_gate=refuse), \
                contextlib.redirect_stdout(out):
            self.assertFalse(amb.cost_gate_soft(
                [], "m", 1000, 1, args, {}))
        self.assertEqual(out.getvalue(), "")  # envelope swallowed


# --------------------------------------------------------------------------
# H2 — intermediate symlink dir must not escape --repo
# --------------------------------------------------------------------------

class TestH2SymlinkDirContainment(unittest.TestCase):
    def test_intermediate_symlink_dir_is_rejected(self):
        root = make_repo({"src/a.py": "print('a')\n"})
        outside = tempfile.mkdtemp()
        with open(os.path.join(outside, "leak.py"), "w") as fh:
            fh.write("SECRET = 'leak'\n")
        os.symlink(outside, os.path.join(root, "linkdir"))
        # git lane: a listing can name a path whose INTERMEDIATE component
        # is a symlink out of root — the leaf lstat alone misses it.
        listing = "src/a.py\0linkdir/leak.py\0"
        with patched(amb, subprocess=fake_git(listing)):
            files, skipped, used_git = amb.repo_walk(root)
        rels = [rel for rel, _full, _sz in files]
        self.assertEqual(rels, ["src/a.py"])
        self.assertTrue(used_git)
        self.assertGreaterEqual(skipped["nonregular"], 1)
        for _rel, full, _sz in files:
            real = os.path.realpath(full)
            rroot = os.path.realpath(root)
            self.assertTrue(amb._within_root(real, rroot))

    def test_symlink_within_root_still_allowed_via_real_dir(self):
        # Containment must not reject ordinary in-root files.
        root = make_repo({"src/a.py": "print('a')\n",
                          "src/sub/b.py": "print('b')\n"})
        listing = "src/a.py\0src/sub/b.py\0"
        with patched(amb, subprocess=fake_git(listing)):
            files, _skipped, _g = amb.repo_walk(root)
        rels = [rel for rel, _full, _sz in files]
        self.assertEqual(rels, ["src/a.py", "src/sub/b.py"])


# --------------------------------------------------------------------------
# H3 — ceiling bounds the POST-GUTTER size
# --------------------------------------------------------------------------

class TestH3GutteredCeiling(unittest.TestCase):
    def test_refuses_when_only_guttered_size_exceeds_ceiling(self):
        catalog = repo_catalog()
        # raw 8,000 chars < 10,000 ceiling, but 4,000 lines of gutter
        # ("  42| ") add ~24k → the SENT size is far over the ceiling.
        root = make_repo({"a.py": "x\n" * 4000})
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

    def test_allow_partial_trims_on_guttered_sizes(self):
        catalog = repo_catalog()
        # a.py: raw 60k but guttered ~270k > the 250k ceiling; b.py tiny.
        # Raw-size accounting would keep BOTH with no gap note.
        root = make_repo({"a.py": "x\n" * 30_000, "b.py": "y = 1\n"})

        def fake_mr(api_key, api_url, model, map_system, chunks, args,
                    *a, **k):
            return ('{"findings": [], "verdict": "SHIP"}', False, "")

        args = audit_args(repo=root, allow_cost=False, allow_partial=True,
                          format="report", deep=False)
        err = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(), ABS_MAX_CHARS=250_000,
                     run_map_reduce=fake_mr,
                     complete=lambda *a, **k: (
                         '{"findings": [], "verdict": "SHIP"}',
                         {}, {"finish_reason": "stop"}),
                     log_usage=lambda *a, **k: None,
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            amb.cmd_audit(args, KEY, "https://x", {})
        self.assertIn("EXCLUDED", err.getvalue())
        self.assertIn("a.py", err.getvalue())  # names the trimmed file

    def test_within_ceiling_repo_untouched(self):
        catalog = repo_catalog()
        root = make_repo({"a.py": "x = 1\n" * 50})
        args = audit_args(repo=root, allow_cost=False)
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git()), \
                contextlib.redirect_stderr(io.StringIO()):
            labeled, meta = amb.repo_audit_inputs(args, KEY)
        self.assertEqual(meta["files"], 1)
        self.assertEqual(meta["omitted_over_cap"], 0)
        # meta chars reflect the guttered (actually sent) size
        sent = sum(len(t) for _, t in labeled)
        self.assertGreaterEqual(sent, meta["chars"] * 0.9)


# --------------------------------------------------------------------------
# M1 — cost estimate must include the injected repo map
# --------------------------------------------------------------------------

class TestM1MapInjectionPriced(unittest.TestCase):
    def _repo(self):
        return make_repo({
            "src/a.py": "x = 1\n" * 8000,
            "src/b.py": "y = 2\n" * 8000,
            "src/c.py": "z = 3\n" * 8000,
        })

    def test_live_gate_includes_map_times_chunks(self):
        catalog = repo_catalog()
        root = self._repo()
        seen = {}

        def spy_gate_mr(catalog_, model_, reduce_model_, input_chars,
                        n_chunks, args_, conf_, **k):
            seen["gate_chars"] = input_chars
            seen["gate_chunks"] = n_chunks

        def fake_mr(api_key, api_url, model, map_system, chunks, args,
                    *a, **k):
            seen["map"] = k.get("code_map") or ""
            seen["n"] = len(chunks)
            return ('{"findings": [], "verdict": "SHIP"}', False, "")

        args = audit_args(repo=root, format="report", deep=False)
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(), run_map_reduce=fake_mr,
                     cost_gate_mr=spy_gate_mr,
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, KEY, "https://x", {})
        with patched(amb, subprocess=no_git()), \
                contextlib.redirect_stderr(io.StringIO()):
            labeled, _meta = amb.repo_audit_inputs(
                audit_args(repo=root), KEY)
        total = sum(len(t) for _, t in labeled)
        self.assertTrue(seen["map"])
        self.assertEqual(seen["gate_chunks"], seen["n"])
        self.assertEqual(seen["gate_chars"],
                         total + len(seen["map"]) * seen["n"])

    def test_split_estimate_includes_map_times_chunks(self):
        catalog = repo_catalog()
        root = self._repo()
        with patched(amb, subprocess=no_git()), \
                contextlib.redirect_stderr(io.StringIO()):
            labeled, _meta = amb.repo_audit_inputs(
                audit_args(repo=root), KEY)
        total = sum(len(t) for _, t in labeled)
        profile = amb.model_profile(catalog, "tiny/auditor")
        seen = {}
        real_est = amb.estimate_cost_mr

        def spy_est(catalog_, model_, reduce_model_, input_chars,
                    n_chunks, max_tokens, **k):
            seen["est_chars"] = input_chars
            seen["n_chunks"] = n_chunks
            return real_est(catalog_, model_, reduce_model_, input_chars,
                            n_chunks, max_tokens, **k)

        with patched(amb, estimate_cost_mr=spy_est):
            n_chunks, _e, _b, _a = amb._audit_split_estimate(
                catalog, "tiny/auditor", "tiny/auditor", labeled, total,
                total, profile, 1.0, 4096, True)
        self.assertGreater(n_chunks, 1)
        map_len = len(amb.build_code_map(
            labeled, budget=amb.code_map_budget(profile.single_shot_chars)))
        self.assertGreater(map_len, 0)
        self.assertEqual(seen["est_chars"], total + map_len * n_chunks)


# --------------------------------------------------------------------------
# M2 — deep pass under --consensus: explicitly skipped, and says so
# --------------------------------------------------------------------------

class TestM2ConsensusDeepPolicy(unittest.TestCase):
    def _repo(self):
        return make_repo({
            "src/a.py": "x = 1\n" * 8000,
            "src/b.py": "y = 2\n" * 8000,
        })

    def test_consensus_repo_states_deep_skip(self):
        catalog = repo_catalog()
        root = self._repo()

        def boom(*a, **k):
            raise AssertionError("run_cross_file_pass must not run under "
                                 "--consensus")

        args = audit_args(repo=root, deep=True,
                          consensus="tiny/auditor,strong/reduce")
        err = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(),
                     run_one_audit=lambda *a, **k: ([], True),
                     run_cross_file_pass=boom,
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            amb.cmd_audit(args, KEY, "https://x", {})
        text = err.getvalue().lower()
        self.assertIn("consensus", text)
        self.assertIn("cross-file", text)
        self.assertTrue("skip" in text or "does not run" in text, text)

    def test_json_plan_reports_deep_false_under_consensus(self):
        catalog = repo_catalog()
        root = self._repo()
        args = audit_args(repo=root, deep=True, format="json",
                          consensus="tiny/auditor,strong/reduce")
        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(),
                     run_one_audit=lambda *a, **k: ([], True),
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, KEY, "https://x", {})
        plan = json.loads(out.getvalue().strip().splitlines()[0])
        self.assertEqual(plan["status"], "plan")
        self.assertIs(plan["deep"], False)

    def test_json_plan_reports_deep_true_on_plain_repo(self):
        catalog = repo_catalog()
        root = self._repo()

        def fake_mr(*a, **k):
            return ('{"findings": [], "verdict": "SHIP"}', False, "")

        args = audit_args(repo=root, format="json", deep=False)
        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(), run_map_reduce=fake_mr,
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, KEY, "https://x", {})
        plan = json.loads(out.getvalue().strip().splitlines()[0])
        self.assertIs(plan["deep"], False)  # --no-deep honored in the plan
        args = audit_args(repo=root, format="json")
        out = io.StringIO()
        with patched(amb, safe_catalog=lambda *a, **k: catalog,
                     subprocess=no_git(), run_map_reduce=fake_mr,
                     _gate_amount=lambda *a, **k: None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            amb.cmd_audit(args, KEY, "https://x", {})
        plan = json.loads(out.getvalue().strip().splitlines()[0])
        self.assertIs(plan["deep"], True)


if __name__ == "__main__":
    unittest.main()
