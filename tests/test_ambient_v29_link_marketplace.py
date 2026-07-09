"""Hermetic tests for `_link_is_ours` under a MARKETPLACE install layout.

A Claude Code marketplace directory is named after the MARKETPLACE, not the
plugin: `marketplace.json` with `"name": "ambient"` installs the plugin to
`.../plugins/marketplaces/ambient/`, whose path has NO `/ambient-code/`
component. The original guard only looked for `/ambient-code/`, so ambient
mistook its OWN launcher for a foreign tool — `ambient link` refused to
re-link and `ambient link --remove` refused to clean up, both with
"another tool" / "not an ambient-code launcher". The dev install
(`~/.claude/skills/ambient-code/`) masked it. No network, no writes outside
tempdirs.
"""
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "ambient")


def load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_p29", BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_p29", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = load_module()


class LinkIsOursMarketplaceTest(unittest.TestCase):
    """`_link_is_ours` must recognize our launcher across install layouts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.bindir = os.path.join(self.tmp, "localbin")
        os.makedirs(self.bindir)
        self.dest = os.path.join(self.bindir, "ambient")
        self._orig_this_script = amb._this_script

    def tearDown(self):
        amb._this_script = self._orig_this_script
        self._tmp.cleanup()

    def _install(self, root_rel):
        """Create <tmp>/<root_rel>/bin/ambient and point _this_script at it."""
        root = os.path.join(self.tmp, *root_rel.split("/"))
        binp = os.path.join(root, "bin")
        os.makedirs(binp, exist_ok=True)
        script = os.path.join(binp, "ambient")
        with open(script, "w") as fh:
            fh.write("#!/usr/bin/env python3\n")
        amb._this_script = lambda: os.path.realpath(script)
        return script

    def test_marketplace_layout_is_ours(self):
        """REGRESSION: marketplace dir named after the MARKETPLACE ('ambient'),
        so the target has no `/ambient-code/` — must still be recognized."""
        script = self._install("plugins/marketplaces/ambient")
        os.symlink(script, self.dest)
        self.assertNotIn("/ambient-code/", script)
        self.assertTrue(amb._link_is_ours(self.dest))

    @unittest.skipIf(
        os.name == "nt",
        "launcher self-heal via symlink is POSIX-only; Windows uses the .cmd "
        "shim (covered by _shim_is_ours). os.path.realpath cannot resolve a "
        "dangling symlink's 8.3 short path on Windows.")
    def test_marketplace_dangling_link_is_ours(self):
        """Post-GC: the target no longer exists, but it is still our path."""
        script = self._install("plugins/marketplaces/ambient")
        os.symlink(script, self.dest)
        os.remove(script)
        self.assertFalse(os.path.exists(script))
        self.assertTrue(amb._link_is_ours(self.dest))

    def test_dev_skills_dir_layout_still_ours(self):
        """The legacy `/ambient-code/` signal must keep working."""
        script = self._install("skills/ambient-code")
        os.symlink(script, self.dest)
        self.assertIn("/ambient-code/", script.replace(os.sep, "/"))
        self.assertTrue(amb._link_is_ours(self.dest))

    def test_versioned_cache_layout_still_ours(self):
        script = self._install("plugins/cache/ambient/ambient-code/1.0.0")
        os.symlink(script, self.dest)
        self.assertTrue(amb._link_is_ours(self.dest))

    def test_foreign_launcher_is_never_ours(self):
        """A different tool merely named `ambient` must never be clobbered."""
        self._install("plugins/marketplaces/ambient")
        foreign = os.path.join(self.tmp, "usr", "bin", "ambient")
        os.makedirs(os.path.dirname(foreign))
        with open(foreign, "w") as fh:
            fh.write("#!/bin/sh\necho not ours\n")
        os.symlink(foreign, self.dest)
        self.assertFalse(amb._link_is_ours(self.dest))

    def test_foreign_dangling_launcher_is_never_ours(self):
        self._install("plugins/marketplaces/ambient")
        os.symlink(os.path.join(self.tmp, "gone", "elsewhere", "ambient"), self.dest)
        self.assertFalse(amb._link_is_ours(self.dest))

    def test_regular_file_is_not_a_link(self):
        self._install("plugins/marketplaces/ambient")
        with open(self.dest, "w") as fh:
            fh.write("not a symlink\n")
        self.assertFalse(amb._link_is_ours(self.dest))

    def test_missing_dest_is_not_ours(self):
        self._install("plugins/marketplaces/ambient")
        self.assertFalse(amb._link_is_ours(self.dest))


if __name__ == "__main__":
    unittest.main()
