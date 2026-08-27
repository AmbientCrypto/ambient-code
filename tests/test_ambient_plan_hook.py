"""the PreToolUse plan-mode guardrail (hooks/pre-ambient.py)."""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "hooks", "pre-ambient.py")


def run(payload):
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                       capture_output=True, text=True)
    out = p.stdout.strip()
    decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None
    return decision, p.returncode


class TestGuardrail(unittest.TestCase):
    def _cmd(self, cmd, mode="plan", tool="Bash"):
        return {"tool_name": tool, "permission_mode": mode, "tool_input": {"command": cmd}}

    def test_plan_mutating_denied(self):
        for cmd in ("ambient build x --apply", "ambient claude", "ambient code x",
                    "ambient agent run y", "ambient serve", "ambient setup",
                    "ambient use kimi", "ambient mode on", "ambient mode takeover"):
            decision, rc = run(self._cmd(cmd))
            self.assertEqual(decision, "deny", "should deny in plan mode: %r" % cmd)
            self.assertEqual(rc, 0)

    def test_plan_readonly_allowed(self):
        for cmd in ("ambient ask hi", "git diff | ambient audit --json",
                    "ambient models --json", "ambient doctor", "ambient usage",
                    "ambient mode", "ambient mode off"):
            decision, _ = run(self._cmd(cmd))
            self.assertIsNone(decision, "should allow in plan mode: %r" % cmd)

    def test_non_plan_allows_mutating(self):
        decision, _ = run(self._cmd("ambient build x --apply", mode="default"))
        self.assertIsNone(decision)
        decision, _ = run(self._cmd("ambient claude", mode="acceptEdits"))
        self.assertIsNone(decision)

    def test_non_bash_and_non_ambient_allowed(self):
        self.assertIsNone(run(self._cmd("ambient build x", tool="Edit"))[0])
        self.assertIsNone(run(self._cmd("npm run build"))[0])

    def test_unparseable_fails_open(self):
        p = subprocess.run([sys.executable, HOOK], input="not json",
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")

    def test_path_prefixed_ambient_still_caught(self):
        decision, _ = run(self._cmd("~/.local/bin/ambient build x --apply"))
        self.assertEqual(decision, "deny")

    def test_env_prefixed_ambient_caught(self):
        decision, _ = run(self._cmd("AMBIENT_MAX_SPEND=1 ambient build x --apply"))
        self.assertEqual(decision, "deny")


class TestConditionalMutation(unittest.TestCase):
    def _cmd(self, cmd):
        return {"tool_name": "Bash", "permission_mode": "plan", "tool_input": {"command": cmd}}

    def test_conditionally_denied(self):
        for cmd in ("ambient build x --apply", "ambient settings set streaming on",
                    "ambient config set fallback on", "ambient curate hide qwen/*",
                    "ambient cache clear", "git diff | ambient audit --install-hook",
                    "ambient mode takeover"):
            self.assertEqual(run(self._cmd(cmd))[0], "deny", "should deny: %r" % cmd)

    def test_conditionally_allowed(self):
        for cmd in ("ambient build x --dry-run", "ambient curate status", "ambient curate",
                    "ambient settings", "ambient config", "git diff | ambient audit --json",
                    "ambient mode off", "echo ambient build", "echo 'ambient claude'"):
            self.assertIsNone(run(self._cmd(cmd))[0], "should allow: %r" % cmd)


if __name__ == "__main__":
    unittest.main()
