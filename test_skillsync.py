import contextlib
import io
import json
import os
import re
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
        args = type("Args", (), {"all": False, "skill": "demo", "reviewed": reviewed, "create_missing": False})()
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

    def test_exact_port_starts_with_loader_frontmatter(self):
        self.run_sync()
        port = self.target / "demo" / "SKILL.md"
        text = port.read_text()
        fields, has_frontmatter = skillsync.parse_frontmatter(text)
        self.assertTrue(has_frontmatter)
        self.assertEqual(fields["name"], "demo")
        self.assertTrue(fields["description"])
        self.assertIn("<!-- synced-from:", text)
        self.assertEqual(skillsync.normalized_skill_body(text), "# Demo\n\nVersion one.\n")

    def test_configured_codex_adapter_is_minimal_and_source_derived(self):
        (self.root / "skillsync.json").write_text(json.dumps({
            "source_dir": str(self.source),
            "targets": {"codex": str(self.target)},
            "target_adapters": {"codex": {"openai_yaml": True}},
        }))
        self.run_sync()
        adapter = self.target / "demo" / "agents" / "openai.yaml"
        text = adapter.read_text()
        self.assertIn('display_name: "Demo"', text)
        self.assertIn('default_prompt: "Use $demo to apply this NordSym Core skill."', text)
        self.assertNotIn("dependencies:", text)
        self.assertNotIn("policy:", text)

    def test_missing_port_needs_explicit_creation_flag(self):
        (self.target / "demo" / "SKILL.md").unlink()
        args = type("Args", (), {"all": False, "skill": "demo", "reviewed": True, "create_missing": True})()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(io.StringIO()):
                skillsync.cmd_sync_exact(args)
        finally:
            os.chdir(previous)
        created = self.target / "demo" / "SKILL.md"
        self.assertTrue(created.exists())
        self.assertEqual(skillsync.parse_frontmatter(created.read_text())[0]["name"], "demo")

    def test_managed_root_prevents_local_name_collision_from_being_overwritten(self):
        managed = self.target / "nordsym"
        local = self.target / "outbound" / "demo" / "SKILL.md"
        local.parent.mkdir(parents=True)
        local.write_text("# Local learning\n")
        (self.target / "demo" / "SKILL.md").unlink()
        (self.root / "skillsync.json").write_text(json.dumps({
            "source_dir": str(self.source),
            "targets": {"test": str(self.target)},
            "managed_roots": {"test": str(managed)},
        }))
        args = type("Args", (), {"all": False, "skill": "demo", "reviewed": True, "create_missing": True})()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(io.StringIO()):
                skillsync.cmd_sync_exact(args)
        finally:
            os.chdir(previous)
        self.assertEqual(local.read_text(), "# Local learning\n")
        self.assertTrue((managed / "demo" / "SKILL.md").exists())

    def test_create_missing_requires_reviewed_flag(self):
        (self.target / "demo" / "SKILL.md").unlink()
        args = type("Args", (), {"all": False, "skill": "demo", "reviewed": False, "create_missing": True})()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with self.assertRaisesRegex(SystemExit, "requires --reviewed"):
                skillsync.cmd_sync_exact(args)
        finally:
            os.chdir(previous)

    def test_prepare_discovery_preserves_local_body_without_claiming_parity(self):
        port = self.target / "demo" / "SKILL.md"
        port.write_text("# Local Demo\n\n## Purpose\n\nLocal learning.\n")
        args = SimpleNamespace(skill="demo", target="test", reviewed=True)
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(io.StringIO()):
                skillsync.cmd_prepare_discovery(args)
        finally:
            os.chdir(previous)
        text = port.read_text()
        fields, has_frontmatter = skillsync.parse_frontmatter(text)
        self.assertTrue(has_frontmatter)
        self.assertEqual(fields["name"], "demo")
        self.assertNotIn("synced-from", text)
        self.assertEqual(skillsync.normalized_skill_body(text), "# Local Demo\n\n## Purpose\n\nLocal learning.\n")

    def test_prepare_discovery_requires_review_and_refuses_existing_frontmatter(self):
        port = self.target / "demo" / "SKILL.md"
        version = skillsync.source_version(self.source, self.skill)
        port.write_text(skillsync.render_core_port("demo", self.skill.read_text(), version))
        args = SimpleNamespace(skill="demo", target="test", reviewed=False)
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with self.assertRaisesRegex(SystemExit, "requires --reviewed"):
                skillsync.cmd_prepare_discovery(args)
            args.reviewed = True
            with self.assertRaisesRegex(SystemExit, "already has frontmatter"):
                skillsync.cmd_prepare_discovery(args)
        finally:
            os.chdir(previous)

    def test_compact_descriptions_changes_only_frontmatter_metadata(self):
        port = self.target / "demo" / "SKILL.md"
        original_body = "# Demo\n\nInstruction body stays exactly here.\n"
        port.write_text(
            "---\nname: demo\ndescription: " + json.dumps("One very long discovery description " * 12) + "\n---\n" + original_body
        )
        args = SimpleNamespace(target="test", max_chars=80, reviewed=False, include_unmanaged=True)
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with self.assertRaisesRegex(SystemExit, "requires --reviewed"):
                skillsync.cmd_compact_descriptions(args)
            args.reviewed = True
            with contextlib.redirect_stdout(io.StringIO()):
                skillsync.cmd_compact_descriptions(args)
        finally:
            os.chdir(previous)
        fields, has_frontmatter = skillsync.parse_frontmatter(port.read_text())
        self.assertTrue(has_frontmatter)
        self.assertLessEqual(len(fields["description"]), 80)
        self.assertTrue(port.read_text().endswith(original_body))

    def test_compact_descriptions_preserves_quoted_json_scalars(self):
        original_body = "# Demo\n\nBody.\n"
        source_description = 'Unicode å and a "quoted" capability that must remain valid metadata.'
        rendered = skillsync.render_compacted_discovery_port(
            "demo",
            "---\nname: demo\ndescription: " + json.dumps(source_description, ensure_ascii=False) + "\n---\n" + original_body,
            160,
        )
        fields, has_frontmatter = skillsync.parse_frontmatter(rendered)
        self.assertTrue(has_frontmatter)
        self.assertEqual(json.loads(re.search(r"(?m)^description:\s*(.*)$", rendered).group(1)), source_description)
        self.assertTrue(rendered.endswith(original_body))

    def test_check_detects_current_stamp_with_semantic_divergence(self):
        port = self.target / "demo" / "SKILL.md"
        port.write_text(port.read_text() + "Runtime-only instruction.\n")
        args = SimpleNamespace(skill=None, fail_on_drift=True, webhook=False)
        previous = Path.cwd()
        output = io.StringIO()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
                skillsync.cmd_check(args)
        finally:
            os.chdir(previous)
        self.assertIn("DIVERGED test:demo", output.getvalue())
        self.assertIn("DIVERGED: 1", output.getvalue())


class PromotionAndSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()
        (self.source / "demo.md").write_text("# Demo\n\n## Purpose\n\nPortable capability.\n")
        (self.target / "local-demo").mkdir()
        (self.target / "local-demo" / "SKILL.md").write_text("---\nname: local-demo\ndescription: local\n---\n# Local\n")
        (self.root / "skillsync.json").write_text(json.dumps({
            "source_dir": str(self.source),
            "targets": {"hermes": str(self.target)},
            "managed_roots": {"hermes": str(self.target / "nordsym")},
        }))
        self.old_config = skillsync.CONFIG_FILE
        skillsync.CONFIG_FILE = "skillsync.json"

    def tearDown(self):
        skillsync.CONFIG_FILE = self.old_config
        self.tmp.cleanup()

    def test_candidate_packet_is_review_only_and_risk_flagged(self):
        candidate = self.target / "local-demo" / "SKILL.md"
        candidate.write_text(candidate.read_text() + "Use Keychain token only after review.\n")
        args = SimpleNamespace(target="hermes", skill="local-demo", output=str(self.root / "packet.json"))
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(io.StringIO()):
                skillsync.cmd_promote_candidate(args)
        finally:
            os.chdir(previous)
        packet = json.loads((self.root / "packet.json").read_text())
        self.assertEqual(packet["classification"], "REVIEW_REQUIRED")
        self.assertIn("credential-like reference", packet["candidate"]["risk_flags"])
        self.assertFalse(packet["canonical_core"]["exists"])
        self.assertIn("promotion_contract", packet)

    def test_capability_snapshot_does_not_claim_native_discovery(self):
        args = SimpleNamespace(output=str(self.root / "snapshot.json"))
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(io.StringIO()):
                skillsync.cmd_capability_snapshot(args)
        finally:
            os.chdir(previous)
        snapshot = json.loads((self.root / "snapshot.json").read_text())
        self.assertEqual(snapshot["schema"], "skillsync-capability-snapshot/v1")
        self.assertEqual(snapshot["semantics"]["native_discovery"], "not_observed")
        self.assertEqual(snapshot["skills"][0]["ports"][0]["native_discovery"], "not_observed")

    def test_capability_snapshot_requires_body_as_well_as_version_parity(self):
        managed = self.target / "nordsym" / "demo" / "SKILL.md"
        managed.parent.mkdir(parents=True)
        version = skillsync.source_version(self.source, self.source / "demo.md")
        managed.write_text(skillsync.render_core_port("demo", (self.source / "demo.md").read_text(), version) + "Local addendum.\n")
        args = SimpleNamespace(output=str(self.root / "snapshot.json"))
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(io.StringIO()):
                skillsync.cmd_capability_snapshot(args)
        finally:
            os.chdir(previous)
        port = json.loads((self.root / "snapshot.json").read_text())["skills"][0]["ports"][0]
        self.assertTrue(port["version_parity"])
        self.assertFalse(port["body_parity"])
        self.assertFalse(port["parity"])


