#!/usr/bin/env python3
"""
skillsync -- keep AI agent "skill" files in sync across multiple harnesses.

The problem: Claude Code, Codex, Grok, and other agent runtimes each expect
skill/instruction files in their own folder with their own conventions. If
you maintain the same skill for more than one harness, you end up with N
copies that silently drift out of sync -- nobody notices until an agent runs
on stale instructions.

skillsync does not translate skill *prose* between formats (that's a
rewriting task, an LLM or a human does it better than a script ever will).
What it does mechanically:

  1. Track one canonical source directory for your skill files.
  2. Stamp each ported copy with a marker recording which version of the
     source it reflects (a git commit SHA if the source is a git repo,
     otherwise a content hash -- works either way).
  3. Compare stamps and normalized bodies against the source's *current*
     version and report ports that are missing, stale, or semantically
     diverged. This is a real-content comparison,
     not a file-timestamp comparison -- moving, cloning, or checking out the
     source repo can never produce a false positive.
  4. Optionally fire a webhook when real drift is found, and optionally
     install a git post-commit hook so drift is caught the moment the
     source changes, not on the next scheduled check.
  5. Audit the actual model-visible skill catalog against an explicit context
     budget before a runtime silently drops skills.
  6. Render exact Core ports with minimal portable `name` and `description`
     frontmatter required by native skill loaders. The skill prose stays
     canonical and is never generated or rewritten.
  7. Learn each target's frontmatter *shape* (not its prose) from the
     skills already there, and scaffold a draft in that shape for a new
     port, pre-filled with the target's fixed fields and the source's raw
     content for a human/agent to actually adapt. Never auto-stamped, a
     scaffold is a starting point, not a finished port.

Zero dependencies beyond the Python 3.9+ standard library.

Usage:
  skillsync.py init                                  # write skillsync.json in the current dir
  skillsync.py stamp [<skill>] [--all]                # mark port(s) as synced to the current source version
  skillsync.py sync-exact [<skill>] [--all] [--reviewed] [--create-missing]
                                                        # propagate canonical body with divergence guard
  skillsync.py check [<skill>] [--fail-on-drift] [--webhook]
  skillsync.py registry [--output <path>]             # emit a generated inventory of all target skills
  skillsync.py install-hook                           # add a post-commit hook to the source repo (git sources only)
  skillsync.py learn-format [<target>] [--all]        # infer a target's frontmatter shape from its existing skills
  skillsync.py scaffold <skill> <target> [--force]    # draft a new port in the learned shape, needs manual review
  skillsync.py propose-upstream <skill> --target <target> [--output <path>]
                                                        # read-only runtime-to-source proposal
  skillsync.py promote-candidate <target> <skill> [--output <path>]
                                                        # read-only local-skill promotion packet
  skillsync.py capability-snapshot [--output <path>]   # generated port-parity snapshot
  skillsync.py catalog-audit <profile> [--strict]      # model-visible catalog budget check
  skillsync.py catalog-search <profile> <query>        # find one skill outside startup context
  skillsync.py catalog-read <profile> <skill>          # load one exact catalog skill on demand
  skillsync.py install-catalog-router <target> --profile <profile> --reviewed
  skillsync.py compact-descriptions <target> --reviewed # compact discovery metadata only
  skillsync.py prepare-discovery <skill> --target <target> --reviewed
                                                        # add loader metadata without claiming Core parity
"""
import argparse
import difflib
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

__version__ = "0.8.0"
CONFIG_FILE = "skillsync.json"
MARKER_RE = re.compile(r"<!-- synced-from: [0-9a-f]+ -->\n?")
SKILL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
DEFAULT_DISCOVERY_DESCRIPTION_MAX_CHARS = 160
DEFAULT_CATALOG_ENTRY_OVERHEAD_TOKENS = 8
DEFAULT_CATALOG_READ_MAX_CHARS = 16000
PROMOTION_RISK_PATTERNS = {
    "credential-like reference": re.compile(r"(?i)\b(api[_-]?key|secret|token|password|private[_-]?key|keychain)\b"),
    "client-bound reference": re.compile(r"(?i)(^|[ /])0?2\s*-\s*clients?([ /]|$)|\bclient[_ -]?(data|id|secret|token)\b"),
    "external-action instruction": re.compile(r"(?i)\b(send|publish|deploy|invite|payment|outreach)\b"),
}
VENDOR_MARKERS = (
    "anthropics/skills",
    "trail of bits",
    "trailofbits",
    "openai-curated",
    "openai bundled",
    "source: anthropic",
    "source: openai",
    "source: trail",
)


def strip_vault_wrappers(text: str) -> str:
    """Remove Obsidian/vault-only wrappers from a runtime port.

    Runtime skill ports should carry the skill body, not vault governance
    metadata. Keep content from the first H1 onward and drop the trailing
    Obsidian navigation footer.
    """
    text = MARKER_RE.sub("", text)
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + len("\n---\n") :]

    h1 = re.search(r"(?m)^#\s+", text)
    if h1:
        text = text[h1.start() :]

    lines = text.rstrip().splitlines()
    while lines and (not lines[-1].strip() or re.match(r"^#[A-Za-z0-9_-]+$", lines[-1].strip())):
        lines.pop()
    if lines and lines[-1].startswith("Up: "):
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == "---":
        lines.pop()
    return "\n".join(lines).rstrip() + "\n"


def load_config():
    path = Path.cwd() / CONFIG_FILE
    if not path.exists():
        sys.exit(
            f"No {CONFIG_FILE} found in {Path.cwd()}. Run 'skillsync.py init' first."
        )
    return json.loads(path.read_text())


def write_config(config):
    (Path.cwd() / CONFIG_FILE).write_text(json.dumps(config, indent=2) + "\n")


def git_root(path: Path):
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None
    return Path(out.stdout.strip())


def source_version(source_dir: Path, skill_file: Path) -> str:
    """A short, stable identifier for the current version of a skill file.
    Uses the last git commit touching the file if source_dir is a git repo
    (so it survives renames within the same content), otherwise a content
    hash so the tool still works on a plain, non-git folder of skills.
    """
    root = git_root(source_dir)
    if root:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h", "--", str(skill_file)],
            cwd=root,
            capture_output=True,
            text=True,
        )
        sha = out.stdout.strip()
        if sha:
            return sha
    return hashlib.sha256(skill_file.read_bytes()).hexdigest()[:8]


