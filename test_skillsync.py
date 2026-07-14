import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import skillsync


class UpstreamProposalTests(unittest.TestCase):
    def test_normalization_removes_wrappers(self):
        source = "---\nweight: 50\n---\n\n# Skill\n\nBody.\n\n---\nUp: [[Skills]]\n\n#Moon\n"
        runtime = "<!-- synced-from: abc1234 -->\n# Skill\n\nBody.\n"
        self.assertEqual(skillsync.normalized_skill_body(source), skillsync.normalized_skill_body(runtime))

    def test_no_change(self):
        self.assertEqual(skillsync.classify_upstream_proposal("same\n", "same\n"), "NO_CHANGE")

    def test_unbased_difference_is_candidate(self):
        self.assertEqual(skillsync.classify_upstream_proposal("source\n", "runtime\n"), "CORE_CANDIDATE")

    def test_runtime_change_is_candidate(self):
        self.assertEqual(skillsync.classify_upstream_proposal("base\n", "runtime\n", "base\n"), "CORE_CANDIDATE")

    def test_source_only_change_is_runtime_only(self):
        self.assertEqual(skillsync.classify_upstream_proposal("source\n", "base\n", "base\n"), "RUNTIME_ONLY")

    def test_two_sided_change_is_conflict(self):
        self.assertEqual(skillsync.classify_upstream_proposal("source\n", "runtime\n", "base\n"), "CONFLICT")

    def test_rejects_skill_path_traversal(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source").mkdir()
            config = {"source_dir": str(root / "source"), "targets": {"test": str(root / "target")}}
            with patch.object(skillsync, "load_config", return_value=config):
                with self.assertRaises(SystemExit):
                    skillsync.cmd_propose_upstream(SimpleNamespace(skill="../escape", target="test", output=None))

    def test_refuses_report_inside_source_tree(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            runtime = root / "target" / "demo"
            source.mkdir()
            runtime.mkdir(parents=True)
            (source / "demo.md").write_text("# Demo\n\nSource.\n")
            (runtime / "SKILL.md").write_text("# Demo\n\nRuntime.\n")
            config = {"source_dir": str(source), "targets": {"test": str(root / "target")}}
            args = SimpleNamespace(skill="demo", target="test", output=str(source / "report.diff"))
            with patch.object(skillsync, "load_config", return_value=config):
                with self.assertRaises(SystemExit):
                    skillsync.cmd_propose_upstream(args)


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


class PublicCliContractTests(unittest.TestCase):
    def test_version_matches_release_line(self):
        self.assertEqual(skillsync.__version__, "0.3.1")

    def test_readme_commands_exist_in_cli_help(self):
        readme = Path(__file__).with_name("README.md").read_text()
        documented = set()
        for match in __import__("re").finditer(r"(?m)^\s*(?:\./|python3?\s+)?skillsync\.py\s+([a-z][a-z-]+)", readme):
            documented.add(match.group(1))
        result = subprocess.run(
            [__import__("sys").executable, str(Path(__file__).with_name("skillsync.py")), "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        for command in documented:
            self.assertIn(command, result.stdout)


class WebhookCredentialTests(unittest.TestCase):
    def test_resolves_keychain_secret_only_in_memory(self):
        config = {
            "webhook_url": "https://example.test/bot{secret}/send",
            "webhook_keychain": {"service": "alerts", "account": "operator"},
        }
        completed = subprocess.CompletedProcess([], 0, stdout="123:abc_DEF\n", stderr="")
        with patch.object(skillsync.subprocess, "run", return_value=completed) as run:
            url = skillsync.resolve_webhook_url(config)
        self.assertEqual(url, "https://example.test/bot123:abc_DEF/send")
        self.assertEqual(
            run.call_args.args[0],
            ["security", "find-generic-password", "-s", "alerts", "-a", "operator", "-w"],
        )

    def test_missing_keychain_secret_fails_closed(self):
        config = {
            "webhook_url": "https://example.test/bot{secret}/send",
            "webhook_keychain": {"service": "alerts", "account": "operator"},
        }
        completed = subprocess.CompletedProcess([], 44, stdout="", stderr="not found")
        with patch.object(skillsync.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                skillsync.resolve_webhook_url(config)

    def test_plain_webhook_url_remains_supported(self):
        self.assertEqual(
            skillsync.resolve_webhook_url({"webhook_url": "https://example.test/hook"}),
            "https://example.test/hook",
        )

if __name__ == "__main__":
    unittest.main()