class PublicCliContractTests(unittest.TestCase):
    def test_version_matches_release_line(self):
        self.assertEqual(skillsync.__version__, "0.8.0")

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


class CatalogAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.catalog = self.root / "catalog"
        self.catalog.mkdir()
        self.old_config = skillsync.CONFIG_FILE
        skillsync.CONFIG_FILE = "skillsync.json"

    def tearDown(self):
        skillsync.CONFIG_FILE = self.old_config
        self.tmp.cleanup()

    def write_skill(self, path, name, description):
        skill = self.catalog / path / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(
            f"---\nname: {name}\ndescription: {json.dumps(description)}\n---\n# {name}\n"
        )

    def config(self, **profile):
        config = {
            "source_dir": str(self.catalog),
            "targets": {},
            "catalog_profiles": {"general": {"roots": [str(self.catalog)], **profile}},
        }
        (self.root / "skillsync.json").write_text(json.dumps(config))
        return config

    def test_catalog_audit_clips_descriptions_for_budget_estimate(self):
        self.write_skill("alpha", "alpha", "a" * 200)
        profile = self.config(max_description_chars=40, max_estimated_tokens=1000)
        report = skillsync.catalog_audit(profile, "general")
        self.assertEqual(report["metrics"]["entries"], 1)
        self.assertEqual(report["metrics"]["description_chars"], 200)
        self.assertEqual(report["metrics"]["rendered_description_chars"], 40)
        self.assertFalse(report["pass"])
        self.assertIn("overlong_descriptions", report["violations"]["codes"])

    def test_catalog_audit_can_exclude_nested_skill_trees_when_the_loader_does(self):
        self.write_skill("alpha", "alpha", "kept")
        self.write_skill("package/.claude/skills/embedded", "embedded", "ignored")
        self.write_skill("package/node_modules/vendor", "vendor", "ignored")
        profile = self.config(exclude_parts=[".claude", "node_modules"])
        report = skillsync.catalog_audit(profile, "general")
        self.assertEqual([entry["name"] for entry in report["skills"]], ["alpha"])
        self.assertEqual(report["metrics"]["ignored"], 2)

    def test_catalog_audit_flags_duplicate_names_and_budget_overflow(self):
        self.write_skill("one/duplicate", "duplicate", "one")
        self.write_skill("two/duplicate", "duplicate", "two")
        profile = self.config(max_entries=1, max_estimated_tokens=1, fail_on_duplicates=True)
        report = skillsync.catalog_audit(profile, "general")
        self.assertFalse(report["pass"])
        self.assertEqual(report["violations"]["duplicate_names"], ["duplicate"])
        self.assertTrue(report["violations"]["max_entries"])
        self.assertTrue(report["violations"]["max_estimated_tokens"])

    def test_catalog_audit_counts_visible_skill_names_in_budget(self):
        long_name = "x" * 40
        self.write_skill(long_name, long_name, "")
        profile = self.config(max_description_chars=160, entry_overhead_tokens=0, budget_tokens=10)
        report = skillsync.catalog_audit(profile, "general")
        self.assertEqual(report["entries"][0]["rendered_entry_chars"], 42)
        self.assertTrue(report["summary"]["over_budget"])

    def test_catalog_audit_command_fails_only_when_requested(self):
        self.write_skill("one", "one", "one")
        self.write_skill("two", "two", "two")
        config = self.config(max_entries=1)
        args = SimpleNamespace(profile="general", json=False, fail_on_budget=False)
        with patch.object(skillsync, "load_config", return_value=config), contextlib.redirect_stdout(io.StringIO()):
            skillsync.cmd_catalog_audit(args)
        args.fail_on_budget = True
        with patch.object(skillsync, "load_config", return_value=config), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            skillsync.cmd_catalog_audit(args)

    def test_catalog_audit_resolves_only_enabled_codex_plugin_skill_roots(self):
        codex_home = self.root / "codex"
        cache = codex_home / "plugins" / "cache"
        config_path = codex_home / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            '[plugins."alpha@provider"]\nenabled = true\n\n'
            '[plugins."remote@provider"]\nenabled = true\n\n'
            '[plugins."beta@provider"]\nenabled = false\n\n'
            '[unrelated]\nenabled = true\n'
        )
        enabled = cache / "provider" / "alpha" / "1.0.0" / "skills" / "alpha" / "SKILL.md"
        remote = cache / "provider-remote" / "remote" / "1.0.0" / "skills" / "remote" / "SKILL.md"
        disabled = cache / "provider" / "beta" / "1.0.0" / "skills" / "beta" / "SKILL.md"
        enabled.parent.mkdir(parents=True)
        remote.parent.mkdir(parents=True)
        disabled.parent.mkdir(parents=True)
        enabled.write_text("---\nname: alpha\ndescription: enabled\n---\n# Alpha\n")
        remote.write_text("---\nname: remote\ndescription: remote\n---\n# Remote\n")
        disabled.write_text("---\nname: beta\ndescription: disabled\n---\n# Beta\n")
        profile = self.config(roots=[{"codex_config": str(config_path)}])
        report = skillsync.catalog_audit(profile, "general")
        self.assertEqual([entry["name"] for entry in report["entries"]], ["alpha", "remote"])
        self.assertEqual(report["roots"][0]["root"], "plugin:alpha@provider")

    def test_catalog_audit_follows_a_symlinked_codex_config_to_its_live_cache(self):
        real_home = self.root / "real-codex"
        real_config = real_home / "config.toml"
        real_config.parent.mkdir(parents=True)
        real_config.write_text('[plugins."alpha@provider"]\nenabled = true\n')
        lexical_config = self.root / "buzz" / "config.toml"
        lexical_config.parent.mkdir(parents=True)
        lexical_config.symlink_to(real_config)
        real_skill = real_home / "plugins" / "cache" / "provider" / "alpha" / "1" / "skills" / "real" / "SKILL.md"
        stale_skill = lexical_config.parent / "plugins" / "cache" / "provider" / "alpha" / "1" / "skills" / "stale" / "SKILL.md"
        real_skill.parent.mkdir(parents=True)
        stale_skill.parent.mkdir(parents=True)
        real_skill.write_text("---\nname: real\ndescription: live\n---\n# Real\n")
        stale_skill.write_text("---\nname: stale\ndescription: stale\n---\n# Stale\n")
        profile = self.config(roots=[{"codex_config": str(lexical_config)}])
        report = skillsync.catalog_audit(profile, "general")
        self.assertEqual([entry["name"] for entry in report["entries"]], ["real"])

    def test_catalog_search_ranks_name_then_description_and_catalog_read_is_exact(self):
        self.write_skill("web", "web-deploy", "Deploy a website safely.")
        self.write_skill("notes", "meeting-notes", "Turn a meeting into an action plan.")
        profile = self.config()
        found = skillsync.catalog_search(profile, "general", "deploy website", 5)
        self.assertEqual(found[0]["name"], "web-deploy")
        content = skillsync.catalog_read(profile, "general", "web-deploy")
        self.assertIn("Deploy a website safely.", content["content"])

    def test_catalog_search_collapses_mirrored_names_and_keeps_alternative_paths(self):
        codex = self.root / "codex"
        agents = self.root / "agents"
        for root, text in ((codex, "codex copy"), (agents, "agents copy")):
            skill = root / "shared" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                f"---\nname: shared-skill\ndescription: {json.dumps(text)}\n---\n# Shared\n"
            )
        profile = self.config(roots=[
            {"name": "agents", "path": str(agents)},
            {"name": "codex", "path": str(codex)},
        ])
        results = skillsync.catalog_search(profile, "general", "shared", 5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["root"], "codex")
        self.assertEqual(results[0]["alternatives"], [str((agents / "shared" / "SKILL.md").resolve())])

    def test_catalog_read_refuses_ambiguous_name_without_an_exact_path(self):
        self.write_skill("one/duplicate", "duplicate", "first")
        self.write_skill("two/duplicate", "duplicate", "second")
        profile = self.config()
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            skillsync.catalog_read(profile, "general", "duplicate")

    def test_catalog_read_enforces_a_bounded_instruction_payload(self):
        self.write_skill("large", "large", "large skill")
        skill = self.catalog / "large" / "SKILL.md"
        skill.write_text(skill.read_text() + "x" * 300)
        profile = self.config()
        with self.assertRaisesRegex(ValueError, "read limit"):
            skillsync.catalog_read(profile, "general", "large", max_chars=100)
        with self.assertRaisesRegex(ValueError, "hard 16000-character"):
            skillsync.catalog_read(profile, "general", "large", max_chars=16001)

    def test_install_catalog_router_writes_one_small_model_visible_skill(self):
        router_root = self.root / "router-target"
        config = self.config()
        config["catalog_router_targets"] = {"desktop": str(router_root)}
        (self.root / "skillsync.json").write_text(json.dumps(config))
        args = SimpleNamespace(target="desktop", profile="general", reviewed=True, force=False)
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(io.StringIO()):
                skillsync.cmd_install_catalog_router(args)
        finally:
            os.chdir(previous)
        router = router_root / "skillsync-catalog-router" / "SKILL.md"
        self.assertTrue(router.exists())
        self.assertIn("catalog-search general", router.read_text())
        self.assertIn("catalog-read general", router.read_text())

    def test_catalog_library_can_search_all_cached_plugins_without_enabling_them(self):
        cache = self.root / "plugin-cache"
        skill = cache / "provider" / "disabled-plugin" / "1" / "skills" / "specialist" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: specialist\ndescription: Handle specialised work.\n---\n# Specialist\n")
        profile = self.config(roots=[{"plugin_cache": str(cache)}])
        results = skillsync.catalog_search(profile, "general", "specialised", 5)
        self.assertEqual(results[0]["name"], "specialist")


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