def versions_match(source_dir: Path, stamped: str, current: str) -> bool:
    """Compare Git abbreviations by commit identity, not abbreviation length."""
    if stamped == current:
        return True
    if not re.fullmatch(r"[0-9a-f]{7,40}", stamped or "") or not re.fullmatch(r"[0-9a-f]{7,40}", current or ""):
        return False
    root = git_root(source_dir)
    if not root:
        return False
    out = subprocess.run(
        ["git", "rev-parse", f"{stamped}^{{commit}}", f"{current}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    resolved = [line.strip() for line in out.stdout.splitlines() if line.strip()]
    return out.returncode == 0 and len(resolved) == 2 and resolved[0] == resolved[1]


def find_skills(source_dir: Path):
    return sorted(p for p in source_dir.glob("*.md") if p.is_file())


def managed_target_dir(config: dict, target_name: str) -> str:
    """Return the explicit governed-Core landing zone for one runtime.

    A runtime can keep locally evolved skills beside Core only when Core has
    its own managed root. That prevents a name collision from being mistaken
    for a managed port and overwritten by a synchronization operation.
    """
    return config.get("managed_roots", {}).get(target_name, config["targets"][target_name])


def target_file(target_dir: str, skill_name: str) -> Path:
    """Find <skill_name>/SKILL.md under target_dir, at any depth.

    Not every harness uses a flat <target_dir>/<skill_name>/SKILL.md layout.
    OpenClaw does. Hermes does not -- it nests skills under a category
    (<target_dir>/<category>/<skill_name>/SKILL.md), and the category isn't
    knowable from the skill name alone. A shallow glob handles both without
    per-harness configuration: search recursively for a directory named
    exactly <skill_name> containing a SKILL.md, wherever it sits.

    Returns the flat <target_dir>/<skill_name>/SKILL.md path if nothing is
    found (the natural "this doesn't exist yet" default for stamp/check to
    report MISSING against).
    """
    if not SKILL_NAME_RE.fullmatch(skill_name):
        raise ValueError("Skill name must be a simple filename stem")
    base = Path(target_dir).expanduser()
    if not base.exists():
        return base / skill_name / "SKILL.md"
    matches = list(base.glob(f"**/{skill_name}/SKILL.md"))
    if matches:
        def priority(path: Path):
            rel = path.relative_to(base)
            parts = rel.parts
            if len(parts) == 3 and parts[0] == "nordsym":
                return (0, str(rel))
            if len(parts) == 2:
                return (1, str(rel))
            return (2, str(rel))

        return sorted(matches, key=priority)[0]
    return base / skill_name / "SKILL.md"


def stamp_content(text: str, version: str) -> str:
    marker = f"<!-- synced-from: {version} -->\n"
    return marker + strip_vault_wrappers(text)


def safe_description(text: str, fallback_name: str, max_chars=DEFAULT_DISCOVERY_DESCRIPTION_MAX_CHARS) -> str:
    """Extract one bounded parser-safe description from canonical prose."""
    _title, description = parse_source_skill(text)
    description = " ".join(description.split()).replace("\x00", "")
    return (description or f"NordSym Core skill: {fallback_name}.")[:max_chars]


def render_core_port(skill_name: str, source_text: str, version: str) -> str:
    """Render a portable exact Core port without inventing any prose.

    Native loaders expect YAML at byte zero. Keep the source-version marker
    immediately after the YAML document so loaders and skillsync both retain
    their native parsing behavior. JSON strings are valid YAML scalars and
    prevent source prose from injecting additional frontmatter fields.
    """
    if not SKILL_NAME_RE.fullmatch(skill_name):
        raise ValueError("Skill name must be a simple filename stem")
    header = "\n".join([
        "---",
        f"name: {skill_name}",
        f"description: {json.dumps(safe_description(source_text, skill_name), ensure_ascii=False)}",
        "---",
        "",
    ])
    return header + stamp_content(source_text, version)


def render_discovery_port(skill_name: str, runtime_text: str) -> str:
    """Add only standard loader metadata to an unformatted local adaptation.

    This deliberately omits a `synced-from` marker. The port becomes natively
    discoverable, but skillsync continues to report it as unreviewed until the
    semantic local change is promoted, reconciled, or retired.
    """
    if not SKILL_NAME_RE.fullmatch(skill_name):
        raise ValueError("Skill name must be a simple filename stem")
    fields, has_frontmatter = parse_frontmatter(runtime_text)
    if has_frontmatter:
        raise ValueError("Port already has frontmatter; refuse to replace target-local metadata")
    header = "\n".join([
        "---",
        f"name: {skill_name}",
        f"description: {json.dumps(safe_description(runtime_text, skill_name), ensure_ascii=False)}",
        "---",
        "",
    ])
    return header + runtime_text.lstrip("\n")


def compact_discovery_description(description: str, max_chars: int) -> str:
    """Shorten discovery metadata at a word boundary, never skill prose."""
    normalized = " ".join(description.split()).replace("\x00", "")
    if len(normalized) <= max_chars:
        return normalized
    clipped = normalized[: max_chars + 1].rsplit(" ", 1)[0].rstrip(" ,;:.-")
    return clipped or normalized[:max_chars].rstrip()


def decode_discovery_description_scalar(raw_value: str) -> str:
    """Decode only a lossless JSON-style description scalar.

    Most native Codex descriptions are JSON strings, which are valid YAML.
    Do not guess at broader YAML scalar syntax before a mutating operation.
    """
    value = raw_value.strip()
    if not value:
        return ""
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"description is not a valid JSON string: {exc.msg}")
        if not isinstance(decoded, str):
            raise ValueError("description JSON value must be a string")
        return decoded
    if value.startswith("'") or value.startswith("|") or value.startswith(">"):
        raise ValueError("description uses unsupported YAML scalar syntax")
    return value


def render_compacted_discovery_port(skill_name: str, runtime_text: str, max_chars: int) -> str:
    """Replace only a simple YAML ``description`` scalar in a native port."""
    fields, has_frontmatter = parse_frontmatter(runtime_text)
    if not has_frontmatter:
        raise ValueError("Skill has no simple frontmatter")
    end = runtime_text.find("\n---\n", 4)
    header = runtime_text[4:end]
    body = runtime_text[end:]
    match = re.search(r"(?m)^description:\s*(.*)$", header)
    if match:
        description = decode_discovery_description_scalar(match.group(1))
    else:
        description = safe_description(runtime_text, skill_name, max_chars)
    compacted = compact_discovery_description(description, max_chars)
    rendered_line = f"description: {json.dumps(compacted, ensure_ascii=False)}"
    if match:
        header = re.sub(r"(?m)^description:\s*.*$", rendered_line, header)
    else:
        lines = header.splitlines()
        insert_at = next((i + 1 for i, line in enumerate(lines) if line.startswith("name:")), len(lines))
        lines.insert(insert_at, rendered_line)
        header = "\n".join(lines)
    return "---\n" + header + body


def render_openai_yaml(skill_name: str, source_text: str) -> str:
    """Render the minimal Codex UI adapter with no dependencies or grants."""
    title, _description = parse_source_skill(source_text)
    title = re.sub(r"\s*\(Core\)\s*$", "", title).strip() or skill_name
    short = safe_description(source_text, skill_name)[:64].rstrip(" ,;:.-")
    return "\n".join([
        "interface:",
        f"  display_name: {json.dumps(title, ensure_ascii=False)}",
        f"  short_description: {json.dumps(short, ensure_ascii=False)}",
        f"  default_prompt: {json.dumps(f'Use ${skill_name} to apply this NordSym Core skill.', ensure_ascii=False)}",
        "",
    ])


def write_target_adapters(config: dict, target_name: str, skill_name: str, source_text: str, dest: Path):
    """Write only explicitly configured target UI metadata."""
    adapters = config.get("target_adapters", {}).get(target_name, {})
    if adapters.get("openai_yaml"):
        adapter = dest.parent / "agents" / "openai.yaml"
        adapter.parent.mkdir(parents=True, exist_ok=True)
        adapter.write_text(render_openai_yaml(skill_name, source_text))


def read_stamp(text: str):
    m = re.search(r"synced-from: ([0-9a-f]+)", text)
    return m.group(1) if m else None


def normalized_skill_body(text: str) -> str:
    """Return the comparable body shared by source and runtime ports."""
    return strip_vault_wrappers(text).replace("\r\n", "\n")


def source_body_at_version(source_dir: Path, skill_file: Path, version: str):
    """Read a source skill at a stamped git version, or return None."""
    root = git_root(source_dir)
    if not root or not re.fullmatch(r"[0-9a-f]{7,40}", version or ""):
        return None
    try:
        rel = skill_file.relative_to(root)
    except ValueError:
        return None
    out = subprocess.run(
        ["git", "show", f"{version}:{rel.as_posix()}"], cwd=root,
        capture_output=True, text=True,
    )
    return normalized_skill_body(out.stdout) if out.returncode == 0 else None


def classify_upstream_proposal(source_body: str, runtime_body: str, base_body=None):
    """Classify a read-only runtime-to-source proposal."""
    if source_body == runtime_body:
        return "NO_CHANGE"
    if base_body is None:
        return "CORE_CANDIDATE"
    source_changed = source_body != base_body
    runtime_changed = runtime_body != base_body
    if source_changed and runtime_changed:
        return "CONFLICT"
    if runtime_changed:
        return "CORE_CANDIDATE"
    return "RUNTIME_ONLY"


def cmd_propose_upstream(args):
    config = load_config()
    source_dir = Path(config["source_dir"]).expanduser().resolve()
    if not SKILL_NAME_RE.fullmatch(args.skill):
        sys.exit("Skill name must be a simple filename stem with no path separators")
    source_file = source_dir / f"{args.skill}.md"
    if not source_file.exists():
        sys.exit(f"No canonical Core skill named '{args.skill}' in {source_dir}")
    if args.target not in config["targets"]:
        sys.exit(f"Unknown target '{args.target}'. Known: {', '.join(config['targets'])}")
    runtime_file = target_file(managed_target_dir(config, args.target), args.skill)
    if not runtime_file.exists():
        sys.exit(f"No runtime port for '{args.skill}' in target '{args.target}'")

    runtime_raw = runtime_file.read_text()
    source_body = normalized_skill_body(source_file.read_text())
    runtime_body = normalized_skill_body(runtime_raw)
    stamp = read_stamp(runtime_raw)
    base_body = source_body_at_version(source_dir, source_file, stamp) if stamp else None
    classification = classify_upstream_proposal(source_body, runtime_body, base_body)
    diff = "".join(difflib.unified_diff(
        source_body.splitlines(keepends=True), runtime_body.splitlines(keepends=True),
        fromfile=f"canonical/{args.skill}.md",
        tofile=f"{args.target}/{args.skill}/SKILL.md",
    ))
    report = "\n".join([
        "skillsync upstream proposal",
        f"Classification: {classification}",
        f"Skill: {args.skill}",
        f"Target: {args.target}",
        f"Canonical: {source_file}",
        f"Runtime: {runtime_file}",
        f"Runtime stamp: {stamp or 'UNSTAMPED'}",
        f"Historical base: {'available' if base_body is not None else 'unavailable'}",
        "", "This is read-only. Review the diff and patch canonical Core deliberately.",
        "", diff or "(no semantic body diff)\n",
    ])
    if args.output:
        output = Path(args.output).expanduser()
        if output.is_symlink():
            sys.exit("Refusing to write an upstream report through a symlink")
        resolved_output = output.resolve()
        protected_roots = [source_dir] + [Path(p).expanduser().resolve() for p in config["targets"].values()]
        protected_roots += [Path(p).expanduser().resolve() for p in config.get("managed_roots", {}).values()]
        if resolved_output == source_file.resolve() or resolved_output == runtime_file.resolve():
            sys.exit("Refusing to overwrite canonical source or runtime port with a report")
        if any(root == resolved_output or root in resolved_output.parents for root in protected_roots):
            sys.exit("Refusing to write an upstream report inside a source or target skill tree")
        if any(part == ".git" for part in resolved_output.parts):
            sys.exit("Refusing to write an upstream report inside .git")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report)
        print(f"Wrote {output}")
        print(f"Classification: {classification}")
    else:
        print(report, end="" if report.endswith("\n") else "\n")


def runtime_candidate_files(config: dict, target_name: str, skill_name: str):
    """Find non-managed runtime-local candidates with an exact skill stem."""
    if target_name not in config["targets"]:
        sys.exit(f"Unknown target '{target_name}'. Known: {', '.join(config['targets'])}")
    if not SKILL_NAME_RE.fullmatch(skill_name):
        sys.exit("Skill name must be a simple filename stem with no path separators")
    runtime_root = Path(config["targets"][target_name]).expanduser()
    managed_file = target_file(managed_target_dir(config, target_name), skill_name)
    candidates = []
    if runtime_root.exists():
        for path in runtime_root.glob(f"**/{skill_name}/SKILL.md"):
            if path.resolve() != managed_file.resolve() and ".archive" not in path.parts and "archive" not in path.parts:
                candidates.append(path)
    return sorted(candidates)


def candidate_risk_flags(text: str) -> list:
    return sorted(name for name, pattern in PROMOTION_RISK_PATTERNS.items() if pattern.search(text))


def safe_report_output(path: Path, config: dict):
    """Reject writes through links or into executable source/skill trees."""
    if path.is_symlink():
        sys.exit("Refusing to write a report through a symlink")
    resolved = path.resolve()
    protected = [Path(config["source_dir"]).expanduser().resolve()]
    protected += [Path(p).expanduser().resolve() for p in config["targets"].values()]
    protected += [Path(p).expanduser().resolve() for p in config.get("managed_roots", {}).values()]
    if any(root == resolved or root in resolved.parents for root in protected):
        sys.exit("Refusing to write a report inside a source or target skill tree")
    if any(part == ".git" for part in resolved.parts):
        sys.exit("Refusing to write a report inside .git")


def cmd_promote_candidate(args):
    """Produce a review-only packet for a runtime-local skill.

    This never copies, enables, or promotes a skill. It gives the Core owner
    enough provenance and risk signal to decide whether a local learning may
    become an agent-agnostic governed capability.
    """
    config = load_config()
    files = runtime_candidate_files(config, args.target, args.skill)
    if not files:
        sys.exit(f"No non-managed runtime-local skill named '{args.skill}' in target '{args.target}'")
    if len(files) != 1:
        listing = ", ".join(str(path) for path in files)
        sys.exit(f"Ambiguous runtime-local skill '{args.skill}' in target '{args.target}': {listing}")
    skill_file = files[0]
    raw = skill_file.read_text(errors="ignore")
    fields, has_frontmatter = parse_frontmatter(raw)
    source_dir = Path(config["source_dir"]).expanduser().resolve()
    source_file = source_dir / f"{args.skill}.md"
    report = {
        "schema": "skillsync-promotion-candidate/v1",
        "classification": "REVIEW_REQUIRED",
        "candidate": {
            "target": args.target,
            "path": str(skill_file),
            "sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "line_count": len(raw.splitlines()),
            "frontmatter": {"present": has_frontmatter, "name": fields.get("name"), "description": fields.get("description")},
            "risk_flags": candidate_risk_flags(raw),
        },
        "canonical_core": {
            "exists": source_file.exists(),
            "path": str(source_file),
        },
        "promotion_contract": [
            "Review source and risk flags without copying secrets, client material, or execution authority.",
            "Decide the smallest governed Core scope and target allowlist explicitly.",
            "Add or revise canonical Core deliberately, then use sync-exact to create managed ports.",
            "Native discovery and runtime authority remain separate acceptance gates.",
        ],
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        output = Path(args.output).expanduser()
        safe_report_output(output, config)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered)
        print(f"Wrote {output}")
    else:
        print(rendered, end="")


