# skillsync

Keep AI agent "skill" files in sync across multiple harnesses, without false alarms.

## The problem

Claude Code, Codex, Grok, and most other agent runtimes now support some form of
skill/instruction file (usually named `SKILL.md`), but each expects it in its own
folder with its own conventions. The moment you maintain the same skill for more
than one harness, you get N copies. Nobody notices when they drift apart until an
agent runs on stale instructions.

`skillsync` does **not** translate content between formats. Writing a good
skill for a specific harness is a rewriting task, not a mechanical one, so
that part stays a human (or LLM) job. What it does instead:

1. Tracks one canonical **source directory** for your skill files.
2. Stamps each ported copy with a marker recording exactly which version of
   the source it reflects.
3. Compares stamps against the source's *current* version and reports which
   ports are **missing** or **out of date**.
4. Optionally fires a webhook when real drift is found, and optionally
   installs a git hook so drift is caught the moment the source changes.

## Why this doesn't produce false alarms

The obvious approach is comparing file timestamps: "has the source file been
touched since the port was written?" That's what this tool started as, and it
produced false positives on every git checkout, clone, or `mv` regardless of
whether the actual content changed. Timestamps get reset by things that have
nothing to do with content.

`skillsync` compares **versions**, not clocks:

- If the source directory is a git repo, it uses `git log -1` on the specific
  file, a real content-change signal that only moves on an actual commit
  touching that file.
- If the source isn't a git repo, it falls back to a content hash, so the
  tool still works on a plain folder with no version control.

Either way, moving files, cloning the repo, or checking out a branch can
never trigger a false "stale" flag. Only a genuine edit can.

## Install

No dependencies beyond Python 3.9+.

```bash
curl -O https://raw.githubusercontent.com/nordsym/skillsync/main/skillsync.py
chmod +x skillsync.py
```

## Quickstart

```bash
./skillsync.py init
# edit skillsync.json: set source_dir and your target harness folders

./skillsync.py stamp --all
# marks every currently-synced port as up to date

./skillsync.py check
# OK / MISSING / STALE per skill per target

./skillsync.py check --fail-on-drift
# exit 1 if anything is out of sync, for CI

./skillsync.py install-hook
# (git sources only) fires a check automatically on every commit that
# touches a skill file, instead of waiting for a scheduled run
```

## Config (`skillsync.json`)

```json
{
  "source_dir": "./skills",
  "targets": {
    "claude": "~/.claude/skills",
    "codex": "~/.codex/skills",
    "agents": "~/.agents/skills"
  },
  "webhook_url": null
}
```

- `source_dir`: your canonical skill files, one `.md` per skill.
- `targets`: name to directory. Convention is `<target_dir>/<skill_name>/SKILL.md`,
  matching what Claude Code, Codex, and most current agent harnesses expect.
- `webhook_url`: optional. Any endpoint that accepts a JSON POST with a
  `text` field (Slack incoming webhooks, Discord, a custom endpoint, etc.).
  Fired only when real drift is found, and only when `--webhook` is passed.

## Typical workflow

1. Write or edit a skill in your source directory.
2. Manually adapt it into each target harness's native format (frontmatter
   conventions differ per runtime, that part stays your job or your agent's).
3. Run `skillsync.py stamp <skill-name>` to mark the ports as current.
4. Commit the source. If you installed the hook, any future edit that
   doesn't get re-stamped will surface automatically on the next commit,
   not silently.

## License

MIT. See [LICENSE](LICENSE).
