#!/usr/bin/env bash
# PreToolUse(Bash) guardrail entrypoint: forward the hook's JSON stdin to the Python
# decision logic (pre-ambient.py). Kept as a thin wrapper so the hook's stdin (the tool
# payload) reaches Python intact. If python3 is missing, allow the call (fail open).
dir="$(cd "$(dirname "$0")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$dir/pre-ambient.py"
fi
exit 0