def cmd_capability_snapshot(args):
    """Emit machine-readable Core port parity, never a claim of authority."""
    config = load_config()
    source_dir = Path(config["source_dir"]).expanduser().resolve()
    skills = find_skills(source_dir)
    snapshot = {
        "schema": "skillsync-capability-snapshot/v1",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_dir": str(source_dir),
        "semantics": {
            "port_parity": "A current managed Core file exists and matches canonical source version and normalized body.",
            "native_discovery": "not_observed",
            "authority": "A visible skill grants no tools, identity, credential, client, or execution authority.",
        },
        "skills": [],
    }
    for skill_file in skills:
        version = source_version(source_dir, skill_file)
        ports = []
        for target_name in config["targets"]:
            path = target_file(managed_target_dir(config, target_name), skill_file.stem)
            text = path.read_text(errors="ignore") if path.exists() else ""
            stamp = read_stamp(text)
            fields, has_frontmatter = parse_frontmatter(text)
            ports.append({
                "target": target_name,
                "path": str(path),
                "exists": path.exists(),
                "source_version": stamp or None,
                "version_parity": bool(stamp and versions_match(source_dir, stamp, version)),
                "body_parity": bool(path.exists() and normalized_skill_body(text) == normalized_skill_body(skill_file.read_text())),
                "parity": bool(stamp and versions_match(source_dir, stamp, version) and normalized_skill_body(text) == normalized_skill_body(skill_file.read_text())),
                "frontmatter": bool(has_frontmatter and fields.get("name") == skill_file.stem and fields.get("description")),
                "native_discovery": "not_observed",
            })
        snapshot["skills"].append({
            "name": skill_file.stem,
            "source_version": version,
            "source_sha256": hashlib.sha256(skill_file.read_bytes()).hexdigest(),
            "ports": ports,
        })
    rendered = json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        output = Path(args.output).expanduser()
        safe_report_output(output, config)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered)
        print(f"Wrote {output}")
    else:
        print(rendered, end="")


def has_symlink_component(path: Path, stop_at: Path) -> bool:
    """True when path or one of its parents under stop_at is a symlink."""
    try:
        rel = path.relative_to(stop_at)
    except ValueError:
        return path.is_symlink()
    cur = stop_at
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            return True
    return False


def classify_runtime_skill(skill_file: Path, target_dir: Path, core_target, core_names: set) -> tuple:
    """Return (class, note) for one runtime skill file."""
    rel = skill_file.relative_to(target_dir)
    parts = rel.parts
    name = skill_file.parent.name
    text = skill_file.read_text(errors="ignore")
    lower = text[:5000].lower()

    if any(part.startswith(".archive") or part == "archive" or part == ".archive" for part in parts):
        return "archived", "archive path"

    if core_target and skill_file.resolve() == core_target.resolve():
        stamp = read_stamp(text)
        return "core-port", f"stamp {stamp or 'missing'}"

    if name in core_names:
        return "local", "duplicate name of Core skill"

    if re.search(r"(?m)^license:\s*proprietary", lower) or any(marker in lower for marker in VENDOR_MARKERS):
        return "vendor", "vendor marker"

    return "local", "runtime-local"


def parse_frontmatter(text: str):
    """Returns (fields: dict, has_frontmatter: bool). Only handles simple
    `key: value` lines, good enough for shape-learning, not a full YAML
    parser (skillsync stays dependency-free, no pyyaml)."""
    if not text.startswith("---\n"):
        return {}, False
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, False
    fields = {}
    for line in text[4:end].split("\n"):
        if ":" in line and not line.startswith(" ") and not line.startswith("-"):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip('"')
    return fields, True


def catalog_profile(config: dict, profile_name: str) -> dict:
    """Resolve one explicit model-visible catalog profile.

    A catalog profile is deliberately separate from ``targets``. Targets are
    ports Skillsync manages. A profile states which roots one runtime will
    actually expose to a model, along with a conservative rendered-list
    budget. It never enables or disables a host runtime's plugins.
    """
    profiles = config.get("catalog_profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("catalog_profiles must be an object keyed by profile name")
    profile = profiles.get(profile_name)
    if not isinstance(profile, dict):
        known = ", ".join(sorted(profiles)) or "(none configured)"
        raise ValueError(f"Unknown catalog profile '{profile_name}'. Known: {known}")
    if not isinstance(profile.get("roots"), list) or not profile["roots"]:
        raise ValueError(f"Catalog profile '{profile_name}' requires a non-empty roots list")
    return profile


CODEX_PLUGIN_SECTION_RE = re.compile(r'^\[plugins\."([^"\n]+)"\]\s*$')


def enabled_codex_plugin_ids(config_path: Path, include_disabled=False):
    """Read the small TOML subset needed for Codex plugin enablement.

    Skillsync remains Python 3.9 compatible, so it cannot depend on
    ``tomllib``. Plugin sections and their boolean ``enabled`` fields are
    intentionally parsed narrowly rather than claiming general TOML support.
    """
    current = None
    plugins = []
    enabled = []
    for line in config_path.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        match = CODEX_PLUGIN_SECTION_RE.match(stripped)
        if match:
            current = match.group(1)
            plugins.append(current)
            continue
        if stripped.startswith("["):
            current = None
            continue
        if current and re.match(r"^enabled\s*=\s*true\s*(?:#.*)?$", stripped, re.IGNORECASE):
            enabled.append(current)
    return plugins if include_disabled else enabled


def codex_plugin_roots(config_path: Path, cache_dir=None, include_disabled=False):
    """Resolve enabled Codex plugin skill roots from the local cache.

    A plugin may supply tools without a skill folder. That is not an audit
    error. A package with multiple cached versions is surfaced as multiple
    roots because the loader's exact version selection is host-owned and must
    not be guessed by a catalog guard.
    """
    cache = Path(cache_dir).expanduser() if cache_dir else config_path.parent / "plugins" / "cache"
    roots = []
    for plugin_id in enabled_codex_plugin_ids(config_path, include_disabled):
        if "@" not in plugin_id:
            continue
        name, provider = plugin_id.rsplit("@", 1)
        package = cache / provider / name
        # Desktop-hosted catalogs use a `-remote` cache namespace for the
        # same configured provider. Prefer the exact namespace if it exists;
        # only fall back to its remote mirror when the exact package is absent.
        if not package.is_dir():
            package = cache / f"{provider}-remote" / name
        for skill_dir in sorted(package.glob("*/skills")):
            if skill_dir.is_dir():
                roots.append({"root": f"plugin:{plugin_id}", "path": skill_dir.resolve()})
    return roots


def cached_plugin_roots(cache_dir: Path):
    """Return every installed plugin skill root for an on-demand library."""
    return [
        {"root": f"plugin-cache:{skill_dir.relative_to(cache_dir)}", "path": skill_dir.resolve()}
        for skill_dir in sorted(cache_dir.glob("*/*/*/skills"))
        if skill_dir.is_dir()
    ]


def catalog_roots(config: dict, profile_name: str):
    """Return named catalog roots, resolving target names and filesystem paths."""
    profile = catalog_profile(config, profile_name)
    roots = []
    for raw in profile["roots"]:
        if isinstance(raw, str) and raw in config.get("targets", {}):
            label, path = raw, config["targets"][raw]
        elif isinstance(raw, str):
            label, path = raw, raw
        elif isinstance(raw, dict) and isinstance(raw.get("codex_config"), str):
            # Follow the actual configuration file. A desktop host may expose
            # a convenience symlink, but its live plugin registry belongs to
            # the config target unless the profile explicitly sets cache_dir.
            config_path = Path(raw["codex_config"]).expanduser().resolve()
            if not config_path.is_file():
                roots.append({"root": f"codex-config:{config_path}", "path": config_path})
                continue
            roots.extend(codex_plugin_roots(
                config_path, raw.get("cache_dir"), raw.get("include_disabled", False)
            ))
            continue
        elif isinstance(raw, dict) and isinstance(raw.get("plugin_cache"), str):
            cache_dir = Path(raw["plugin_cache"]).expanduser().resolve()
            if not cache_dir.is_dir():
                roots.append({"root": f"plugin-cache:{cache_dir}", "path": cache_dir})
                continue
            roots.extend(cached_plugin_roots(cache_dir))
            continue
        elif isinstance(raw, dict) and isinstance(raw.get("path"), str):
            label = raw.get("name") or raw["path"]
            path = raw["path"]
        else:
            raise ValueError(
                f"Catalog profile '{profile_name}' has an invalid root. "
                "Use a target name, a path, {name, path}, {codex_config}, or {plugin_cache}."
            )
        roots.append({"root": str(label), "path": Path(path).expanduser().resolve()})
    return roots


def description_issue(description: str, has_frontmatter: bool):
    """Return a loader-metadata issue without pretending to parse full YAML."""
    if not has_frontmatter or not description:
        return "missing_description"
    # The dependency-free frontmatter parser only supports single-line scalar
    # values. A dangling quote is a strong signal that a multiline YAML value
    # would otherwise be silently measured as valid metadata.
    if description.startswith('"') and not description.endswith('"'):
        return "malformed_description"
    return None


def catalog_audit(config: dict, profile_name: str) -> dict:
    """Measure a configured model-visible skill catalog without mutating it.

    Catalog token counts are intentionally estimates, not tokenizer claims:
    every visible description costs ``ceil(chars / 4)`` plus a configurable
    per-entry envelope. Duplicates are retained in the count because a native
    loader sees every discovered file before it can decide how to resolve a
    collision.
    """
    profile = catalog_profile(config, profile_name)
    roots = catalog_roots(config, profile_name)
    max_description_chars = profile.get(
        "max_description_chars", DEFAULT_DISCOVERY_DESCRIPTION_MAX_CHARS
    )
    entry_overhead_tokens = profile.get(
        "entry_overhead_tokens", DEFAULT_CATALOG_ENTRY_OVERHEAD_TOKENS
    )
    budget_tokens = profile.get("budget_tokens", profile.get("max_estimated_tokens"))
    max_entries = profile.get("max_entries")
    fail_on_duplicates = profile.get("fail_on_duplicates", True)
    fail_on_metadata = profile.get("fail_on_metadata", True)
    excluded_parts = profile.get("exclude_parts", [])
    if not isinstance(max_description_chars, int) or max_description_chars < 1:
        raise ValueError("max_description_chars must be a positive integer")
    if not isinstance(entry_overhead_tokens, int) or entry_overhead_tokens < 0:
        raise ValueError("entry_overhead_tokens must be a non-negative integer")
    for field, value in (("budget_tokens", budget_tokens), ("max_entries", max_entries)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"{field} must be a non-negative integer")
    if not isinstance(fail_on_duplicates, bool) or not isinstance(fail_on_metadata, bool):
        raise ValueError("fail_on_duplicates and fail_on_metadata must be booleans")
    if not isinstance(excluded_parts, list) or not all(isinstance(part, str) and part for part in excluded_parts):
        raise ValueError("exclude_parts must be a list of non-empty path components")

    entries = []
    root_rows = []
    missing_roots = []
    ignored = 0
    for root in roots:
        path = root["path"]
        exists = path.is_dir()
        root_rows.append({"root": root["root"], "path": str(path), "exists": exists})
        if not exists:
            missing_roots.append(root["root"])
            continue
        for skill_file in sorted(path.rglob("SKILL.md")):
            if not skill_file.is_file():
                continue
            relative_path = skill_file.relative_to(path)
            if any(part in excluded_parts for part in relative_path.parts[:-1]):
                ignored += 1
                continue
            raw = skill_file.read_text(errors="ignore")
            fields, has_frontmatter = parse_frontmatter(raw)
            name = fields.get("name") or skill_file.parent.name
            description = fields.get("description", "")
            issue = description_issue(description, has_frontmatter)
            description_chars = len(description)
            description_tokens = (description_chars + 3) // 4
            render_description_chars = min(description_chars, max_description_chars)
            # Native catalogs at minimum include a skill name and a separator
            # beside discovery metadata. Count that actual visible payload
            # instead of relying on the fixed overhead to hide long names.
            rendered_entry_chars = len(name) + 2 + render_description_chars
            render_tokens = (rendered_entry_chars + 3) // 4 + entry_overhead_tokens
            issues = [issue] if issue else []
            if description_chars > max_description_chars:
                issues.append("description_too_long")
            entries.append({
                "name": name,
                "path": str(skill_file),
                "relative_path": str(relative_path),
                "root": root["root"],
                "description_chars": description_chars,
                "rendered_description_chars": render_description_chars,
                "rendered_entry_chars": rendered_entry_chars,
                "description_tokens_estimate": description_tokens,
                "render_tokens_estimate": render_tokens,
                "issues": issues,
            })

    entries.sort(key=lambda entry: (entry["name"], entry["root"], entry["relative_path"]))
    by_name = {}
    for entry in entries:
        by_name.setdefault(entry["name"], []).append(entry)
    duplicates = [
        {"name": name, "entries": [{"root": entry["root"], "path": entry["path"]} for entry in matches]}
        for name, matches in sorted(by_name.items()) if len(matches) > 1
    ]
    duplicate_names = [item["name"] for item in duplicates]
    missing_description_count = sum("missing_description" in entry["issues"] for entry in entries)
    malformed_description_count = sum("malformed_description" in entry["issues"] for entry in entries)
    overlong_description_count = sum("description_too_long" in entry["issues"] for entry in entries)
    description_chars = sum(entry["description_chars"] for entry in entries)
    rendered_description_chars = sum(entry["rendered_description_chars"] for entry in entries)
    rendered_entry_chars = sum(entry["rendered_entry_chars"] for entry in entries)
    description_tokens_estimate = sum(entry["description_tokens_estimate"] for entry in entries)
    catalog_tokens_estimate = sum(entry["render_tokens_estimate"] for entry in entries)
    over_budget = budget_tokens is not None and catalog_tokens_estimate > budget_tokens
    over_entry_limit = max_entries is not None and len(entries) > max_entries
    codes = []
    if missing_roots:
        codes.append("missing_roots")
    if duplicate_names and fail_on_duplicates:
        codes.append("duplicate_names")
    if fail_on_metadata:
        if missing_description_count:
            codes.append("missing_descriptions")
        if malformed_description_count:
            codes.append("malformed_descriptions")
        if overlong_description_count:
            codes.append("overlong_descriptions")
    if over_entry_limit:
        codes.append("max_entries")
    if over_budget:
        codes.append("budget_tokens")

    summary = {
        "entries": len(entries),
        "unique_names": len(by_name),
        "description_chars": description_chars,
        "rendered_description_chars": rendered_description_chars,
        "rendered_entry_chars": rendered_entry_chars,
        "description_tokens_estimate": description_tokens_estimate,
        "entry_overhead_tokens": entry_overhead_tokens,
        "entry_overhead_tokens_estimate": len(entries) * entry_overhead_tokens,
        "catalog_tokens_estimate": catalog_tokens_estimate,
        "budget_tokens": budget_tokens,
        "budget_ratio": (catalog_tokens_estimate / budget_tokens) if budget_tokens else None,
        "over_budget": over_budget,
        "over_entry_limit": over_entry_limit,
        "missing_description_count": missing_description_count,
        "malformed_description_count": malformed_description_count,
        "overlong_description_count": overlong_description_count,
        "duplicate_name_count": len(duplicate_names),
        "ignored": ignored,
    }
    return {
        "schema": "skillsync-catalog-audit/v1",
        "profile": profile_name,
        "roots": root_rows,
        "policy": {
            "estimator": "ceil((skill_name_chars + separator + capped_description_chars) / 4) + entry_overhead_tokens per visible skill",
            "max_description_chars": max_description_chars,
            "entry_overhead_tokens": entry_overhead_tokens,
            "budget_tokens": budget_tokens,
            "max_entries": max_entries,
            "fail_on_duplicates": fail_on_duplicates,
            "fail_on_metadata": fail_on_metadata,
            "exclude_parts": excluded_parts,
        },
        "summary": summary,
        # ``metrics`` and ``skills`` retain a simple stable shape for scripts
        # that adopted the initial 0.5.0 preview API.
        "metrics": summary,
        "duplicates": duplicates,
        "violations": {
            "codes": codes,
            "missing_roots": missing_roots,
            "duplicate_names": duplicate_names,
            "max_entries": over_entry_limit,
            "max_estimated_tokens": over_budget,
            "budget_tokens": over_budget,
        },
        "pass": not codes,
        "entries": entries,
        "skills": entries,
    }


def render_catalog_audit(report: dict) -> str:
    """Render a concise human report. JSON remains available for CI."""
    summary = report["summary"]
    state = "PASS" if report["pass"] else "FAIL"
    lines = [
        "skillsync catalog audit",
        f"Profile: {report['profile']}",
        f"Result: {state}",
        f"Visible skills: {summary['entries']} ({summary['unique_names']} unique names)",
        f"Estimated catalog tokens: {summary['catalog_tokens_estimate']}"
        + (f" / {summary['budget_tokens']}" if summary["budget_tokens"] is not None else ""),
        f"Description cap: {report['policy']['max_description_chars']} chars",
        f"Duplicate names: {summary['duplicate_name_count']}",
    ]
    if report["violations"]["codes"]:
        lines.append("Violations: " + ", ".join(report["violations"]["codes"]))
    return "\n".join(lines) + "\n"


def cmd_catalog_audit(args):
    config = load_config()
    try:
        report = catalog_audit(config, args.profile)
    except ValueError as exc:
        sys.exit(str(exc))
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_catalog_audit(report), end="")
    if (getattr(args, "strict", False) or getattr(args, "fail_on_budget", False)) and not report["pass"]:
        sys.exit(1)


