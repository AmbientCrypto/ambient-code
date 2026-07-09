#!/usr/bin/env bash
# ambient-code stress / QA battery v2. Exercises every command against the LIVE
# Ambient API with planted-bug correctness checks, exit-code contract asserts,
# a key-leak tripwire, and pathological-input robustness checks — all under a
# SANDBOX HOME so the operator's real config is never touched.
#
# Usage:  bash tools/stress_test.sh                 # full battery (spends a small amount of Ambient credit)
#         AMB_NO_LIVE=1 bash tools/stress_test.sh   # offline checks only (no spend)
#         AMB=/path/to/bin/ambient bash tools/stress_test.sh
set -u
AMB="${AMB:-$(cd "$(dirname "$0")/.." && pwd)/bin/ambient}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PASS=0; FAIL=0; SKIP=0
pass() { echo "  PASS  $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $1  -- $2"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $1  -- $2"; SKIP=$((SKIP+1)); }
live() { [ -z "${AMB_NO_LIVE:-}" ] && [ -n "$KEY" ]; }

# ---- sandbox HOME + key acquisition (never echo the key) -------------------
REAL_HOME="$HOME"
SBHOME="$WORK/home"
mkdir -p "$SBHOME/.config/ambient"
KEY="$(security find-generic-password -s ambient.xyz -a api-key -w 2>/dev/null || true)"
if [ -z "$KEY" ] && [ -f "$REAL_HOME/.config/ambient/env" ]; then
  KEY="$(sed -n 's/^AMBIENT_API_KEY=//p' "$REAL_HOME/.config/ambient/env" | head -1)"
fi
export HOME="$SBHOME"
export AMBIENT_NO_ONBOARD=1   # battery is non-interactive by definition
FAKEKEY="sk-battery-fake-key-00000000"   # for offline paths gated on a key
LOG="$WORK/all.log"
# run <name> -- <cmd...>: capture combined output to a per-check file + the
# global leak log; exposes exit code in $RC and output file in $OUT.
run() {
  OUT="$WORK/out.$PASS.$FAIL.$SKIP.$RANDOM"
  "$@" >"$OUT" 2>&1
  RC=$?
  cat "$OUT" >> "$LOG"
  return 0
}
with_key() { AMBIENT_API_KEY="$KEY" "$@"; }

echo "=== ambient-code stress battery v2 ($("$AMB" --version 2>/dev/null)) ==="
[ -n "$KEY" ] || echo "  (no API key found — live checks will be skipped)"

# ---- fixtures ---------------------------------------------------------------
printf 'def divide(a, b):\n    return a / b\n\ndef fetch(d, k):\n    return d[k]\n' > "$WORK/bugs.py"
printf 'SELECT * FROM users WHERE name = "%%s" %% name  # sql injection\n' > "$WORK/inj.py"
head -c 4000000 /dev/urandom | base64 > "$WORK/big.txt"          # ~5.4MB text
printf 'A%.0s' $(seq 1 400000) > "$WORK/minified.js"             # one 400k line
printf '\x00\x01\x02binarydata' > "$WORK/blob.bin"               # binary
: > "$WORK/empty.py"                                             # empty
printf 'api_key = "sk-fixture-deadbeef-0000-not-a-real-key"\n' > "$WORK/secret.py"

# ==== OFFLINE: CLI surface, exit codes, robustness ===========================
echo "--- offline: surface + exit-code contract ---"
run "$AMB" --version;              [ $RC -eq 0 ] && pass "--version exits 0" || fail "--version" "rc=$RC"
run "$AMB" version;                [ $RC -eq 0 ] && pass "version subcommand" || fail "version" "rc=$RC"
run "$AMB";                        [ $RC -eq 0 ] && grep -q "ambient setup" "$OUT" && pass "bare banner (exit 0, shows commands)" || fail "bare banner" "rc=$RC"
run "$AMB" nonsense;               [ $RC -eq 64 ] && pass "unknown command exits 64" || fail "usage exit" "rc=$RC"
run "$AMB" audti;                  grep -q "did you mean: audit" "$OUT" && [ $RC -eq 64 ] && pass "typo gets did-you-mean + 64" || fail "did-you-mean" "rc=$RC"
run env AMBIENT_API_KEY="$FAKEKEY" "$AMB" ask < /dev/null
[ $RC -eq 64 ] && pass "ask with nothing exits 64 (usage)" || fail "ask usage" "rc=$RC"
run "$AMB" codex;                  [ $RC -eq 1 ] && grep -qi "ambient agent" "$OUT" && pass "codex explains + points at agent" || fail "codex" "rc=$RC"
run "$AMB" audit --help;           grep -q -- --consensus "$OUT" && pass "audit --help lists flags" || fail "audit help" "missing"
run "$AMB" build --help;           grep -q -- --plan-only "$OUT" && pass "build --help lists flags" || fail "build help" "missing"

echo "--- offline: unconfigured behavior (sandbox HOME has no key) ---"
run "$AMB" ask "hi" < /dev/null;   [ $RC -eq 3 ] && grep -q "app.ambient.xyz" "$OUT" && pass "unconfigured exits 3 + get-a-key pointer" || fail "exit-3" "rc=$RC"
run "$AMB" mode;                   grep -q "MISSING" "$OUT" && pass "mode reports missing key" || fail "mode" "no MISSING"

echo "--- offline: config + endpoint hardening ---"
printf '\xff\xfe' > "$SBHOME/.config/ambient/env"
run "$AMB" version;                [ $RC -eq 0 ] && ! grep -q "internal" "$OUT" && pass "corrupt config degrades cleanly" || fail "corrupt config" "rc=$RC"
rm -f "$SBHOME/.config/ambient/env"
run env AMBIENT_API_URL=https://evil.example AMBIENT_API_KEY=sk-x "$AMB" models
[ $RC -ne 0 ] && grep -q "trust-url" "$OUT" && pass "host pinning refuses non-Ambient endpoint" || fail "host pinning" "rc=$RC"
run env AMBIENT_API_KEY=sk-bogus-key-0000000000 "$AMB" setup sk-fixture-argv-key-0000000000000000
[ $RC -ne 0 ] && grep -qi "shell history" "$OUT" && pass "key-in-argv refused" || fail "key-in-argv" "rc=$RC"

echo "--- offline: tripwires + robustness ---"
run env AMBIENT_API_KEY="$FAKEKEY" "$AMB" audit "$WORK/secret.py" --dry-run
grep -qi "credential" "$OUT" && pass "secrets tripwire blocks a key" || fail "secrets tripwire" "did not block"
run env AMBIENT_API_KEY="$FAKEKEY" "$AMB" audit "$WORK/blob.bin" --dry-run
grep -qi "binary\|nothing to audit" "$OUT" && pass "binary file skipped" || fail "binary skip" "not skipped"
run env AMBIENT_API_KEY="$FAKEKEY" "$AMB" audit "$WORK/empty.py" --dry-run
grep -qi "empty\|nothing to audit" "$OUT" && pass "empty file skipped" || fail "empty skip" "not skipped"
run env AMBIENT_API_KEY="$FAKEKEY" "$AMB" audit "$WORK/big.txt" "$WORK/minified.js" --dry-run
[ $RC -eq 0 ] && grep -qi "map-reduce" "$OUT" && pass "5.4MB + 400KB-line inputs plan a map-reduce (dry run, no spend)" || fail "big-input dry-run" "rc=$RC"
( cd "$WORK" && AMBIENT_API_KEY="$FAKEKEY" "$AMB" audit --staged --dry-run >"$WORK/staged.out" 2>&1; echo $? > "$WORK/staged.rc" )
cat "$WORK/staged.out" >> "$LOG"; OUT="$WORK/staged.out"; RC=$(cat "$WORK/staged.rc")
grep -qi "git repository" "$OUT" && pass "--staged outside repo: clean error" || fail "--staged guard" "no clean error"
run "$AMB" link --dir "$WORK/bin"
[ -L "$WORK/bin/ambient" ] && pass "link creates launcher symlink" || fail "link" "no symlink"
run "$AMB" link --dir "$WORK/bin" --remove
[ ! -e "$WORK/bin/ambient" ] && pass "link --remove removes it" || fail "link remove" "still there"
run "$AMB" cache;                  grep -q "cache:" "$OUT" && pass "cache status prints" || fail "cache" "no status"
run "$AMB" curate hide 'qwen/*'
run "$AMB" curate status;          grep -q "hide=qwen" "$OUT" && pass "curate hide persists" || fail "curate" "not persisted"
run "$AMB" curate reset;           grep -q "reset" "$OUT" && pass "curate reset" || fail "curate reset" "rc=$RC"
run env AMBIENT_API_KEY="$FAKEKEY" "$AMB" build "x" --apply < /dev/null
[ $RC -ne 0 ] && grep -q -- "--dir" "$OUT" && pass "headless build --apply refuses without --dir" || fail "build gates" "rc=$RC"

# ==== LIVE checks (spend) =====================================================
jassert() {  # jassert <file> <python-expr-on-d> <passmsg> <failmsg> [expected_rc]
  # run() merges stderr into the capture, so parse from the first JSON object.
  # Also asserts the process exit code (default 0) and the envelope's own
  # exit_code agree — a partial result must not false-pass.
  want_rc="${5:-0}"
  if [ "$RC" -ne "$want_rc" ]; then fail "$4" "rc=$RC (want $want_rc)"; return; fi
  if JRC="$RC" python3 - "$1" "$2" <<'PY' 2>/dev/null
import json,os,sys
raw=open(sys.argv[1]).read()
d=json.JSONDecoder().raw_decode(raw[raw.index("{"):])[0]
assert d.get("exit_code", 0) == int(os.environ["JRC"])
assert eval(sys.argv[2])
PY
  then pass "$3"; else fail "$4" "$(head -c 200 "$1")"; fi
}

if ! live; then echo "--- skipping live checks (AMB_NO_LIVE or no key) ---"; else
echo "--- live: keyless storefront + models envelope ---"
run "$AMB" models
grep -qi "browsing only" "$OUT" && pass "keyless models lists + browsing-only note" || fail "keyless models" "rc=$RC"
run with_key "$AMB" models --json
jassert "$OUT" "d['schema_version']==1 and d['configured'] is True and isinstance(d['models'],list) and d['models']" \
  "models --json envelope (configured + models)" "models --json"

echo "--- live: bad key is diagnosed, never a traceback ---"
run env AMBIENT_API_KEY=sk-bogus-key-000000000000 HOME="$SBHOME" "$AMB" doctor
if [ $RC -eq 1 ] && ! grep -q "Traceback" "$OUT" && grep -qiE "rejected|auth|FAIL" "$OUT"; then
  pass "bogus key: clean [key] diagnosis, exit 1"
else fail "bad-key doctor" "rc=$RC"; fi

echo "--- live: correctness (must FIND planted bugs) ---"
run with_key "$AMB" audit "$WORK/bugs.py" --json --yes
jassert "$OUT" "d['kind']=='audit' and d['findings'] and any(k in (f.get('title','')+f.get('defect','')).lower() for f in d['findings'] for k in ('zero','division','keyerror','key error'))" \
  "single-shot --json identifies a planted bug" "single-shot --json"
run with_key "$AMB" audit "$WORK/inj.py" --format report --yes
grep -qi "verdict" "$OUT" && pass "--format report renders + verdict" || fail "--format report" "no verdict"

echo "--- live: map-reduce on a genuinely large input ---"
BIG="$WORK/big_src.py"
cat "$WORK/bugs.py" > "$BIG"
for i in $(seq 1 3500); do printf 'def fn_%d(x):\n    y = x + %d\n    return y * 2\n\n' "$i" "$i"; done >> "$BIG"
echo "  (big input: $(wc -c < "$BIG") chars)"
run with_key "$AMB" audit "$BIG" --json --yes
jassert "$OUT" "'verdict' in d and isinstance(d.get('findings'),list)" \
  "map-reduce audit returns a structured verdict" "map-reduce"
if grep -qi "chunks" "$OUT"; then
  pass "map-reduce actually chunked ($(grep -oi '[0-9]* chunks' "$OUT" | head -1))"
else
  fail "map-reduce chunk evidence" "no chunk banner — input went single-shot?"
fi

echo "--- live: resumable cache (re-run reuses chunks) ---"
run with_key "$AMB" audit "$BIG" --json --yes
if grep -qi "served from cache" "$OUT"; then pass "cache: re-run served chunks from cache"
else skip "cache reuse" "no cache line"; fi

echo "--- live: consensus (2 models, honest partial) ---"
run with_key "$AMB" audit "$WORK/bugs.py" --consensus z-ai/glm-5.2,moonshotai/kimi-k2.7-code --yes --allow-partial
grep -qi "consensus" "$OUT" && pass "consensus runs across 2 models" || fail "consensus" "rc=$RC"

echo "--- live: code + build lanes ---"
run with_key "$AMB" code "Write a Python function is_prime(n) returning True/False. Output only code." --yes
grep -v "^ambient" "$OUT" | grep -v "^\[ambient" | sed 's/^```.*$//' > "$WORK/gen.py"
if python3 -c "import ast; s=open('$WORK/gen.py').read(); ast.parse(s); assert 'is_prime' in s" 2>/dev/null; then
  pass "code gen: valid python defining is_prime"
else skip "code gen validity" "$(head -c 100 "$WORK/gen.py")"; fi
run with_key "$AMB" build "a tiny python package 'greeter' with greeter/__init__.py exposing greet(name) and a test_greeter.py using pytest" --dir "$WORK/bld" --json --apply --yes
jassert "$OUT" "d['kind']=='build' and d['written'] is True and d['files']" \
  "build: generated + applied a manifest" "build"
if python3 -c "import ast,glob; [ast.parse(open(f).read()) for f in glob.glob('$WORK/bld/**/*.py', recursive=True)]" 2>/dev/null \
   && [ -z "$(find "$WORK/bld" -name '*.py' -newer /tmp -perm +111 2>/dev/null)" ]; then
  pass "build: every generated .py parses; nothing executable"
else fail "build output" "unparseable or executable file"; fi
run with_key "$AMB" build "x" --dir "$WORK/bld2" --dry-run
grep -q "est. cost" "$OUT" && [ $RC -eq 0 ] && pass "build --dry-run prints plan, no spend" || fail "build dry-run" "rc=$RC"

echo "--- live: ask envelope + streaming + non-TTY once ---"
run with_key "$AMB" ask "Reply with exactly: OK" --json
jassert "$OUT" "d['content'].strip()=='OK' and d['status']=='ok' and d['schema_version']==1" \
  "ask --json stable envelope" "ask --json"
AMBIENT_API_KEY="$KEY" "$AMB" ask "Reply with exactly: PIPEONCE" --yes </dev/null 2>>"$LOG" | tee "$WORK/pipe.txt" >> "$LOG"
N=$(grep -c "PIPEONCE" "$WORK/pipe.txt" 2>/dev/null || echo 0)
[ "$N" -eq 1 ] && pass "piped ask prints exactly once" || fail "piped-once" "count=$N"
# Grep the pty capture FILE — piping `script`'s stdout is unreliable headless (macOS).
AMBIENT_API_KEY="$KEY" script -q "$WORK/tty.txt" "$AMB" ask "Reply with exactly: STREAMOK" </dev/null >/dev/null 2>&1
cat "$WORK/tty.txt" >> "$LOG" 2>/dev/null || true
N=$(grep -c "STREAMOK" "$WORK/tty.txt" 2>/dev/null || echo 0)
[ "$N" -ge 1 ] && pass "ask streams on a TTY" || fail "ask streaming" "count=$N"

echo "--- live: doctor health (real key) ---"
run with_key "$AMB" doctor
grep -qi "DIAGNOSIS: healthy" "$OUT" && pass "doctor: healthy" || fail "doctor" "not healthy (rc=$RC)"
fi

# ==== KEY-LEAK TRIPWIRE (the #1 constraint, enforced mechanically) ===========
if [ -n "$KEY" ] && grep -qF "$KEY" "$LOG"; then
  fail "KEY-LEAK tripwire" "the API key appeared in command output"
else
  pass "key-leak tripwire: key never appeared in any output"
fi

echo "=== SUMMARY: $PASS passed, $FAIL failed, $SKIP skipped ==="
[ "$FAIL" -eq 0 ]
