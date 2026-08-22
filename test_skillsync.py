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
        self.assertEqual(skillsync.__version__, "0.5.0")

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

if __name__ == "__main__":
    unittest.main()