def catalog_search(config: dict, profile_name: str, query: str, limit: int = 8):
    """Find skills in the full library without injecting that library at startup."""
    terms = [term for term in re.findall(r"[a-z0-9][a-z0-9_-]*", query.lower()) if len(term) > 1]
    if not terms:
        raise ValueError("Search query needs at least one word or identifier")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")
    results = []
    by_name = {}
    root_priority = {"codex": 0, "agents": 1, "claude": 2, "grok": 3, "openclaw": 4, "hermes": 5}
    for entry in catalog_audit(config, profile_name)["entries"]:
        raw = Path(entry["path"]).read_text(errors="ignore")
        fields, _has_frontmatter = parse_frontmatter(raw)
        haystacks = (entry["name"].lower(), fields.get("description", "").lower(), entry["relative_path"].lower())
        score = 0
        for term in terms:
            if term in haystacks[0]:
                score += 12
            if term in haystacks[1]:
                score += 4
            if term in haystacks[2]:
                score += 1
        if score:
            result = {
                "name": entry["name"],
                "path": entry["path"],
                "root": entry["root"],
                "description": compact_discovery_description(fields.get("description", ""), 240),
                "score": score,
                "relative_depth": len(Path(entry["relative_path"]).parts),
            }
            by_name.setdefault(entry["name"], []).append(result)
    for matches in by_name.values():
        matches.sort(key=lambda item: (-item["score"], root_priority.get(item["root"], 99), item["relative_depth"], item["path"]))
        primary = matches[0]
        primary["alternatives"] = [item["path"] for item in matches[1:]]
        primary.pop("relative_depth")
        results.append(primary)
    return sorted(results, key=lambda item: (-item["score"], item["name"], item["path"]))[:limit]


def catalog_read(config: dict, profile_name: str, skill_name: str, path=None, max_chars=DEFAULT_CATALOG_READ_MAX_CHARS):
    """Read one audited library skill. Never accept an arbitrary filesystem path."""
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    if max_chars > DEFAULT_CATALOG_READ_MAX_CHARS:
        raise ValueError(
            f"max_chars cannot exceed the hard {DEFAULT_CATALOG_READ_MAX_CHARS}-character catalog-read ceiling. "
            "Read a narrower source instead of expanding the conversation payload."
        )
    matches = [entry for entry in catalog_audit(config, profile_name)["entries"] if entry["name"] == skill_name]
    if path is not None:
        matches = [entry for entry in matches if entry["path"] == str(Path(path).expanduser().resolve())]
    if not matches:
        raise ValueError(f"No catalog skill named '{skill_name}' in profile '{profile_name}'")
    if len(matches) > 1:
        candidates = ", ".join(entry["path"] for entry in matches)
        raise ValueError(f"Catalog skill '{skill_name}' is ambiguous. Choose --path: {candidates}")
    entry = matches[0]
    content = Path(entry["path"]).read_text(errors="ignore")
    if len(content) > max_chars:
        raise ValueError(
            f"Catalog skill '{skill_name}' is {len(content)} characters, above the {max_chars}-character read limit. "
            "Read a narrower source instead of expanding the conversation payload."
        )
    return {
        "schema": "skillsync-catalog-skill/v1",
        "profile": profile_name,
        "name": entry["name"],
        "path": entry["path"],
        "content": content,
        "content_chars": len(content),
    }


def cmd_catalog_search(args):
    config = load_config()
    try:
        results = catalog_search(config, args.profile, " ".join(args.query), args.limit)
    except ValueError as exc:
        sys.exit(str(exc))
    if args.json:
        print(json.dumps({"schema": "skillsync-catalog-search/v1", "profile": args.profile, "results": results}, indent=2, ensure_ascii=False))
        return
    if not results:
        print("No matching catalog skills.")
        return
    for result in results:
        print(f"{result['name']}\t{result['description']}\t{result['path']}")


def cmd_catalog_read(args):
    config = load_config()
    try:
        result = catalog_read(config, args.profile, args.skill, args.path, args.max_chars)
    except ValueError as exc:
        sys.exit(str(exc))
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result["content"], end="" if result["content"].endswith("\n") else "\n")


