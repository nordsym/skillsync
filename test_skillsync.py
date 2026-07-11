import unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()