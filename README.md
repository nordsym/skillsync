# skillsync

Keep AI agent "skill" files in sync across multiple harnesses, without false alarms.

## The problem

Claude Code, Codex, Grok, and most other agent runtimes now support some form of
skill/instruction file (usually named `SKILL.md`), but each expects it in its own
folder with its own conventions. The moment you maintain the same skill for more
than one harness, you get N copies. Nobody notices when they drift apart until an
agent runs on stale instructions.

`skillsync` does **not** translate skill *prose* between formats. Writing a
good skill for a specific harness is a rewriting task, an LLM or a human does
it better than a script ever will, so that part stays deliberate. What it
does mechanically:

1. Tracks one canonical **source directory** for your skill files.
2. Stamps each ported copy with a marker recording exactly which version of
   the source it reflects.
3. Compares stamps against the source's *current* version and reports which
   ports are **missing** or **out of date**.
4. Strips source-only vault wrappers when stamping a port: leading YAML or
   preamble before the first H1, plus trailing Obsidian `Up:`/hashtag footers.
   Runtime ports keep the skill body, not the source repo's navigation
   metadata.
5. Optionally fires a webhook when real drift is found, and optionally
   installs a git hook so drift is caught the moment the source changes.
6. Learns each target's frontmatter *shape* (fields, whether it uses
   frontmatter at all, whether skills live flat or under a category folder)
   from the skills already there, and scaffolds a draft in that shape for a
   new port. Never auto-stamped, a scaffold is a starting point for a human
   or agent to actually adapt, not a finished translation.
7. Produces a read-only upstream proposal when a runtime-local skill has
   learned something worth reviewing for canonical source. It normalizes
   wrappers, shows a unified diff, detects two-sided conflicts when a stamped
   git base is available, and never writes to the source or runtime port.

## Why this doesn't produce false alarms

The obvious approach is comparing file timestamps: "has the source file been
touched since the port was written?" That's what this tool started as, and it
produced false positives on every git checkout, clone, or `mv` regardless of
whether the actual content changed. Timestamps get reset by things that have
nothing to do with content.

`skillsync` compares **versions**, not clocks:

- If the source directory is inside a git repo, it uses `git log -1` on the
  specific file, a real content-change signal that only moves on an actual
  commit touching that file.
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

./skillsync.py sync-exact --all
# propagates canonical bodies only when each runtime still matches its stamped
# base; refuses runtime-local divergence instead of overwriting it

./skillsync.py check
# OK / MISSING / STALE per skill per target

./skillsync.py check --fail-on-drift
# exit 1 if anything is out of sync, for CI

./skillsync.py registry --output SKILL-REGISTRY.md
# writes a generated inventory of every target skill, including core ports,
# local skills, vendor skills, archives, duplicates, and symlink rows

./skillsync.py install-hook
# (git sources only) fires a check automatically on every commit that
# touches a skill file, instead of waiting for a scheduled run

./skillsync.py learn-format --all
# infers each target's frontmatter shape (fields, flat vs categorized
# layout) from the skills already ported there

./skillsync.py scaffold <skill-name> <target-name>
# drafts a new port in the learned shape, placed at the right path,
# pre-filled with fixed fields and the raw source content -- never
# auto-stamped, review and rewrite the prose before 'stamp'

./skillsync.py propose-upstream <skill-name> --target <target-name>
# prints a read-only, classified runtime-to-source diff for review

./skillsync.py sync-exact <skill-name> --reviewed
# after reviewing a refused port, explicitly accept canonical Core for it
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
- `targets`: name to directory. Skills are found by searching recursively for
  `<skill_name>/SKILL.md` under the target directory, so both flat layouts
  (`<target_dir>/<skill_name>/SKILL.md`, what Claude Code, Codex, and
  OpenClaw use) and categorized layouts (`<target_dir>/<category>/<skill_name>/SKILL.md`,
  what Hermes uses) work without extra configuration.
- `webhook_url`: optional. Any endpoint that accepts a JSON POST with a
  `text` field (Slack incoming webhooks, Discord, a custom endpoint, etc.).
  Fired only when real drift is found, and only when `--webhook` is passed.

## Typical workflow

1. Write or edit a skill in your source directory.
2. Adapt it into each target harness's native format. `scaffold` gets you a
   correctly-shaped starting point (right frontmatter fields, right folder
   depth), the actual prose adaptation is still your job or your agent's.
3. Run `skillsync.py stamp <skill-name>` to mark the ports as current.
4. Commit the source. If you installed the hook, any future edit that
   doesn't get re-stamped will surface automatically on the next commit,
   not silently.

For agent-agnostic Core skills whose runtime ports are intended to be exact,
use `sync-exact` after the source commit. It verifies that each runtime still
matches the body at its existing stamp before replacing it. If a runtime has
learned something locally, the command refuses that port. Review it with
`propose-upstream`, promote any useful learning into Core, then rerun with
`--reviewed` only when choosing canonical Core deliberately.

## Registry

`registry` emits a generated markdown inventory across every configured
target. It is intentionally broader than `check`: `check` only asks whether
the governed source skills have current ports, while `registry` also shows
runtime-local skills, vendor skills, archived skills, symlinked rows, and
duplicate names that could mask a governed port.

```bash
./skillsync.py registry --output SKILL-REGISTRY.md
```

Use this when the problem is catalog visibility rather than drift.

## Upstream proposals

Runtime agents sometimes improve their local copy of a skill. Do not copy that
file over canonical source or silently distribute it to every runtime. Generate
a proposal instead:

```bash
./skillsync.py propose-upstream nordsym-state --target hermes
./skillsync.py propose-upstream nordsym-state --target hermes --output /tmp/nordsym-state-upstream.diff
```

The command strips source-only wrappers and the sync marker before comparison.
If the runtime stamp points to an available git commit, it uses that version as
the merge base. `CONFLICT` means both source and runtime changed since that base.
`CORE_CANDIDATE` means the runtime contains a substantive candidate change, not
that the change has been approved for Core. `RUNTIME_ONLY` means the runtime did
not diverge from its base while canonical source moved. `NO_CHANGE` means the
normalized bodies match. The command performs no writes unless `--output` is
explicitly supplied, and that write contains only the report.

## Why this doesn't auto-generate the full port

A tool that mechanically infers frontmatter *shape* is safe: getting a field
name wrong is obvious and harmless. A tool that auto-generates skill *prose*
via an LLM and silently ships it is a different risk entirely, a subtly
wrong instruction can make an agent behave incorrectly in production, and
that shouldn't happen without a human or agent actually reading the result.
`scaffold` deliberately stops at the shape. If you want full LLM-assisted
drafting, wire your own model call around the source content, review its
output, then run `stamp` yourself. Keeping that step manual is the point,
not a missing feature.

## License

MIT. See [LICENSE](LICENSE).