def render_catalog_router(profile_name: str, command: str, config_path: Path) -> str:
    """Render a tiny native skill that routes into the library on demand."""
    config_arg = shlex.quote(str(config_path))
    return "\n".join([
        "---",
        "name: skillsync-catalog-router",
        'description: "Find and load one relevant skill from the full catalog without exposing every skill at startup."',
        "---",
        "# SkillSync Catalog Router",
        "",
        "Use this when the task needs a specialised capability that is not already active.",
        "",
        "1. Search the full library:",
        "```bash",
        f"{command} catalog-search {shlex.quote(profile_name)} \"<task terms>\" --json --config {config_arg}",
        "```",
        "2. Pick one result. Read exactly that audited path:",
        "```bash",
        f"{command} catalog-read {shlex.quote(profile_name)} <skill-name> --path \"<result path>\" --config {config_arg}",
        "```",
        "3. A hard 16,000-character ceiling applies to every read. Read a narrower source when a skill exceeds it.",
        "4. Apply the loaded instructions. Do not load unrelated skills pre-emptively.",
        "5. A loaded skill does not enable a disabled plugin or grant its tools. Use an already available CLI or MCP path, or request plugin activation and a fresh session when the skill requires it.",
        "",
        "The full library remains available. Only the selected instruction enters this conversation.",
        "",
    ])


def cmd_install_catalog_router(args):
    if not args.reviewed:
        sys.exit("install-catalog-router requires --reviewed because it creates a model-visible router skill")
    if args.target == "grokbot":
        sys.exit("grokbot requires lossless 'sync --target grokbot'; legacy rendering is disabled")
    config = load_config()
    targets = {**config.get("targets", {}), **config.get("catalog_router_targets", {})}
    if args.target not in targets:
        sys.exit(f"Unknown router target '{args.target}'. Known: {', '.join(targets)}")
    try:
        catalog_profile(config, args.profile)
    except ValueError as exc:
        sys.exit(str(exc))
    target = Path(targets[args.target]).expanduser()
    destination = target / "skillsync-catalog-router" / "SKILL.md"
    if destination.exists() and not args.force:
        sys.exit(f"{destination} already exists. Use --force only after reviewing the current router.")
    config_path = (Path.cwd() / CONFIG_FILE).resolve()
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_catalog_router(args.profile, command, config_path))
    print(f"INSTALLED catalog router at {destination}")


def learn_format(target_dir: str) -> dict:
    """Infer a target's frontmatter shape from the skills already ported
    there: does it use frontmatter at all, which fields recur, and what
    fixed (non name/description) values are constant across samples (e.g.
    every Hermes skill in this vault's nordsym/ category has the same
    author and license). Returns a template, not a full schema, this is
    shape-inference from examples, not spec parsing.
    """
    base = Path(target_dir).expanduser()
    samples = list(base.glob("**/SKILL.md"))[:20]  # cap, shape doesn't need every file
    if not samples:
        return {"has_frontmatter": True, "fixed_fields": {}, "sample_count": 0}

    frontmatter_count = 0
    field_values = {}  # key -> set of distinct values seen
    categories = set()  # the directory directly under target_dir each sample lives in
    for s in samples:
        fields, has_fm = parse_frontmatter(s.read_text(errors="ignore"))
        if has_fm:
            frontmatter_count += 1
        for k, v in fields.items():
            if k in ("name", "description"):
                continue  # always per-skill, never a fixed field
            field_values.setdefault(k, set()).add(v)
        rel_parts = s.relative_to(base).parts  # (<category?>/)<skill-name>/SKILL.md
        if len(rel_parts) == 3:
            categories.add(rel_parts[0])
        # len == 2 means flat (<skill-name>/SKILL.md), no category layer

    has_frontmatter = frontmatter_count >= len(samples) / 2
    # A field is "fixed" if every sample that had it agreed on one value.
    fixed_fields = {k: next(iter(v)) for k, v in field_values.items() if len(v) == 1}
    # Only infer a default category if every sample agrees on exactly one.
    # Mixed or absent categories -> stay flat, the safer default.
    category = next(iter(categories)) if len(categories) == 1 else None

    return {
        "has_frontmatter": has_frontmatter,
        "fixed_fields": fixed_fields,
        "category": category,
        "sample_count": len(samples),
    }


def cmd_learn_format(args):
    config = load_config()
    targets = config["targets"]
    if not args.all:
        if args.target not in targets:
            sys.exit(f"Unknown target '{args.target}'. Known: {', '.join(targets)}")
        targets = {args.target: targets[args.target]}

    formats = config.setdefault("formats", {})
    for name, target_dir in targets.items():
        result = learn_format(target_dir)
        formats[name] = result
        if result["sample_count"] == 0:
            print(f"{name}: no existing skills found, nothing to learn from yet")
            continue
        shape = "frontmatter" if result["has_frontmatter"] else "no frontmatter (plain markdown)"
        fixed = ", ".join(f"{k}={v}" for k, v in result["fixed_fields"].items()) or "(none)"
        cat = result["category"] or "flat, no category folder"
        print(f"{name}: {shape}, from {result['sample_count']} sample(s), fixed fields: {fixed}, layout: {cat}")

    write_config(config)
    print(f"\nSaved to {CONFIG_FILE}. Run 'skillsync.py scaffold <skill> <target>' to draft a port.")


def parse_source_skill(text: str):
    """Best-effort extraction of a title and one-line description from a
    Universal/-style source file. These aren't strictly uniform (bold-line
    'Category:'/'Version:' style vs YAML frontmatter), so this stays
    forgiving rather than requiring one exact format.
    """
    fields, has_fm = parse_frontmatter(text)
    name = fields.get("name") or fields.get("title")
    description = fields.get("description")

    if not name:
        m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        name = m.group(1).strip() if m else "unknown-skill"
    if not description:
        m = re.search(r"^##\s*Purpose\s*\n+(.+?)(?:\n\n|\n#)", text, re.MULTILINE | re.DOTALL)
        if m:
            description = " ".join(m.group(1).split())
        else:
            description = f"See source for details: {name}."
    return name, description


def cmd_scaffold(args):
    if args.target == "grokbot":
        sys.exit("grokbot requires lossless 'sync --target grokbot'; legacy rendering is disabled")
    config = load_config()
    source_dir = Path(config["source_dir"]).expanduser().resolve()
    src = source_dir / f"{args.skill}.md"
    if not src.exists():
        sys.exit(f"No source file for '{args.skill}' in {source_dir}")

    if args.target not in config["targets"]:
        sys.exit(f"Unknown target '{args.target}'. Known: {', '.join(config['targets'])}")
    target_dir = managed_target_dir(config, args.target)

    fmt = config.get("formats", {}).get(args.target)
    if fmt is None:
        print(f"No learned format for '{args.target}' yet, learning now...")
        fmt = learn_format(target_dir)
        config.setdefault("formats", {})[args.target] = fmt
        write_config(config)

    dest = target_file(target_dir, args.skill)
    if not dest.exists() and fmt.get("category"):
        # target_file() only finds *existing* files; for a brand-new skill
        # with no match anywhere yet, place it using the layout learned
        # from this target's other skills instead of defaulting to flat.
        dest = Path(target_dir).expanduser() / fmt["category"] / args.skill / "SKILL.md"
    if dest.exists() and not args.force:
        sys.exit(f"{dest} already exists. Use --force to overwrite the draft (never overwrites a stamped port silently otherwise).")

    source_text = strip_vault_wrappers(src.read_text())
    name, description = parse_source_skill(source_text)

    if fmt["has_frontmatter"]:
        lines = ["---", f"name: {name}", f"description: {description}"]
        for k, v in fmt["fixed_fields"].items():
            lines.append(f"{k}: {v}")
        lines.append("---")
        header = "\n".join(lines) + "\n"
    else:
        header = ""

    body = (
        f"\n<!-- skillsync-draft: needs manual review before stamping -->\n\n"
        f"# {name}\n\n"
        f"Source of truth: `{src}`.\n\n"
        f"<!-- Raw source content below, adapt it to this target's voice and format before treating this as final. -->\n\n"
        f"{source_text}"
    )

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(header + body)
    print(f"Drafted {dest}")
    print("This is a starting point, not a finished port. Review and rewrite before running 'stamp'.")


def cmd_init(args):
    if (Path.cwd() / CONFIG_FILE).exists():
        sys.exit(f"{CONFIG_FILE} already exists here.")
    write_config(
        {
            "source_dir": "./skills",
            "targets": {
                "claude": "~/.claude/skills",
                "codex": "~/.codex/skills",
                "agents": "~/.agents/skills",
                "grokbot": "/home/box/agent-data/workflows",
            },
            "catalog_profiles": {
                "general": {
                    "roots": ["codex"],
                    "max_entries": 48,
                    "max_description_chars": 160,
                    "entry_overhead_tokens": 8,
                    "budget_tokens": 3500,
                    "fail_on_duplicates": True,
                },
                # This is a library profile, not a startup admission profile.
                # Add installed plugin caches explicitly when the host keeps
                # them outside these native roots.
                "full-library": {
                    "roots": ["claude", "codex", "agents"],
                    "max_description_chars": 160,
                    "entry_overhead_tokens": 8,
                    "fail_on_duplicates": False,
                    "fail_on_metadata": False,
                },
            },
            "webhook_url": None,
        }
    )
    print(f"Wrote {CONFIG_FILE}. Edit source_dir and targets, then run 'skillsync.py stamp --all'.")


def cmd_stamp(args):
    config = load_config()
    source_dir = Path(config["source_dir"]).expanduser().resolve()
    skills = find_skills(source_dir)
    if not skills:
        sys.exit(f"No .md files found in {source_dir}")

    if not args.all:
        skills = [s for s in skills if s.stem == args.skill]
        if not skills:
            sys.exit(f"No skill named '{args.skill}' in {source_dir}")

    for skill_file in skills:
        name = skill_file.stem
        version = source_version(source_dir, skill_file)
        for target_name, target_dir in config["targets"].items():
            if target_name == "grokbot":
                print("SKIPPED  grokbot (use lossless sync --target grokbot)")
                continue
            dest = target_file(managed_target_dir(config, target_name), name)
            if not dest.exists():
                print(f"MISSING  {target_name}:{name} (not stamped, port does not exist)")
                continue
            dest.write_text(stamp_content(dest.read_text(), version))
            print(f"STAMPED  {target_name}:{name} -> {version}")


def cmd_sync_exact(args):
    """Propagate the exact canonical body to managed runtime ports.

    A port is safe to update automatically only when its current normalized
    body still matches the source body at its stamp. Diverged or unstamped
    ports require an explicit --reviewed acknowledgement after
    propose-upstream has been inspected.
    """
    config = load_config()
    if getattr(args, "create_missing", False) and not args.reviewed:
        sys.exit("--create-missing requires --reviewed because it installs a new managed Core port")
    source_dir = Path(config["source_dir"]).expanduser().resolve()
    skills = find_skills(source_dir)
    if not args.all:
        skills = [s for s in skills if s.stem == args.skill]
        if not skills:
            sys.exit(f"No skill named '{args.skill}' in {source_dir}")

    refused = 0
    synced = 0
    for skill_file in skills:
        name = skill_file.stem
        version = source_version(source_dir, skill_file)
        canonical = render_core_port(name, skill_file.read_text(), version)
        for target_name, target_dir in config["targets"].items():
            if target_name == "grokbot":
                print("SKIPPED  grokbot (use lossless sync --target grokbot)")
                continue
            dest = target_file(managed_target_dir(config, target_name), name)
            if not dest.exists():
                if not getattr(args, "create_missing", False):
                    print(f"MISSING  {target_name}:{name}")
                    refused += 1
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(canonical)
                write_target_adapters(config, target_name, name, skill_file.read_text(), dest)
                print(f"CREATED  {target_name}:{name} -> {version}")
                synced += 1
                continue

            runtime_text = dest.read_text()
            stamp = read_stamp(runtime_text)
            base_body = source_body_at_version(source_dir, skill_file, stamp) if stamp else None
            unchanged_from_base = (
                base_body is not None
                and normalized_skill_body(runtime_text) == base_body
            )
            if not unchanged_from_base and not args.reviewed:
                print(
                    f"REFUSED  {target_name}:{name} "
                    "(runtime diverged or is unstamped; review with propose-upstream, then rerun with --reviewed)"
                )
                refused += 1
                continue

            dest.write_text(canonical)
            write_target_adapters(config, target_name, name, skill_file.read_text(), dest)
            mode = "reviewed" if not unchanged_from_base else "exact-base"
            print(f"SYNCED   {target_name}:{name} -> {version} ({mode})")
            synced += 1

    print(f"\nSummary: SYNCED: {synced}   REFUSED/MISSING: {refused}")
    if refused:
        sys.exit(1)


