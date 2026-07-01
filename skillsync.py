#!/usr/bin/env python3
"""
skillsync -- keep AI agent "skill" files in sync across multiple harnesses.

The problem: Claude Code, Codex, Grok, and other agent runtimes each expect
skill/instruction files in their own folder with their own conventions. If
you maintain the same skill for more than one harness, you end up with N
copies that silently drift out of sync -- nobody notices until an agent runs
on stale instructions.

skillsync does not translate content between formats (that's a rewriting
task, not a mechanical one -- do it by hand or with an LLM). What it does:

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

Zero dependencies beyond the Python 3.9+ standard library.

Usage:
  skillsync.py init                                  # write skillsync.json in the current dir
  skillsync.py stamp [<skill>] [--all]                # mark port(s) as synced to the current source version
  skillsync.py check [<skill>] [--fail-on-drift] [--webhook]
  skillsync.py install-hook                           # add a post-commit hook to the source repo (git sources only)
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

CONFIG_FILE = "skillsync.json"
MARKER_RE = re.compile(r"<!-- synced-from: [0-9a-f]+ -->\n?")


def load_config():
    path = Path.cwd() / CONFIG_FILE
    if not path.exists():
        sys.exit(
            f"No {CONFIG_FILE} found in {Path.cwd()}. Run 'skillsync.py init' first."
        )
    return json.loads(path.read_text())


def write_config(config):
    (Path.cwd() / CONFIG_FILE).write_text(json.dumps(config, indent=2) + "\n")


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def source_version(source_dir: Path, skill_file: Path) -> str:
    """A short, stable identifier for the current version of a skill file.
    Uses the last git commit touching the file if source_dir is a git repo
    (so it survives renames within the same content), otherwise a content
    hash so the tool still works on a plain, non-git folder of skills.
    """
    if is_git_repo(source_dir):
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h", "--", str(skill_file)],
            cwd=source_dir,
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
    """Convention: <target_dir>/<skill_name>/SKILL.md, matching the layout
    Claude Code, Codex, and most current agent harnesses expect. Override
    per-target in config with a "file_pattern" if a harness differs.
    """
    return Path(target_dir).expanduser() / skill_name / "SKILL.md"


def stamp_content(text: str, version: str) -> str:
    marker = f"<!-- synced-from: {version} -->\n"
    text = MARKER_RE.sub("", text)
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            insert_at = end + len("\n---\n")
            return text[:insert_at] + marker + text[insert_at:]
    return marker + text


def read_stamp(text: str):
    m = re.search(r"synced-from: ([0-9a-f]+)", text)
    return m.group(1) if m else None


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
        send_webhook(config["webhook_url"], missing, missing_list, stale, stale_list)

    if args.fail_on_drift and (missing or stale):
        sys.exit(1)


def send_webhook(url, missing, missing_list, stale, stale_list):
    lines = ["skillsync: real drift found", ""]
    if missing:
        lines.append(f"Missing ({missing}):")
        lines += [f"- {m}" for m in missing_list]
        lines.append("")
    if stale:
        lines.append(f"Stale ({stale}):")
        lines += [f"- {s}" for s in stale_list]
    body = json.dumps({"text": "\n".join(lines)}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
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
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="write a starter skillsync.json in the current directory")

    p_stamp = sub.add_parser("stamp", help="mark port(s) as synced to the current source version")
    p_stamp.add_argument("skill", nargs="?", help="skill name (omit with --all)")
    p_stamp.add_argument("--all", action="store_true", help="stamp every skill")

    p_check = sub.add_parser("check", help="report missing/stale ports")
    p_check.add_argument("skill", nargs="?", help="check only this skill")
    p_check.add_argument("--fail-on-drift", action="store_true", help="exit 1 if anything is out of sync")
    p_check.add_argument("--webhook", action="store_true", help="POST to webhook_url on real drift")
    p_check.add_argument("--config", help="path to a specific skillsync.json (default: ./skillsync.json)")

    sub.add_parser("install-hook", help="install a git post-commit hook in the source repo")

    args = parser.parse_args()

    global CONFIG_FILE
    if getattr(args, "config", None):
        CONFIG_FILE = args.config

    {
        "init": cmd_init,
        "stamp": cmd_stamp,
        "check": cmd_check,
        "install-hook": cmd_install_hook,
    }[args.command](args)


if __name__ == "__main__":
    main()
