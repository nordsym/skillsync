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
  3. Compare stamps against the source's *current* version and report which
     ports are missing or out of date. This is a real-content comparison,
     not a file-timestamp comparison -- moving, cloning, or checking out the
     source repo can never produce a false positive.
  4. Optionally fire a webhook when real drift is found, and optionally
     install a git post-commit hook so drift is caught the moment the
     source changes, not on the next scheduled check.
  5. Learn each target's frontmatter *shape* (not its prose) from the
     skills already there, and scaffold a draft in that shape for a new
     port, pre-filled with the target's fixed fields and the source's raw
     content for a human/agent to actually adapt. Never auto-stamped, a
     scaffold is a starting point, not a finished port.

Zero dependencies beyond the Python 3.9+ standard library.

Usage:
  skillsync.py init                                  # write skillsync.json in the current dir
  skillsync.py stamp [<skill>] [--all]                # mark port(s) as synced to the current source version
  skillsync.py sync-exact [<skill>] [--all] [--reviewed]
                                                        # propagate canonical body with divergence guard
  skillsync.py check [<skill>] [--fail-on-drift] [--webhook]
  skillsync.py registry [--output <path>]             # emit a generated inventory of all target skills
  skillsync.py install-hook                           # add a post-commit hook to the source repo (git sources only)
  skillsync.py learn-format [<target>] [--all]        # infer a target's frontmatter shape from its existing skills
  skillsync.py scaffold <skill> <target> [--force]    # draft a new port in the learned shape, needs manual review
  skillsync.py propose-upstream <skill> --target <target> [--output <path>]
                                                        # read-only runtime-to-source proposal
"""
import argparse
import difflib
from datetime import datetime, timezone
import hashlib
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

__version__ = "0.3.1"
CONFIG_FILE = "skillsync.json"
MARKER_RE = re.compile(r"<!-- synced-from: [0-9a-f]+ -->\n?")
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


def find_skills(source_dir: Path):
    return sorted(p for p in source_dir.glob("*.md") if p.is_file())


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
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.skill):
        sys.exit("Skill name must be a simple filename stem with no path separators")
    source_file = source_dir / f"{args.skill}.md"
    if not source_file.exists():
        sys.exit(f"No canonical Core skill named '{args.skill}' in {source_dir}")
    if args.target not in config["targets"]:
        sys.exit(f"Unknown target '{args.target}'. Known: {', '.join(config['targets'])}")
    runtime_file = target_file(config["targets"][args.target], args.skill)
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
    config = load_config()
    source_dir = Path(config["source_dir"]).expanduser().resolve()
    src = source_dir / f"{args.skill}.md"
    if not src.exists():
        sys.exit(f"No source file for '{args.skill}' in {source_dir}")

    if args.target not in config["targets"]:
        sys.exit(f"Unknown target '{args.target}'. Known: {', '.join(config['targets'])}")
    target_dir = config["targets"][args.target]

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
            dest = target_file(target_dir, name)
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
        canonical = stamp_content(skill_file.read_text(), version)
        for target_name, target_dir in config["targets"].items():
            dest = target_file(target_dir, name)
            if not dest.exists():
                print(f"MISSING  {target_name}:{name}")
                refused += 1
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
            mode = "reviewed" if not unchanged_from_base else "exact-base"
            print(f"SYNCED   {target_name}:{name} -> {version} ({mode})")
            synced += 1

    print(f"\nSummary: SYNCED: {synced}   REFUSED/MISSING: {refused}")
    if refused:
        sys.exit(1)


def cmd_check(args):
    config = load_config()
    source_dir = Path(config["source_dir"]).expanduser().resolve()
    skills = find_skills(source_dir)
    if args.skill:
        skills = [s for s in skills if s.stem == args.skill]
        if not skills:
            sys.exit(f"No skill named '{args.skill}' in {source_dir}")

    missing, stale, ok = 0, 0, 0
    missing_list, stale_list = [], []

    print("skillsync check")
    print(f"Source: {source_dir}\n")

    for skill_file in skills:
        name = skill_file.stem
        current = source_version(source_dir, skill_file)
        for target_name, target_dir in config["targets"].items():
            dest = target_file(target_dir, name)
            if not dest.exists():
                print(f"MISSING  {target_name}:{name}")
                missing_list.append(f"{target_name}:{name}")
                missing += 1
                continue
            stamped = read_stamp(dest.read_text())
            if stamped is None:
                print(f"UNSTAMPED {target_name}:{name} (never stamped -- run 'skillsync.py stamp')")
                stale_list.append(f"{target_name}:{name} (unstamped)")
                stale += 1
            elif stamped != current:
                print(f"STALE    {target_name}:{name} (stamped {stamped}, source now {current})")
                stale_list.append(f"{target_name}:{name} (source moved to {current})")
                stale += 1
            else:
                ok += 1

    total = len(skills)
    n_targets = len(config["targets"])
    print(f"\nSummary: {total} skill(s) x {n_targets} target(s) = {total * n_targets} expected ports.")
    print(f"OK: {ok}   MISSING: {missing}   STALE: {stale}")

    if (missing or stale) and args.webhook and config.get("webhook_url"):
        send_webhook(config, missing, missing_list, stale, stale_list)

    if args.fail_on_drift and (missing or stale):
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

        core_targets = {name: target_file(target_raw, name) for name in core_names}
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


def send_webhook(config, missing, missing_list, stale, stale_list):
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"skillsync {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="write a starter skillsync.json in the current directory")

    p_stamp = sub.add_parser("stamp", help="mark port(s) as synced to the current source version")
    p_stamp.add_argument("skill", nargs="?", help="skill name (omit with --all)")
    p_stamp.add_argument("--all", action="store_true", help="stamp every skill")

    p_sync = sub.add_parser("sync-exact", help="propagate canonical bodies without overwriting unreviewed runtime divergence")
    p_sync.add_argument("skill", nargs="?", help="skill name (omit with --all)")
    p_sync.add_argument("--all", action="store_true", help="sync every skill")
    p_sync.add_argument("--reviewed", action="store_true", help="allow overwrite of diverged or unstamped ports after propose-upstream review")

    p_check = sub.add_parser("check", help="report missing/stale ports")
    p_check.add_argument("skill", nargs="?", help="check only this skill")
    p_check.add_argument("--fail-on-drift", action="store_true", help="exit 1 if anything is out of sync")
    p_check.add_argument("--webhook", action="store_true", help="POST to webhook_url on real drift")
    p_check.add_argument("--config", help="path to a specific skillsync.json (default: ./skillsync.json)")

    p_registry = sub.add_parser("registry", help="emit a generated inventory of all target skills")
    p_registry.add_argument("--output", help="write markdown to this path instead of stdout")

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

    args = parser.parse_args()

    global CONFIG_FILE
    if getattr(args, "config", None):
        CONFIG_FILE = args.config

    {
        "init": cmd_init,
        "stamp": cmd_stamp,
        "sync-exact": cmd_sync_exact,
        "check": cmd_check,
        "registry": cmd_registry,
        "install-hook": cmd_install_hook,
        "learn-format": cmd_learn_format,
        "scaffold": cmd_scaffold,
        "propose-upstream": cmd_propose_upstream,
    }[args.command](args)


if __name__ == "__main__":
    main()