def cmd_prepare_discovery(args):
    """Safely make one reviewed local adaptation visible to native loaders."""
    if not args.reviewed:
        sys.exit("prepare-discovery requires --reviewed because it changes a runtime port")
    if args.target == "grokbot":
        sys.exit("grokbot requires lossless 'sync --target grokbot'; legacy rendering is disabled")
    config = load_config()
    if args.target not in config["targets"]:
        sys.exit(f"Unknown target '{args.target}'. Known: {', '.join(config['targets'])}")
    if not SKILL_NAME_RE.fullmatch(args.skill):
        sys.exit("Skill name must be a simple filename stem with no path separators")
    dest = target_file(managed_target_dir(config, args.target), args.skill)
    if not dest.exists():
        sys.exit(f"No managed port for '{args.skill}' in target '{args.target}'")
    try:
        rendered = render_discovery_port(args.skill, dest.read_text())
    except ValueError as exc:
        sys.exit(str(exc))
    dest.write_text(rendered)
    print(f"PREPARED {args.target}:{args.skill} for native discovery (semantic parity remains unreviewed)")


def cmd_compact_descriptions(args):
    """Compact native discovery metadata in one reviewed target, never bodies."""
    if not args.reviewed:
        sys.exit("compact-descriptions requires --reviewed because it changes runtime discovery metadata")
    if args.target == "grokbot":
        sys.exit("grokbot requires lossless 'sync --target grokbot'; legacy rendering is disabled")
    config = load_config()
    if args.target not in config["targets"]:
        sys.exit(f"Unknown target '{args.target}'. Known: {', '.join(config['targets'])}")
    max_chars = args.max_chars
    if max_chars < 1:
        sys.exit("--max-chars must be a positive integer")
    target = Path(config["targets"][args.target]).expanduser()
    managed = config.get("managed_roots", {}).get(args.target)
    if managed:
        scan_root = Path(managed).expanduser()
    elif getattr(args, "include_unmanaged", False):
        scan_root = target
    else:
        sys.exit(
            "compact-descriptions only changes a configured managed_root by default. "
            "Use --include-unmanaged after reviewing a full target tree."
        )
    changed = skipped = 0
    skipped_paths = []
    for skill_file in sorted(scan_root.rglob("SKILL.md")):
        raw = skill_file.read_text(errors="ignore")
        try:
            rendered = render_compacted_discovery_port(skill_file.parent.name, raw, max_chars)
        except ValueError as exc:
            skipped += 1
            skipped_paths.append(f"{skill_file}: {exc}")
            continue
        if rendered != raw:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=skill_file.parent,
                prefix=f".{skill_file.name}.", suffix=".tmp", delete=False,
            ) as replacement:
                replacement.write(rendered)
                replacement_path = Path(replacement.name)
            replacement_path.replace(skill_file)
            changed += 1
    print(
        f"COMPACTED {changed} discovery description(s) in {scan_root}; "
        f"skipped {skipped} skill(s). Instruction bodies were unchanged."
    )
    for skipped_path in skipped_paths:
        print(f"SKIPPED {skipped_path}")


def cmd_check(args):
    config = load_config()
    source_dir = Path(config["source_dir"]).expanduser().resolve()
    skills = find_skills(source_dir)
    if args.skill:
        skills = [s for s in skills if s.stem == args.skill]
        if not skills:
            sys.exit(f"No skill named '{args.skill}' in {source_dir}")

    missing, stale, diverged, ok = 0, 0, 0, 0
    missing_list, stale_list, diverged_list = [], [], []

    print("skillsync check")
    print(f"Source: {source_dir}\n")

    for skill_file in skills:
        name = skill_file.stem
        current = source_version(source_dir, skill_file)
        source_body = normalized_skill_body(skill_file.read_text())
        for target_name, target_dir in config["targets"].items():
            dest = target_file(managed_target_dir(config, target_name), name)
            if not dest.exists():
                print(f"MISSING  {target_name}:{name}")
                missing_list.append(f"{target_name}:{name}")
                missing += 1
                continue
            runtime_text = dest.read_text()
            stamped = read_stamp(runtime_text)
            if stamped is None:
                print(f"UNSTAMPED {target_name}:{name} (never stamped -- run 'skillsync.py stamp')")
                stale_list.append(f"{target_name}:{name} (unstamped)")
                stale += 1
            elif not versions_match(source_dir, stamped, current):
                print(f"STALE    {target_name}:{name} (stamped {stamped}, source now {current})")
                stale_list.append(f"{target_name}:{name} (source moved to {current})")
                stale += 1
            elif normalized_skill_body(runtime_text) != source_body:
                print(f"DIVERGED {target_name}:{name} (stamp is current but normalized body differs)")
                diverged_list.append(f"{target_name}:{name} (stamp {stamped}, body differs)")
                diverged += 1
            else:
                ok += 1

    total = len(skills)
    n_targets = len(config["targets"])
    print(f"\nSummary: {total} skill(s) x {n_targets} target(s) = {total * n_targets} expected ports.")
    print(f"OK: {ok}   MISSING: {missing}   STALE: {stale}   DIVERGED: {diverged}")

    if (missing or stale or diverged) and args.webhook and config.get("webhook_url"):
        send_webhook(config, missing, missing_list, stale, stale_list, diverged, diverged_list)

    if args.fail_on_drift and (missing or stale or diverged):
        sys.exit(1)


def cmd_registry(args):
    config = load_config()
    source_dir = Path(config["source_dir"]).expanduser().resolve()
    core_files = find_skills(source_dir)
    core_names = {p.stem for p in core_files}
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = []
    class_counts = {}
    runtime_counts = {}
    duplicate_rows = []
    symlink_rows = []

    for target_name, target_raw in config["targets"].items():
        target_dir = Path(target_raw).expanduser()
        if not target_dir.exists():
            row = {
                "runtime": target_name,
                "skill": "(target missing)",
                "class": "unknown",
                "path": str(target_dir),
                "stamp": "",
                "symlink": "",
                "note": "target directory missing",
            }
            rows.append(row)
            class_counts["unknown"] = class_counts.get("unknown", 0) + 1
            runtime_counts[target_name] = runtime_counts.get(target_name, 0) + 1
            continue

        core_targets = {name: target_file(managed_target_dir(config, target_name), name) for name in core_names}
        seen_core_names = {}

        for skill_file in sorted(target_dir.glob("**/SKILL.md")):
            name = skill_file.parent.name
            rel = skill_file.relative_to(target_dir)
            core_target = core_targets.get(name)
            cls, note = classify_runtime_skill(skill_file, target_dir, core_target, core_names)
            stamp = read_stamp(skill_file.read_text(errors="ignore")) or ""
            symlink = "yes" if has_symlink_component(skill_file, target_dir) else ""
            row = {
                "runtime": target_name,
                "skill": name,
                "class": cls,
                "path": str(rel),
                "stamp": stamp,
                "symlink": symlink,
                "note": note,
            }
            rows.append(row)
            class_counts[cls] = class_counts.get(cls, 0) + 1
            runtime_counts[target_name] = runtime_counts.get(target_name, 0) + 1

            if symlink:
                symlink_rows.append(row)
            if name in core_names:
                seen_core_names.setdefault(name, []).append((rel, cls))

        for name, matches in seen_core_names.items():
            if len(matches) > 1:
                duplicate_rows.append((target_name, name, matches))

    rows.sort(key=lambda r: (r["runtime"], r["class"], r["skill"], r["path"]))

    lines = [
        "---",
        "weight: 70",
        "group: Moons",
        "tags: [reference, stack, skill, agents]",
        "nord_type: REFERENCE",
        "nord_owner: NordSym",
        "nord_status: LIVE",
        f"updated: {generated_at[:10]}",
        "---",
        "",
        "# SKILL-REGISTRY",
        "",
        "Generated inventory of runtime skill files tracked by `skillsync`.",
        "",
        f"Generated at: `{generated_at}`",
        f"Source directory: `{config['source_dir']}`",
        "",
        "> Generated file. Do not hand-edit rows. Regenerate from the vault root with `python3 /Users/gustavhemmingsson/Projects/skillsync/skillsync.py registry --output '15 - Stack/Skills/SKILL-REGISTRY.md'`.",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|---|---:|",
        f"| Runtime skill files | {len(rows)} |",
        f"| Governed Core source skills | {len(core_names)} |",
    ]
    for cls in sorted(class_counts):
        lines.append(f"| `{cls}` rows | {class_counts[cls]} |")

    lines += [
        "",
        "## Runtime Counts",
        "",
        "| Runtime | Rows |",
        "|---|---:|",
    ]
    for runtime in sorted(runtime_counts):
        lines.append(f"| `{runtime}` | {runtime_counts[runtime]} |")

    lines += [
        "",
        "## Duplicate Core Names",
        "",
    ]
    if duplicate_rows:
        lines += ["| Runtime | Skill | Paths |", "|---|---|---|"]
        for runtime, name, matches in duplicate_rows:
            paths = "<br>".join(f"`{rel}` ({cls})" for rel, cls in matches)
            lines.append(f"| `{runtime}` | `{name}` | {paths} |")
    else:
        lines.append("None.")

    lines += [
        "",
        "## Symlink Rows",
        "",
    ]
    if symlink_rows:
        lines += ["| Runtime | Skill | Path | Class |", "|---|---|---|---|"]
        for row in symlink_rows:
            lines.append(f"| `{row['runtime']}` | `{row['skill']}` | `{row['path']}` | `{row['class']}` |")
    else:
        lines.append("None.")

    lines += [
        "",
        "## Inventory",
        "",
        "| Runtime | Skill | Class | Path | Stamp | Symlink | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| `{row['runtime']}` | `{row['skill']}` | `{row['class']}` | "
            f"`{row['path']}` | `{row['stamp']}` | `{row['symlink']}` | {row['note']} |"
        )

    lines += [
        "",
        "---",
        "Up: [[15 - Stack/Skills/SKILL-MOC|Skill MoC]]",
        "",
        "#Moon #Stack #Skill",
        "",
    ]

    output = "\n".join(lines)
    if args.output:
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output)
        print(f"Wrote {out_path}")
    else:
        print(output)


