#!/usr/bin/env python3
"""PreToolUse(Bash) guardrail: in PLAN mode, deny MUTATING `ambient` subcommands.

A best-effort BACKSTOP to the SKILL's plan-mode contract (the contract is primary; this
catches the obvious case). Reads the hook JSON on stdin. FAILS OPEN — an unparseable
payload, a non-Bash tool, a build that doesn't expose the permission mode, or a read-only
ambient action all ALLOW.

It only treats `ambient` as MUTATING when it is the COMMAND of a segment (segment start,
after any leading `VAR=val`, allowing a path prefix) — so `echo ambient build` is allowed —
and applies per-subcommand logic so read-only forms (`build --dry-run`, `curate status`,
`settings` status, `audit --json`, `mode`/`mode off`) are NOT denied. It does not parse
inside `bash -c '…'`; the SKILL contract covers that.
"""
import json
import re
import sys

# Always writes / spawns a process / rewrites config.
_ALWAYS = frozenset({"code", "agent", "serve", "claude", "setup", "uninstall", "link", "use"})
_SEP = re.compile(r"\|\||&&|[;&|\n]")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")


def _is_mutating(sub, args):
    if sub in _ALWAYS:
        return True
    if sub == "build":
        return "--dry-run" not in args          # a plan/dry-run writes nothing
    if sub == "audit":
        return "--install-hook" in args or "--uninstall-hook" in args
    if sub in ("settings", "config"):
        return bool(args) and args[0] in ("set", "unset")
    if sub == "curate":
        return bool(args) and args[0] in ("hide", "show", "only", "note", "reset")
    if sub == "mode":
        return bool(args) and args[0] in ("on", "takeover")   # `mode off`/status are safe
    if sub == "cache":
        return bool(args) and args[0] == "clear"
    return False


def _invocations(cmd):
    for seg in _SEP.split(cmd):
        toks = seg.strip().split()
        i = 0
        while i < len(toks) and _ENV_ASSIGN.match(toks[i]):
            i += 1
        if i >= len(toks) or toks[i].rsplit("/", 1)[-1] != "ambient":
            continue
        rest = toks[i + 1:]
        yield (rest[0] if rest else ""), rest[1:]


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return 0
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return 0
    if (data.get("permission_mode") or data.get("permissionMode") or "") != "plan":
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if not isinstance(cmd, str) or "ambient" not in cmd:
        return 0

    for sub, args in _invocations(cmd):
        if _is_mutating(sub, args):
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "`ambient {0}` changes state (writes files / spawns a process / "
                    "rewrites config). In plan mode, add it to the plan and run it after "
                    "approval.".format(sub))}}))
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
