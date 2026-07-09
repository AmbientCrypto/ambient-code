"""Polish + hardening (post-review hardening).

Pins the two NEW behaviors this batch introduced:

1. `ambient agent` spend disclosure at LAUNCH (S1 MED): the opencode lane is
   billed by Ambient directly and bypasses local metering AND the
   AMBIENT_MAX_SPEND fleet ceiling — that must be said on stderr BEFORE the
   process hands off to opencode, not only at the end of `ambient usage`.

2. hooks/session-start.sh launcher self-heal scoping (S2 LOW): the hook heals
   $HOME/.local/bin/ambient ONLY — and only when it is a SYMLINK that is
   either dangling (plugin update GC'd the old versioned dir) or a stale
   ambient launcher (target exists, basename `ambient`, but not the ACTIVE
   install). It must NEVER clobber a real (non-symlink) file or a foreign
   symlink, and it must not create anything when nothing is there. No
   PATH-wide healing: a foreign tool coincidentally named `ambient` elsewhere
   is out of scope by design.

No network, no live API, no writes outside tempdirs (the hook runs against a
throwaway $HOME).
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")
HOOK = os.path.join(ROOT, "hooks", "session-start.sh")

KEY = "sk-test-key-abcdef1234567890"

DISCLOSURE = ("billed by Ambient directly — its spend is NOT covered by "
              "local metering or AMBIENT_MAX_SPEND")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_v21", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_v21", loader)
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


class _Handoff(Exception):
    """Raised by the fake execvpe so the test regains control."""


class TestAgentSpendDisclosure(unittest.TestCase):
    """S1: cmd_agent tells the user AT LAUNCH that this lane is unmetered."""

    def _run_agent(self):
        record = {}
        err = io.StringIO()

        def fake_execvpe(prog, argv, env):
            # Snapshot stderr AT the handoff — anything printed later would
            # never be seen (execvpe replaces the process image).
            record["prog"] = prog
            record["argv"] = argv
            record["stderr_at_handoff"] = err.getvalue()
            raise _Handoff()

        def fake_run(cmd, **kw):
            # Windows path: cmd_agent runs opencode as a child (subprocess.run)
            # instead of execvpe. Record the same way (basename, so a which()-
            # resolved absolute path still reads as "opencode").
            record["prog"] = os.path.basename(cmd[0])
            record["argv"] = cmd
            record["stderr_at_handoff"] = err.getvalue()
            raise _Handoff()

        args = argparse.Namespace(model="deepseek-ai/DeepSeek-V3",
                                  agent_args=[])
        with tempfile.TemporaryDirectory() as tmp:
            with patched(amb, OPENCODE_CONFIG_PATH=os.path.join(
                    tmp, "opencode.json")), \
                 patched(amb.shutil, which=lambda name:
                         "/usr/bin/opencode" if name == "opencode" else None), \
                 patched(amb.os, execvpe=fake_execvpe), \
                 patched(amb.subprocess, run=fake_run), \
                 contextlib.redirect_stderr(err):
                with self.assertRaises(_Handoff):
                    amb.cmd_agent(args, KEY, "https://api.example.invalid",
                                  {})
        return record, err.getvalue()

    def test_disclosure_printed_before_handoff(self):
        record, _ = self._run_agent()
        self.assertEqual(record["prog"], "opencode")
        self.assertIn(DISCLOSURE, record["stderr_at_handoff"])

    def test_disclosure_sits_next_to_the_secrets_tripwire_warning(self):
        record, _ = self._run_agent()
        pre = record["stderr_at_handoff"]
        self.assertIn("secrets tripwire", pre)
        # Both launch warnings are single ambient:-prefixed stderr lines.
        lines = [ln for ln in pre.splitlines() if DISCLOSURE in ln]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("ambient: "))


@unittest.skipIf(os.name == "nt",
                 "SessionStart self-heal is POSIX-sh + symlinks; Windows "
                 "installs use the ambient.cmd shim path instead")
class TestSessionStartSelfHeal(unittest.TestCase):
    """S2: the hook heals ONLY $HOME/.local/bin/ambient, only when safe."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # realpath: on macOS tempdirs may sit behind symlinked parents, and
        # the CLI's `link` resolves its own path — compare apples to apples.
        base = os.path.realpath(self._tmp.name)
        self.home = os.path.join(base, "home")
        self.root = os.path.join(base, "plugin-cache", "ambient-code", "2.0.0")
        os.makedirs(os.path.join(self.home, ".local", "bin"))
        os.makedirs(os.path.join(self.root, "bin"))
        self.active = os.path.join(self.root, "bin", "ambient")
        shutil.copyfile(BIN, self.active)
        os.chmod(self.active, os.stat(self.active).st_mode
                 | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.link = os.path.join(self.home, ".local", "bin", "ambient")

    def _run_hook(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("AMBIENT_")}
        env["HOME"] = self.home
        env["CLAUDE_PLUGIN_ROOT"] = self.root
        return subprocess.run(["sh", HOOK], env=env, capture_output=True,
                              text=True, timeout=60)

    def _assert_silent_ok(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")

    def test_dangling_symlink_is_relinked_to_a_working_launcher(self):
        # The post-GC scenario the hook header describes: the old versioned
        # install dir is gone, the launcher dangles.
        gone = os.path.join(os.path.dirname(self.root), "1.9.0", "bin",
                            "ambient")
        os.symlink(gone, self.link)
        self.assertTrue(os.path.islink(self.link))
        self.assertFalse(os.path.exists(self.link))  # dangling
        proc = self._run_hook()
        self._assert_silent_ok(proc)
        self.assertTrue(os.path.islink(self.link))
        self.assertEqual(os.path.realpath(self.link), self.active)
        self.assertTrue(os.path.exists(self.link))  # works again

    def test_stale_but_existing_launcher_is_repointed_to_active_root(self):
        old_root = os.path.join(os.path.dirname(self.root), "1.9.0", "bin")
        os.makedirs(old_root)
        old = os.path.join(old_root, "ambient")
        shutil.copyfile(BIN, old)
        os.chmod(old, 0o755)
        os.symlink(old, self.link)
        proc = self._run_hook()
        self._assert_silent_ok(proc)
        self.assertTrue(os.path.islink(self.link))
        self.assertEqual(os.path.realpath(self.link), self.active)

    def test_healing_is_idempotent(self):
        os.symlink(self.active, self.link)
        before = os.lstat(self.link).st_ino
        proc = self._run_hook()
        self._assert_silent_ok(proc)
        self.assertEqual(os.lstat(self.link).st_ino, before)  # untouched
        self.assertEqual(os.path.realpath(self.link), self.active)

    def test_real_file_is_never_clobbered(self):
        payload = "#!/bin/sh\necho this is the user's own ambient\n"
        with open(self.link, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.chmod(self.link, 0o755)
        proc = self._run_hook()
        self._assert_silent_ok(proc)
        self.assertFalse(os.path.islink(self.link))
        with open(self.link, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), payload)

    def test_foreign_symlink_is_never_repointed(self):
        # An EXISTING target that is clearly not an ambient-plugin launcher
        # (basename != ambient) must be left exactly as the user set it.
        foreign_dir = os.path.join(self.home, "tools")
        os.makedirs(foreign_dir)
        foreign = os.path.join(foreign_dir, "ambient-music-player")
        with open(foreign, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho lo-fi beats\n")
        os.chmod(foreign, 0o755)
        os.symlink(foreign, self.link)
        proc = self._run_hook()
        self._assert_silent_ok(proc)
        self.assertTrue(os.path.islink(self.link))
        self.assertEqual(os.readlink(self.link), foreign)

    def test_foreign_symlink_named_ambient_is_never_repointed(self):
        # The exact H2 gap the gap found was: a DIFFERENT tool literally
        # named `ambient` (target BASENAME is `ambient`, but its path has no
        # ambient-code component). The old basename-only guard would clobber it.
        foreign_dir = os.path.join(self.home, "othertool", "bin")
        os.makedirs(foreign_dir)
        foreign = os.path.join(foreign_dir, "ambient")
        with open(foreign, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho a totally different ambient\n")
        os.chmod(foreign, 0o755)
        os.symlink(foreign, self.link)
        proc = self._run_hook()
        self._assert_silent_ok(proc)
        self.assertTrue(os.path.islink(self.link))
        self.assertEqual(os.readlink(self.link), foreign)  # untouched

    def test_dangling_foreign_symlink_named_ambient_is_never_relinked(self):
        # A DANGLING symlink to a foreign `ambient` (no ambient-code component)
        # must also be left alone — readlink still exposes the stored target.
        foreign = os.path.join(self.home, "gone-tool", "bin", "ambient")
        os.symlink(foreign, self.link)
        self.assertFalse(os.path.exists(self.link))  # dangling
        proc = self._run_hook()
        self._assert_silent_ok(proc)
        self.assertEqual(os.readlink(self.link), foreign)  # untouched

    def test_nothing_present_creates_nothing(self):
        self.assertFalse(os.path.lexists(self.link))
        proc = self._run_hook()
        self._assert_silent_ok(proc)
        self.assertFalse(os.path.lexists(self.link))


if __name__ == "__main__":
    unittest.main()