def resolve_webhook_url(config):
    """Resolve an optional URL secret from macOS Keychain without persisting it."""
    url = config["webhook_url"]
    keychain = config.get("webhook_keychain")
    if "{secret}" not in url:
        if keychain:
            raise RuntimeError("webhook_keychain requires a {secret} placeholder")
        return url
    if not isinstance(keychain, dict) or not keychain.get("service") or not keychain.get("account"):
        raise RuntimeError("webhook {secret} placeholder requires service and account")
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                keychain["service"],
                "-a",
                keychain["account"],
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("webhook Keychain lookup failed") from exc
    secret = result.stdout.strip()
    if result.returncode != 0 or not secret:
        raise RuntimeError("webhook credential unavailable in Keychain")
    return url.replace("{secret}", urllib.parse.quote(secret, safe=":-._~"))


def send_webhook(config, missing, missing_list, stale, stale_list, diverged=0, diverged_list=None):
    """POSTs a JSON body to config['webhook_url']. Works unmodified against
    Slack/Discord/Mattermost-style incoming webhooks (a {"text": "..."} body
    is enough for most of them). Services that need extra fixed fields in the
    body (Telegram's sendMessage needs chat_id alongside text, for example)
    can set:

      "webhook_extra": {"chat_id": "-100...", "parse_mode": "HTML"}
      "webhook_field": "text"   # which key holds the message (default "text",
                                 # Discord wants "content" instead)
    """
    lines = ["skillsync: real drift found", ""]
    if missing:
        lines.append(f"Missing ({missing}):")
        lines += [f"- {m}" for m in missing_list]
        lines.append("")
    if stale:
        lines.append(f"Stale ({stale}):")
        lines += [f"- {s}" for s in stale_list]
    if diverged:
        lines.append("")
        lines.append(f"Diverged ({diverged}):")
        lines += [f"- {d}" for d in (diverged_list or [])]

    field = config.get("webhook_field", "text")
    payload = dict(config.get("webhook_extra", {}))
    payload[field] = "\n".join(lines)

    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            resolve_webhook_url(config), data=body, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"(webhook post failed: {e})", file=sys.stderr)


HOOK_SCRIPT = """#!/bin/bash
# Installed by skillsync.py install-hook -- do not edit by hand.
ROOT="$(git rev-parse --show-toplevel)"
CHANGED="$(git diff --name-only HEAD~1 HEAD -- "{source_rel}" 2>/dev/null | sed -n 's#.*/\\(.*\\)\\.md#\\1#p')"
if [ -n "$CHANGED" ]; then
  (
    while IFS= read -r skill; do
      [ -n "$skill" ] && python3 "{skillsync_path}" check "$skill" --webhook --config "{config_path}" > /dev/null 2>&1
    done <<< "$CHANGED"
  ) &
  disown
fi
exit 0
"""


def cmd_install_hook(args):
    config = load_config()
    source_dir = Path(config["source_dir"]).expanduser().resolve()
    if not is_git_repo(source_dir):
        sys.exit(f"{source_dir} is not a git repo -- install-hook needs git to detect what changed.")

    hook_path = source_dir / ".git" / "hooks" / "post-commit"
    skillsync_path = Path(__file__).resolve()
    config_path = (Path.cwd() / CONFIG_FILE).resolve()
    source_rel = source_dir.name

    script = HOOK_SCRIPT.format(
        source_rel=source_rel, skillsync_path=skillsync_path, config_path=config_path
    )

    if hook_path.exists():
        existing = hook_path.read_text()
        if "Installed by skillsync.py" not in existing:
            print(f"⚠️  {hook_path} already exists and wasn't installed by skillsync.")
            print("   Append the following manually instead of overwriting it:\n")
            print(script)
            return

    hook_path.write_text(script)
    hook_path.chmod(0o755)
    print(f"Installed post-commit hook at {hook_path}")
    print("Any commit touching a skill file now triggers an immediate check.")


# Lossless folder synchronization is separate from prose porting: it never
# parses/reformats frontmatter, stamps a body, or scans runtime plugin roots.
READ_ONLY_SKILL_PARTS = {'.cursor', 'plugins', '.plugins', 'node_modules'}


def bundle_root(path):
    path = Path(os.path.abspath(str(Path(path).expanduser())))
    if any(part in READ_ONLY_SKILL_PARTS for part in path.parts):
        raise ValueError('Cursor/plugin-managed skill roots are read-only')
    if any(parent.is_symlink() for parent in [path, *path.parents]):
        raise ValueError('Skill root must not traverse a symlink')
    if path.exists() and not path.is_dir():
        raise ValueError('Skill root must be a directory')
    return path


def bundle_snapshot(folder):
    """Hash paths, bytes and executable bits, including all helper files."""
    if not folder.exists():
        return None
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('Skill folder is not a regular directory')
    digest = hashlib.sha256()
    for current, dirs, files in os.walk(folder, followlinks=False):
        for name in sorted(dirs + files):
            item = Path(current) / name
            if item.is_symlink():
                raise ValueError('Skill contains a symlink')
            if not item.stat().st_mode & 0o222:
                raise ValueError('Skill contains a read-only file or directory')
            if not item.is_dir() and not item.is_file():
                raise ValueError('Skill contains a special file')
        dirs.sort()
        for name in sorted(dirs):
            rel = (Path(current) / name).relative_to(folder).as_posix()
            digest.update(b'D' + rel.encode() + b'\0')
        for name in sorted(files):
            item = Path(current) / name
            rel = item.relative_to(folder).as_posix()
            content = item.read_bytes()
            digest.update(b'F' + rel.encode() + b'\0')
            digest.update(str(item.stat().st_mode & 0o111).encode() + b'\0')
            digest.update(str(len(content)).encode() + b'\0' + content)
    return digest.hexdigest()


def bundle_inventory(root):
    bundles, blocked, skipped = {}, set(), []
    if not root.exists():
        return bundles, blocked, skipped
    for child in sorted(root.iterdir()):
        if child.name.startswith('.') or child.name in READ_ONLY_SKILL_PARTS:
            skipped.append({'skill': child.name, 'status': 'skipped', 'reason': 'read-only or hidden entry'})
            blocked.add(child.name)
            continue
        if not SKILL_NAME_RE.fullmatch(child.name):
            continue
        if child.is_symlink():
            blocked.add(child.name)
            skipped.append({'skill': child.name, 'status': 'skipped', 'reason': 'symlink'})
            continue
        if not child.is_dir():
            continue  # Existing flat .md Core files belong to sync-exact.
        try:
            if not child.stat().st_mode & 0o222:
                raise ValueError('read-only skill directory')
            fingerprint = bundle_snapshot(child)
            if not (child / 'SKILL.md').is_file():
                raise ValueError('directory has no SKILL.md')
            bundles[child.name] = fingerprint
        except ValueError as exc:
            blocked.add(child.name)
            skipped.append({'skill': child.name, 'status': 'skipped', 'reason': str(exc)})
    return bundles, blocked, skipped


def bundle_state_path(source, target, target_name):
    if not SKILL_NAME_RE.fullmatch(target_name):
        raise ValueError('Target name must be a simple filename stem')
    root = git_root(source if source.exists() else source.parent)
    if root is None or not source.is_relative_to(root):
        raise ValueError('sync requires source_dir inside a Git checkout')
    result = subprocess.run(['git', 'rev-parse', '--absolute-git-dir'], cwd=root,
                            capture_output=True, text=True, check=True)
    key = hashlib.sha256((str(source) + '\0' + str(target)).encode()).hexdigest()[:16]
    return Path(result.stdout.strip()) / 'skillsync' / f'{target_name}-{key}.json'