class VersionMatchTests(unittest.TestCase):
    def test_git_abbreviations_of_same_commit_match(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "skillsync test"], cwd=root, check=True)
            (root / "skill.md").write_text("one\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "one"], cwd=root, check=True)
            first = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            self.assertTrue(skillsync.versions_match(root, first[:7], first[:8]))

            (root / "skill.md").write_text("two\n")
            subprocess.run(["git", "commit", "-qam", "two"], cwd=root, check=True)
            second = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            self.assertFalse(skillsync.versions_match(root, first[:7], second[:8]))

class BundleSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git(self.repo, 'init', '-q')
        self.git(self.repo, 'config', 'user.name', 'Test')
        self.git(self.repo, 'config', 'user.email', 'test@example.test')
        self.source = self.repo / 'skills'
        self.source.mkdir()
        self.target = self.root / 'workflows'
        self.target.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, root, *args):
        return subprocess.check_output(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test', *args], cwd=root, text=True).strip()

    def skill(self, root, name='demo', content=None):
        path = root / name / 'SKILL.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content or b'---\r\nname: demo\r\ndescription: test\r\nlicense: MIT\r\ncompatibility: grok\r\nsource: upstream\r\nmetadata:\r\n  nested: [one, two]\r\nunknown: |\r\n  preserve me\r\n---\r\n# Exact body\r\n')
        return path

    def sync(self, **kwargs):
        return skillsync.sync_bundles(self.source, self.target, 'grokbot', **kwargs)

    def test_frontmatter_and_binary_helper_bytes_and_modes_preserved_both_directions(self):
        original = self.skill(self.target)
        helper = original.parent / 'tools' / 'run.sh'
        helper.parent.mkdir()
        helper.write_bytes(b'#!/bin/sh\n\x00\xff\n')
        helper.chmod(0o755)
        (original.parent / 'LICENSE').write_bytes(b'MIT\r\n')
        report = self.sync()
        self.assertEqual(report['summary']['added'], 1)
        self.assertEqual((self.source / 'demo/SKILL.md').read_bytes(), original.read_bytes())
        self.assertEqual((self.source / 'demo/tools/run.sh').read_bytes(), helper.read_bytes())
        self.assertTrue((self.source / 'demo/tools/run.sh').stat().st_mode & 0o111)
        changed = original.read_bytes() + b'new repo edit\n'
        (self.source / 'demo/SKILL.md').write_bytes(changed)
        self.assertEqual(self.sync()['summary']['changed'], 1)
        self.assertEqual(original.read_bytes(), changed)
        self.assertEqual(self.sync()['summary']['skipped'], 1)

    def test_game_builder_verbatim_six_file_roundtrip(self):
        import shutil
        fixture = Path(__file__).parent / 'tests/fixtures/game-builder'
        shutil.copytree(fixture, self.target / 'game-builder')
        self.sync()
        second_box = self.root / 'new-box'
        skillsync.sync_bundles(self.source, second_box, 'grokbot')
        self.assertEqual(len(list(fixture.iterdir())), 6)
        for item in fixture.iterdir():
            self.assertEqual((second_box / 'game-builder' / item.name).read_bytes(), item.read_bytes())
            self.assertEqual((self.source / 'game-builder' / item.name).read_bytes(), item.read_bytes())

    def test_three_way_repo_change_exports_box_change_imports(self):
        box = self.skill(self.target)
        self.sync()
        box.write_bytes(box.read_bytes() + b'box edit')
        self.assertEqual(self.sync()['entries'][0]['direction'], 'import')
        repo = self.source / 'demo/SKILL.md'
        repo.write_bytes(repo.read_bytes() + b'repo edit')
        self.assertEqual(self.sync()['entries'][0]['direction'], 'export')
        self.assertEqual(box.read_bytes(), repo.read_bytes())

    def test_both_helper_edits_conflict_and_future_mtime_does_not_choose_a_winner(self):
        self.skill(self.target)
        helper = self.target / 'demo/helper'
        helper.write_bytes(b'base')
        self.sync()
        helper.write_bytes(b'box')
        (self.source / 'demo/helper').write_bytes(b'repo')
        os.utime(self.source / 'demo/helper', (4000000000, 4000000000))
        self.assertEqual(self.sync()['summary']['conflict'], 1)
        self.assertEqual(helper.read_bytes(), b'box')
        self.assertEqual((self.source / 'demo/helper').read_bytes(), b'repo')

    def test_initial_divergence_and_missing_previously_synced_copy_conflict(self):
        self.skill(self.target, content=b'box')
        self.skill(self.source, content=b'repo')
        self.assertEqual(self.sync()['summary']['conflict'], 1)
        (self.source / 'demo/SKILL.md').write_bytes(b'box')
        self.sync()
        import shutil
        shutil.rmtree(self.source / 'demo')
        self.assertEqual(self.sync()['summary']['conflict'], 1)
        self.assertFalse((self.source / 'demo').exists())

    def test_dry_run_does_not_write_baseline_or_skills(self):
        self.skill(self.target)
        state = skillsync.bundle_state_path(self.source, self.target, 'grokbot')
        self.assertEqual(self.sync(dry_run=True)['summary']['added'], 1)
        self.assertFalse(state.exists())
        self.assertFalse((self.source / 'demo').exists())

    def test_import_export_direction_skip_opposite_edits(self):
        self.skill(self.target)
        self.assertEqual(self.sync(direction='export')['summary']['skipped'], 1)
        self.assertFalse((self.source / 'demo').exists())
        self.sync(direction='import')
        (self.source / 'demo/SKILL.md').write_bytes(b'repo change')
        self.assertEqual(self.sync(direction='import')['summary']['skipped'], 1)

    def test_readonly_cursor_plugins_and_symlinks_are_never_copied(self):
        self.skill(self.target, 'good')
        external = self.root / '.cursor/plugins'
        self.skill(external, 'vendor')
        (self.target / 'linked').symlink_to(external / 'vendor', target_is_directory=True)
        internal = self.skill(self.target, 'internal-link')
        (internal.parent / 'helper').symlink_to(external / 'vendor/SKILL.md')
        readonly = self.skill(self.target, 'readonly')
        readonly.parent.chmod(0o555)
        try:
            report = self.sync()
            self.assertEqual(report['summary']['added'], 1)
            self.assertEqual(report['summary']['skipped'], 3)
            self.assertFalse((self.source / 'vendor').exists())
            self.assertFalse((self.source / 'linked').exists())
            self.assertFalse((self.source / 'internal-link').exists())
            self.assertFalse((self.source / 'readonly').exists())
            with self.assertRaisesRegex(ValueError, 'read-only'):
                skillsync.sync_bundles(self.source, external, 'grokbot')
        finally:
            readonly.parent.chmod(0o755)

    def test_readonly_skill_file_is_skipped_even_with_a_writable_folder(self):
        skill = self.skill(self.target)
        skill.chmod(0o444)
        try:
            report = self.sync()
            self.assertEqual(report['summary']['skipped'], 1)
            self.assertFalse((self.source / 'demo').exists())
        finally:
            skill.chmod(0o644)

    def test_copy_rechecks_destination_before_replacement(self):
        origin = self.skill(self.source)
        dest = self.skill(self.target)
        expected_origin = skillsync.bundle_snapshot(origin.parent)
        expected_dest = skillsync.bundle_snapshot(dest.parent)
        dest.write_bytes(b'concurrent box edit')
        with self.assertRaisesRegex(ValueError, 'changed during sync'):
            skillsync.replace_bundle(origin.parent, dest.parent, expected_origin, expected_dest)
        self.assertEqual(dest.read_bytes(), b'concurrent box edit')

    def test_unsafe_counterpart_cannot_be_overwritten(self):
        external = self.root / 'external'
        self.skill(external)
        self.skill(self.source)
        (self.target / 'demo').symlink_to(external / 'demo', target_is_directory=True)
        self.assertEqual(self.sync()['summary']['conflict'], 1)
        self.assertTrue((self.target / 'demo').is_symlink())

    def test_overlap_symlink_root_and_malformed_state_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'overlap'):
            skillsync.sync_bundles(self.source, self.repo, 'grokbot')
        alias = self.root / 'alias'
        alias.symlink_to(self.target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            skillsync.sync_bundles(self.source, alias, 'grokbot')
        state = skillsync.bundle_state_path(self.source, self.target, 'grokbot')
        state.parent.mkdir(parents=True)
        state.write_text('{"demo": "not a hash"}')
        with self.assertRaisesRegex(ValueError, 'baseline'):
            self.sync()

    def test_legacy_flat_source_collision_is_not_rewritten(self):
        self.skill(self.target)
        flat = self.source / 'demo.md'
        flat.write_bytes(b'# Legacy vault Core')
        self.assertEqual(self.sync()['summary']['conflict'], 1)
        self.assertEqual(flat.read_bytes(), b'# Legacy vault Core')

    def test_legacy_rendering_commands_skip_or_refuse_grokbot(self):
        port = self.skill(self.target)
        original = port.read_bytes()
        (self.source / 'demo.md').write_text('# Demo\n')
        config = {'source_dir': str(self.source), 'targets': {'grokbot': str(self.target)}}
        with patch.object(skillsync, 'load_config', return_value=config), contextlib.redirect_stdout(io.StringIO()):
            skillsync.cmd_stamp(SimpleNamespace(skill=None, all=True))
            skillsync.cmd_sync_exact(SimpleNamespace(skill=None, all=True, reviewed=True, create_missing=True))
            for fn in (skillsync.cmd_prepare_discovery, skillsync.cmd_compact_descriptions, skillsync.cmd_scaffold):
                with self.assertRaisesRegex(SystemExit, 'lossless'):
                    fn(SimpleNamespace(target='grokbot', skill='demo', reviewed=True, force=True))
        self.assertEqual(port.read_bytes(), original)

    def test_concurrent_conflict_prevents_git_publication(self):
        config = {'source_dir': str(self.source), 'targets': {'grokbot': str(self.target)}}
        entries = [{'skill': 'demo', 'status': 'added', 'direction': 'import'}]
        preview = {'entries': entries, 'summary': {'conflict': 0}, 'dry_run': True}
        conflict = {'entries': entries + [{'skill': 'other', 'status': 'conflict'}],
                    'summary': {'conflict': 1}, 'dry_run': False}
        args = SimpleNamespace(target='grokbot', target_dir=None, direction='both',
                               dry_run=False, git=True, json=True)
        with patch.object(skillsync, 'load_config', return_value=config), \
                patch.object(skillsync, 'sync_git', side_effect=['', '0', '']) as git, \
                patch.object(skillsync, 'sync_bundles', side_effect=[preview, conflict]), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            skillsync.cmd_sync(args)
        self.assertEqual([call.args[1] for call in git.call_args_list],
                         ['status', 'rev-list', 'pull'])

    def test_git_transport_two_clones_conflicts_and_scoped_commit(self):
        remote = self.root / 'remote.git'
        remote.mkdir()
        self.git(remote, 'init', '--bare', '-q')
        self.skill(self.source)
        config = self.repo / 'skillsync.json'
        config.write_text(json.dumps({'source_dir': './skills', 'targets': {'grokbot': str(self.target)}}))
        self.git(self.repo, 'add', '.')
        self.git(self.repo, 'commit', '-qm', 'seed')
        self.git(self.repo, 'remote', 'add', 'origin', str(remote))
        self.git(self.repo, 'push', '-qu', 'origin', 'HEAD')
        clone = self.root / 'second'
        self.git(self.root, 'clone', '-q', str(remote), str(clone))
        args = SimpleNamespace(target='grokbot', target_dir=None, direction='both', dry_run=False, git=True, json=True)
        with patch.object(skillsync, 'CONFIG_FILE', str(config)), contextlib.redirect_stdout(io.StringIO()):
            skillsync.cmd_sync(args)
        box = self.target / 'demo/SKILL.md'
        box.write_bytes(box.read_bytes() + b'box edit\n')
        (clone / 'skills/demo/SKILL.md').write_bytes(b'remote edit\n')
        self.git(clone, 'commit', '-qam', 'remote change')
        self.git(clone, 'push', '-q')
        with patch.object(skillsync, 'CONFIG_FILE', str(config)), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            skillsync.cmd_sync(args)
        self.assertTrue(box.read_bytes().endswith(b'box edit\n'))
        self.assertEqual((self.source / 'demo/SKILL.md').read_bytes(), b'remote edit\n')
        self.assertEqual(self.git(self.repo, 'status', '--porcelain'), '')
        # Resolve explicitly by making the copies equal, then a box-only edit is imported and pushed.
        box.write_bytes(b'remote edit\n')
        with patch.object(skillsync, 'CONFIG_FILE', str(config)), contextlib.redirect_stdout(io.StringIO()):
            skillsync.cmd_sync(args)
            box.write_bytes(b'new box edit\n')
            skillsync.cmd_sync(args)
        self.assertEqual(self.git(self.repo, 'status', '--porcelain'), '')
        self.assertEqual(self.git(self.repo, 'rev-list', '--count', '@{upstream}..HEAD'), '0')
        self.git(clone, 'pull', '--ff-only', '-q')
        self.assertEqual((clone / 'skills/demo/SKILL.md').read_bytes(), b'new box edit\n')
        # Dry-run does not even fetch/pull a remote update.
        before = self.git(self.repo, 'rev-parse', 'HEAD')
        (clone / 'skills/demo/SKILL.md').write_bytes(b'next remote\n')
        self.git(clone, 'commit', '-qam', 'next')
        self.git(clone, 'push', '-q')
        args.dry_run = True
        with patch.object(skillsync, 'CONFIG_FILE', str(config)), contextlib.redirect_stdout(io.StringIO()):
            skillsync.cmd_sync(args)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), before)
        self.assertEqual(box.read_bytes(), b'new box edit\n')
        args.dry_run = False
        with patch.object(skillsync, 'CONFIG_FILE', str(config)), contextlib.redirect_stdout(io.StringIO()):
            skillsync.cmd_sync(args)
        self.assertEqual(box.read_bytes(), b'next remote\n')
        (self.repo / 'unrelated').write_bytes(b'preserve')
        with patch.object(skillsync, 'CONFIG_FILE', str(config)), self.assertRaisesRegex(SystemExit, 'clean checkout'):
            skillsync.cmd_sync(args)


if __name__ == "__main__":
    unittest.main()
