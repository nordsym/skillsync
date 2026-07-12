import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import skillsync


class SyncExactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "skillsync test"], cwd=self.root, check=True)
        self.skill = self.source / "demo.md"
        self.skill.write_text("# Demo\n\nVersion one.\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.root, check=True)
        version = skillsync.source_version(self.source, self.skill)
        port = self.target / "demo" / "SKILL.md"
        port.parent.mkdir()
        port.write_text(skillsync.stamp_content(self.skill.read_text(), version))
        (self.root / "skillsync.json").write_text(json.dumps({
            "source_dir": str(self.source),
            "targets": {"test": str(self.target)},
        }))
        self.old_cwd = Path.cwd()
        self.old_config = skillsync.CONFIG_FILE
        skillsync.CONFIG_FILE = "skillsync.json"

    def tearDown(self):
        skillsync.CONFIG_FILE = self.old_config
        self.tmp.cleanup()

    def commit_source_change(self):
        self.skill.write_text("# Demo\n\nVersion two.\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "source update"], cwd=self.root, check=True)

    def run_sync(self, reviewed=False):
        args = type("Args", (), {"all": False, "skill": "demo", "reviewed": reviewed})()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(io.StringIO()):
                skillsync.cmd_sync_exact(args)
        finally:
            os.chdir(previous)

    def test_syncs_port_that_matches_stamped_base(self):
        self.commit_source_change()
        self.run_sync()
        port = self.target / "demo" / "SKILL.md"
        self.assertEqual(skillsync.normalized_skill_body(port.read_text()), "# Demo\n\nVersion two.\n")

    def test_refuses_unreviewed_runtime_divergence(self):
        self.commit_source_change()
        port = self.target / "demo" / "SKILL.md"
        port.write_text(port.read_text() + "Runtime learning.\n")
        with self.assertRaises(SystemExit):
            self.run_sync()
        self.assertIn("Runtime learning", port.read_text())

    def test_reviewed_override_replaces_divergence(self):
        self.commit_source_change()
        port = self.target / "demo" / "SKILL.md"
        port.write_text(port.read_text() + "Runtime learning.\n")
        self.run_sync(reviewed=True)
        self.assertNotIn("Runtime learning", port.read_text())
        self.assertEqual(skillsync.normalized_skill_body(port.read_text()), "# Demo\n\nVersion two.\n")


if __name__ == "__main__":
    unittest.main()
