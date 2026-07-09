#!/usr/bin/env python3
"""Permanent stress harness — the stress test that started this remediation,
made repeatable so "passing the stress test" is a durable bar.

Two modes:
  * default (offline): drives the REAL code paths with recorded model outputs —
    no network, safe in CI. This is what asserts our fixes stay fixed.
  * --live: additionally hits the real Ambient network on both the default code
    model (Kimi) and GLM, skipping any model that isn't serving this minute
    (on-demand scaling is normal, not a failure).

Run:  python3 tests/stress/live_smoke.py            # offline, CI-safe
      python3 tests/stress/live_smoke.py --live     # + real Kimi + GLM

Each check maps to a stress-test finding (see
docs/plans/2026-07-06-stress-test-remediation.md).
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
BIN = os.path.join(ROOT, "bin", "ambient")

KIMI = "moonshotai/kimi-k2.7-code"
GLM = "z-ai/glm-5.2"

# A model whose recorded audit reply is correct PROSE but not JSON (GLM 5.2 on
# Ambient) — the exact shape that returned empty findings before the fix.
GLM_PROSE = (
    "HIGH (confidence: HIGH) — stats.py:10 — top_k slices s[0:k-1], one too few.\n"
    "Scenario: top_k([5,3,8,1],3) returns [8,5] not [8,5,3]. Fix: s[0:k].\n\n"
    "MEDIUM (confidence: HIGH) — stats.py:5 — average([]) divides by zero.\n"
    "Scenario: average([]) -> ZeroDivisionError. Fix: guard empty.\n\n"
    "Verdict: FIX FIRST.\n"
)
HI = "aQ7pR2xL9mZ4kT8vB1nC6wY3jD5sF0hG7uE2iO4a"  # synthetic high-entropy value


def _load():
    loader = importlib.machinery.SourceFileLoader("ambient_stress", BIN)
    spec = importlib.util.spec_from_loader("ambient_stress", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = _load()
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))


def offline_checks():
    print("OFFLINE (recorded outputs through real code paths):")

    # F01 — a schema-ignoring model's prose is recovered into findings + exit 0
    buf = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(buf):
        amb.render_findings(GLM_PROSE, "json", api_key="", model=GLM)
    env = json.loads(buf.getvalue())
    check("F01 prose audit recovers findings (not empty)",
          env["status"] == "ok" and len(env.get("findings") or []) >= 2)
    check("F01 recovered result reports exit 0", env["exit_code"] == 0)

    # F02 — an env-assigned secret in an arbitrarily-named file is refused,
    #        even gutter-prefixed (the live-leak shape)
    check("F02 env secret detected (plain)",
          amb._line_has_secret(f"AWS_SECRET_ACCESS_KEY={HI}"))
    check("F02 env secret detected (gutter-prefixed)",
          any(amb._line_has_secret(m.group(2)) if (m := __import__("re").match(
              r"^ *(\d+)\| ?(.*)$", ln)) else amb._line_has_secret(ln)
              for ln in [f"   1| AWS_SECRET_ACCESS_KEY={HI}"]))
    check("F02 public key is NOT a false positive",
          not amb._line_has_secret(f"PUBLIC_KEY={HI}"))

    # F03 — the stdin '-' sentinel survives the natural arg order
    import unittest.mock as mock
    with mock.patch.object(amb.sys, "argv",
                           ["ambient", "ask", "p", "-m", GLM, "-"]):
        args = amb._parse_args_with_stdin_dash(amb.build_parser())
    check("F03 stdin '-' survives 'ask p -m MODEL -'", "-" in args.prompt)

    # F05a — a mistyped model is a MODEL error, not opaque 'unknown'
    cat, _ = amb.classify_error(
        400, json.dumps({"error": {"message": "Unknown model: x"}}), "")
    check("F05a unknown-model 400 classifies as 'model'", cat == "model")

    # F05b — conservative title match never false-merges distinct findings
    check("F05b distinct findings never false-merge",
          not amb._titles_match(("sql", "injection", "in", "search"),
                                ("sql", "injection", "in", "login")))

    # F04 — catalog count dedupes the alias
    check("F04 alias id deduped from count",
          "zai-org/GLM-5.1-FP8" not in amb._dedupe_catalog_ids(
              ["ambient/large", "zai-org/GLM-5.1-FP8", KIMI]))


def _run(argv, stdin=None):
    return subprocess.run([sys.executable, BIN] + argv, input=stdin,
                          capture_output=True, text=True, timeout=180)


def live_checks():
    print("\nLIVE (real Ambient network; skips models not serving this minute):")
    env = dict(os.environ, AMBIENT_TELEMETRY="on")

    # Which models are serving right now?
    r = subprocess.run([sys.executable, BIN, "models", "--json"],
                       capture_output=True, text=True, env=env)
    try:
        serving = {m["id"] for m in json.loads(r.stdout)["models"]
                   if m["ready"] and not m["hidden"]}
    except Exception:
        serving = set()

    # F02 live — the tripwire fires BEFORE the network, so it needs no serving
    # model. Write a synthetic-secret file and confirm the refusal.
    creds = os.path.join(HERE, "_creds_probe.txt")
    with open(creds, "w") as fh:
        fh.write(f"AWS_SECRET_ACCESS_KEY={HI}\n")
    try:
        p = subprocess.run([sys.executable, BIN, "audit", creds, "-m", KIMI,
                            "--json"], capture_output=True, text=True, env=env)
        try:
            cat = json.loads(p.stdout).get("category")
        except Exception:
            cat = None
        check("F02 LIVE audit of creds file refused (category secrets)",
              cat == "secrets")
    finally:
        os.remove(creds)

    for model in (KIMI, GLM):
        if model not in serving:
            print(f"  [skip] {model} not serving this minute (on-demand)")
            continue
        p = subprocess.run([sys.executable, BIN, "audit",
                            os.path.join(HERE, "fixtures_stats.py"), "-m", model,
                            "--json", "--no-cache"], capture_output=True,
                           text=True, env=env)
        try:
            d = json.loads(p.stdout)
            ok = d.get("status") == "ok" and len(d.get("findings") or []) >= 1
        except Exception:
            ok = False
        check(f"F01 LIVE {model} audit returns findings", ok)


def main():
    live = "--live" in sys.argv
    if live:
        # write the planted-bug fixture the live audit points at
        with open(os.path.join(HERE, "fixtures_stats.py"), "w") as fh:
            fh.write("def average(nums):\n    return sum(nums)/len(nums)\n\n"
                     "def top_k(nums,k):\n    return sorted(nums,reverse=True)[0:k-1]\n")
    offline_checks()
    if live:
        live_checks()
        os.remove(os.path.join(HERE, "fixtures_stats.py"))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
        sys.exit(1)
    print("STRESS TEST PASSED")


if __name__ == "__main__":
    main()