def replace_bundle(origin, destination, expected_origin, expected_destination):
    """Stage a full copy and retain the old folder until replacement succeeds."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.skillsync-', dir=destination.parent))
    incoming, backup = staging / 'incoming', staging / 'backup'
    try:
        shutil.copytree(origin, incoming, copy_function=shutil.copy2)
        if (bundle_snapshot(incoming) != expected_origin or
                bundle_snapshot(origin) != expected_origin or
                bundle_snapshot(destination) != expected_destination):
            raise ValueError('Skill changed during sync; rerun to reconcile')
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(incoming, destination)
        except OSError:
            if backup.exists():
                os.replace(backup, destination)
            raise
    finally:
        shutil.rmtree(staging)


def sync_bundles(source, target, target_name, direction='both', dry_run=False):
    source, target = bundle_root(source), bundle_root(target)
    if source == target or source in target.parents or target in source.parents:
        raise ValueError('Source and target skill roots must not overlap')
    state_path = bundle_state_path(source, target, target_name)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if not isinstance(state, dict) or any(not isinstance(k, str) or not SKILL_NAME_RE.fullmatch(k) or not isinstance(v, str)
                                          or not re.fullmatch('[0-9a-f]{64}', v) for k, v in state.items()):
        raise ValueError('Invalid sync baseline; refusing to guess')
    repo, local, repo_blocked, local_blocked = {}, {}, set(), set()
    repo, repo_blocked, repo_skips = bundle_inventory(source)
    local, local_blocked, local_skips = bundle_inventory(target)
    entries = repo_skips + local_skips
    for name in sorted(set(repo) | set(local) | set(state)):
        left, right, base = repo.get(name), local.get(name), state.get(name)
        action, reason = None, ''
        if name in repo_blocked or name in local_blocked:
            status, reason = 'conflict', 'unsafe or read-only counterpart'
        elif (source / f'{name}.md').exists():
            status, reason = 'conflict', 'name collides with a legacy flat Core skill'
        elif left == right:
            status, reason = 'skipped', 'already identical'
            if left is not None:
                state[name] = left
        elif base is None and left is None:
            action = 'import'
        elif base is None and right is None:
            action = 'export'
        elif left is None or right is None:
            status, reason = 'conflict', 'previously synced skill is missing; deletion requires manual resolution'
        elif right == base and left != base:
            action = 'export'
        elif left == base and right != base:
            action = 'import'
        else:
            status, reason = 'conflict', 'both copies changed or no common baseline'
        if action:
            if direction not in ('both', action):
                status, reason = 'skipped', f'{action} excluded by direction'
            else:
                origin, dest = (target / name, source / name) if action == 'import' else (source / name, target / name)
                original, previous = (right, left) if action == 'import' else (left, right)
                status = 'added' if previous is None else 'changed'
                if not dry_run:
                    try:
                        replace_bundle(origin, dest, original, previous)
                    except ValueError as exc:
                        status, reason = 'conflict', str(exc)
                    else:
                        state[name] = original
        entry = {'skill': name, 'status': status}
        if action:
            entry['direction'] = action
        if reason:
            entry['reason'] = reason
        entries.append(entry)
    if not dry_run:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', dir=state_path.parent, delete=False) as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write('\n')
            temporary = handle.name
        os.replace(temporary, state_path)
    return {'target': target_name, 'dry_run': dry_run, 'entries': entries,
            'summary': {status: sum(e['status'] == status for e in entries)
                        for status in ('added', 'changed', 'skipped', 'conflict')}}


def sync_git(root, *arguments):
    result = subprocess.run(['git', *arguments], cwd=root, capture_output=True, text=True)
    if result.returncode:
        # Git stderr can echo a remote URL containing credentials.
        raise ValueError(f'git {arguments[0]} failed; local changes and commits are preserved')
    return result.stdout.strip()


def cmd_sync(args):
    config_path = Path(CONFIG_FILE).expanduser().absolute()
    config = load_config()
    targets = config.get('targets', {})
    if args.target not in targets and args.target != 'grokbot':
        sys.exit(f'Unknown target: {args.target}')
    def anchored(raw):
        path = Path(raw).expanduser()
        return path if path.is_absolute() else config_path.parent / path
    try:
        source = bundle_root(anchored(config['source_dir']))
        target = bundle_root(anchored(args.target_dir or targets.get(args.target, '/home/box/agent-data/workflows')))
        root = git_root(source if source.exists() else source.parent)
        if args.git and not args.dry_run:
            if root is None:
                raise ValueError('--git requires a Git checkout')
            if sync_git(root, 'status', '--porcelain'):
                raise ValueError('--git requires a clean checkout; commit or resolve your changes first')
            ahead = sync_git(root, 'rev-list', '--count', '@{upstream}..HEAD')
            if ahead != '0':
                raise ValueError('--git refuses unpublished commits; push or resolve them explicitly first')
            sync_git(root, 'pull', '--ff-only')
        report = sync_bundles(source, target, args.target, args.direction, dry_run=True)
        # After pulling, conflicts prevent any sync writes or publication.
        if not args.dry_run and not (args.git and report['summary']['conflict']):
            report = sync_bundles(source, target, args.target, args.direction)
            if args.git and not report['summary']['conflict']:
                paths = [str((source / e['skill']).relative_to(root)) for e in report['entries']
                         if e['status'] in ('added', 'changed') and e.get('direction') == 'import']
                if paths:
                    sync_git(root, 'add', '--', *paths)
                    sync_git(root, 'commit', '-m', f'sync: import {args.target} skill bundles', '--', *paths)
                    sync_git(root, 'push')
        report['git'] = 'dry-run: no pull, commit or push' if args.git and args.dry_run else bool(args.git)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            for entry in report['entries']:
                print(f"{entry['status'].upper():8} {entry['skill']} {entry.get('direction', '')} {entry.get('reason', '')}".rstrip())
            print(('Dry run: ' if report['dry_run'] else 'Summary: ') + ', '.join(
                f'{count} {status}' for status, count in report['summary'].items()))
            if args.git and args.dry_run:
                print(report['git'])
        if report['summary']['conflict']:
            sys.exit(1)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        sys.exit(str(exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"skillsync {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="write a starter skillsync.json in the current directory")

    p_bundle_sync = sub.add_parser("sync", help="lossless bidirectional skill-folder sync with conflicts")
    p_bundle_sync.add_argument("--target", required=True, help="configured runtime target (grokbot defaults to its user workflows)")
    p_bundle_sync.add_argument("--target-dir", help="override the target's user-owned skills directory")
    p_bundle_sync.add_argument("--direction", choices=("both", "import", "export"), default="both")
    p_bundle_sync.add_argument("--dry-run", action="store_true", help="report without changing files or Git")
    p_bundle_sync.add_argument("--git", action="store_true", help="fast-forward pull, sync, commit imported bundles and push")
    p_bundle_sync.add_argument("--json", action="store_true", help="emit a structured summary")
    p_bundle_sync.add_argument("--config", help="path to skillsync.json")

    p_stamp = sub.add_parser("stamp", help="mark port(s) as synced to the current source version")
    p_stamp.add_argument("skill", nargs="?", help="skill name (omit with --all)")
    p_stamp.add_argument("--all", action="store_true", help="stamp every skill")

    p_sync = sub.add_parser("sync-exact", help="propagate canonical bodies without overwriting unreviewed runtime divergence")
    p_sync.add_argument("skill", nargs="?", help="skill name (omit with --all)")
    p_sync.add_argument("--all", action="store_true", help="sync every skill")
    p_sync.add_argument("--reviewed", action="store_true", help="allow overwrite of diverged or unstamped ports after propose-upstream review")
    p_sync.add_argument("--create-missing", action="store_true", help="create a missing managed Core port from reviewed canonical source")

    p_prepare = sub.add_parser("prepare-discovery", help="add native loader metadata to one reviewed local adaptation without stamping parity")
    p_prepare.add_argument("skill", help="managed skill name")
    p_prepare.add_argument("--target", required=True, help="runtime target containing the adaptation")
    p_prepare.add_argument("--reviewed", action="store_true", help="confirm the local adaptation was reviewed")
    p_prepare.add_argument("--config", help="path to a specific skillsync.json (default: ./skillsync.json)")

    p_compact = sub.add_parser("compact-descriptions", help="compact native discovery metadata without changing skill instruction bodies")
    p_compact.add_argument("target", help="runtime target whose local skill descriptions are reviewed")
    p_compact.add_argument("--max-chars", type=int, default=DEFAULT_DISCOVERY_DESCRIPTION_MAX_CHARS, help="maximum discovery-description length (default: 160)")
    p_compact.add_argument("--reviewed", action="store_true", help="confirm the target metadata was reviewed for compaction")
    p_compact.add_argument("--include-unmanaged", action="store_true", help="allow a reviewed full-target metadata pass when no managed_root is configured")
    p_compact.add_argument("--config", help="path to a specific skillsync.json (default: ./skillsync.json)")

    p_check = sub.add_parser("check", help="report missing/stale ports")
    p_check.add_argument("skill", nargs="?", help="check only this skill")
    p_check.add_argument("--fail-on-drift", action="store_true", help="exit 1 if anything is out of sync")
    p_check.add_argument("--webhook", action="store_true", help="POST to webhook_url on real drift")
    p_check.add_argument("--config", help="path to a specific skillsync.json (default: ./skillsync.json)")

    p_registry = sub.add_parser("registry", help="emit a generated inventory of all target skills")
    p_registry.add_argument("--output", help="write markdown to this path instead of stdout")

    p_catalog = sub.add_parser("catalog-audit", help="measure a model-visible skill catalog against an explicit context budget")
    p_catalog.add_argument("profile", help="catalog_profiles entry to audit")
    p_catalog.add_argument("--json", action="store_true", help="emit the machine-readable audit report")
    p_catalog.add_argument("--strict", action="store_true", help="exit 1 for any configured catalog-policy violation")
    p_catalog.add_argument("--config", help="path to a specific skillsync.json (default: ./skillsync.json)")

    p_catalog_search = sub.add_parser("catalog-search", help="search a full skill library without adding it to startup context")
    p_catalog_search.add_argument("profile", help="catalog_profiles entry to search")
    p_catalog_search.add_argument("query", nargs="+", help="task terms or capability to find")
    p_catalog_search.add_argument("--limit", type=int, default=8, help="maximum results (default: 8)")
    p_catalog_search.add_argument("--json", action="store_true", help="emit machine-readable results")
    p_catalog_search.add_argument("--config", help="path to a specific skillsync.json (default: ./skillsync.json)")

    p_catalog_read = sub.add_parser("catalog-read", help="read one audited skill from a full library")
    p_catalog_read.add_argument("profile", help="catalog_profiles entry to read from")
    p_catalog_read.add_argument("skill", help="exact declared skill name")
    p_catalog_read.add_argument("--path", help="required when the declared skill name is ambiguous")
    p_catalog_read.add_argument("--max-chars", type=int, default=DEFAULT_CATALOG_READ_MAX_CHARS, help="maximum instruction payload to emit, 1-16000 (default: 16000)")
    p_catalog_read.add_argument("--json", action="store_true", help="emit machine-readable skill content")
    p_catalog_read.add_argument("--config", help="path to a specific skillsync.json (default: ./skillsync.json)")

    p_router = sub.add_parser("install-catalog-router", help="install one small native router skill for an on-demand catalog")
    p_router.add_argument("target", help="runtime target where the router should be discoverable")
    p_router.add_argument("--profile", required=True, help="catalog profile the router searches")
    p_router.add_argument("--reviewed", action="store_true", help="confirm this target should expose the router")
    p_router.add_argument("--force", action="store_true", help="replace an existing reviewed router")
    p_router.add_argument("--config", help="path to a specific skillsync.json (default: ./skillsync.json)")

    sub.add_parser("install-hook", help="install a git post-commit hook in the source repo")

    p_learn = sub.add_parser("learn-format", help="infer a target's frontmatter shape from its existing skills")
    p_learn.add_argument("target", nargs="?", help="target name (omit with --all)")
    p_learn.add_argument("--all", action="store_true", help="learn every target")

    p_scaffold = sub.add_parser("scaffold", help="draft a new port in a target's learned shape (needs manual review)")
    p_scaffold.add_argument("skill", help="skill name")
    p_scaffold.add_argument("target", help="target name")
    p_scaffold.add_argument("--force", action="store_true", help="overwrite an existing draft")

    p_upstream = sub.add_parser("propose-upstream", help="show a read-only runtime-to-Core proposal")
    p_upstream.add_argument("skill", help="canonical Core skill name")
    p_upstream.add_argument("--target", required=True, help="runtime target containing the local improvement")
    p_upstream.add_argument("--output", help="write the proposal report to a local file")
    p_upstream.add_argument("--config", help="path to a specific skillsync.json (default: ./skillsync.json)")

    p_candidate = sub.add_parser("promote-candidate", help="emit a review-only packet for one runtime-local skill")
    p_candidate.add_argument("target", help="runtime target containing the local skill")
    p_candidate.add_argument("skill", help="runtime-local skill name")
    p_candidate.add_argument("--output", help="write the candidate packet outside source/target roots")
    p_candidate.add_argument("--config", help="path to a specific skillsync.json (default: ./skillsync.json)")

    p_snapshot = sub.add_parser("capability-snapshot", help="emit managed Core parity without claiming native discovery")
    p_snapshot.add_argument("--output", help="write JSON outside source/target roots")
    p_snapshot.add_argument("--config", help="path to a specific skillsync.json (default: ./skillsync.json)")

    args = parser.parse_args()

    global CONFIG_FILE
    if getattr(args, "config", None):
        CONFIG_FILE = args.config

    {
        "init": cmd_init,
        "sync": cmd_sync,
        "stamp": cmd_stamp,
        "sync-exact": cmd_sync_exact,
        "prepare-discovery": cmd_prepare_discovery,
        "compact-descriptions": cmd_compact_descriptions,
        "check": cmd_check,
        "registry": cmd_registry,
        "catalog-audit": cmd_catalog_audit,
        "catalog-search": cmd_catalog_search,
        "catalog-read": cmd_catalog_read,
        "install-catalog-router": cmd_install_catalog_router,
        "install-hook": cmd_install_hook,
        "learn-format": cmd_learn_format,
        "scaffold": cmd_scaffold,
        "propose-upstream": cmd_propose_upstream,
        "promote-candidate": cmd_promote_candidate,
        "capability-snapshot": cmd_capability_snapshot,
    }[args.command](args)


if __name__ == "__main__":
    main()
