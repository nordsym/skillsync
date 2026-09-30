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
3. Compares stamps and normalized bodies against the source's *current*
   version and reports ports that are **missing**, **out of date**, or
   **semantically diverged**. A current stamp alone is never a green claim.
4. Strips source-only vault wrappers when stamping a port: leading YAML or
   preamble before the first H1, plus trailing Obsidian `Up:`/hashtag footers.
   Runtime ports keep the skill body, not the source repo's navigation
   metadata.
5. Optionally fires a webhook when real drift is found, and optionally
   installs a git hook so drift is caught the moment the source changes.
6. Renders exact Core ports with portable `name` and `description`
   frontmatter at byte zero, plus a source-version marker. It never generates
   or rewrites instruction prose.
7. Learns each target's frontmatter *shape* (fields, whether it uses
   frontmatter at all, whether skills live flat or under a category folder)
   from the skills already there, and scaffolds a draft in that shape for a
   new port. Never auto-stamped, a scaffold is a starting point for a human
   or agent to actually adapt, not a finished translation.
8. Produces a read-only upstream proposal when a runtime-local skill has
   learned something worth reviewing for canonical source. It normalizes
   wrappers, shows a unified diff, detects two-sided conflicts when a stamped
   git base is available, and never writes to the source or runtime port.
9. Audits the exact skill roots a model can see before the host trims the
   catalog. It reports duplicate names, long descriptions, missing metadata,
   entry count, and a deliberately conservative rendered-list token estimate.

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

./skillsync.py sync-exact --all --reviewed --create-missing
# explicit managed-Core rollout after reviewing divergence. It creates only
# missing ports in a configured managed root.

./skillsync.py check
# OK / MISSING / STALE per skill per target

./skillsync.py check --fail-on-drift
# exit 1 if anything is out of sync, for CI

./skillsync.py registry --output SKILL-REGISTRY.md
# writes a generated inventory of every target skill, including core ports,
# local skills, vendor skills, archives, duplicates, and symlink rows

./skillsync.py catalog-audit general --strict
# checks the configured model-visible `general` profile and exits 1 on a
# configured catalog-budget or metadata violation

./skillsync.py catalog-search full-library "deploy a Vercel site" --json
# finds a relevant skill in the complete library without adding the entire
# library to the model-visible startup list

./skillsync.py catalog-read full-library vercel-deploy --path /absolute/path/to/SKILL.md
# reads one audited skill after search selected it. An ambiguous name requires
# its exact returned path. It emits at most 16,000 instruction characters by
# hard ceiling, so one oversized skill cannot recreate the startup-context failure.

./skillsync.py compact-descriptions codex --reviewed
# shortens only native discovery descriptions in one reviewed target. It never
# changes the instruction body below the frontmatter.

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

./skillsync.py promote-candidate <target-name> <local-skill-name> --output /tmp/candidate.json
# emits provenance and risk flags for a local skill. It never copies, enables,
# or promotes it.

./skillsync.py capability-snapshot --output /tmp/core-port-parity.json
# emits managed port parity. Native discovery stays explicitly unobserved until
# each runtime's own loader acceptance has run.

./skillsync.py prepare-discovery <skill-name> --target hermes --reviewed
# adds standard native-loader metadata to a reviewed local adaptation without
# stamping it as canonical. Drift remains visible until semantic reconciliation.

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
  "managed_roots": {
    "hermes": "~/.hermes/skills/nordsym"
  },
  "target_adapters": {
    "codex": { "openai_yaml": true }
  },
  "catalog_profiles": {
    "general": {
      "roots": ["codex"],
      "max_entries": 48,
      "max_description_chars": 160,
      "entry_overhead_tokens": 8,
      "budget_tokens": 3500,
      "fail_on_duplicates": true
    }
  },
  "catalog_router_targets": {
    "desktop": "~/.codex/skills"
  },
  "webhook_url": null,
  "webhook_keychain": null
}
```

- `source_dir`: your canonical skill files, one `.md` per skill.
- `targets`: name to directory. Skills are found by searching recursively for
  `<skill_name>/SKILL.md` under the target directory, so both flat layouts
  (`<target_dir>/<skill_name>/SKILL.md`, what Claude Code, Codex, and
  OpenClaw use) and categorized layouts (`<target_dir>/<category>/<skill_name>/SKILL.md`,
  what Hermes uses) work without extra configuration.
- `managed_roots`: optional per-runtime dedicated landing zones for governed
  Core. Use this when a runtime has self-evolved skills that must never be
  mistaken for or overwritten as a Core port.
- `target_adapters`: optional native UI metadata. `openai_yaml` emits only
  Codex's display name, short description, and explicit `$skill` prompt. It
  never adds a tool, MCP, credential, identity, or policy grant.
- `catalog_profiles`: explicit model-exposure contracts. `roots` can name an
  existing target, a filesystem path, `{ "name": "...", "path": "..." }`,
  or `{ "codex_config": "~/.codex/config.toml" }` to resolve only the
  currently enabled Codex plugin skill roots from its local cache. Use
  `{ "plugin_cache": "~/.codex/plugins/cache" }` for a full on-demand
  library, including specialist plugins that are not active at startup.
  The audit recursively measures every `SKILL.md` in those roots because that
  is what native loaders normally see. It never silently deduplicates a
  catalog for the estimate. Keep the general profile small and use specialist
  profiles for design, web, finance, and other dense packs. `budget_tokens`
  is a conservative heuristic, not a claim about a model's hidden context
  window: `ceil((skill name + separator + capped description) characters / 4)
  + entry_overhead_tokens` per visible skill. Set `exclude_parts` only when the real loader excludes those
  path components too. Set `fail_on_metadata` to `false` only for third-party
  plugin roots whose upstream descriptions you cannot yet change. The audit
  still reports that drift, but `--strict` will focus on actual admission
  limits and duplicate-name policy.
- `catalog_router_targets`: optional native skill roots that receive the
  tiny on-demand router without becoming a synchronized Core target. Use this
  for a host-specific shell such as a desktop app.
- `webhook_url`: optional. Any endpoint that accepts a JSON POST with a
  `text` field (Slack incoming webhooks, Discord, a custom endpoint, etc.).
  Fired only when real drift is found, and only when `--webhook` is passed.
- `webhook_keychain`: optional macOS Keychain reference with `service` and
  `account`. Put `{secret}` in `webhook_url`; skillsync resolves it only in
  memory and fails closed when the credential is unavailable.

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

## Managed Core and local evolution

Runtime-local, vendor, experimental, and client-bound skills are not portable
just because they sit in one harness. A `managed_roots` entry keeps governed
Core separate from local evolution. `sync-exact --create-missing` writes only
there and never overwrites a same-named local skill.

Use `promote-candidate` to create a read-only provenance/risk packet for a
local skill. Promotion still requires an explicit Core review, chosen target
allowlist, and native-loader acceptance. A visible Core skill never grants
tools, credentials, identity, client access, or execution authority.

For a reviewed local adaptation that needs native discovery before its semantic
promotion is decided, `prepare-discovery` may add only the standard `name` and
`description` header. It never adds a source stamp, so `check` remains truthful
about the unresolved semantic drift.

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

## Catalog budgets and profiles

Skill discovery is not free context. A catalog with a few hundred skills can
overflow a host's dynamic skills budget even if every individual description
looks reasonable. When that happens the host may remove descriptions and then
drop skills entirely. Shorter prose helps, but it cannot compensate for an
unbounded catalog.

Use `catalog-audit` as the admission check for every model-visible profile:

```bash
./skillsync.py catalog-audit general --json
./skillsync.py catalog-audit general --strict
```

The command is read-only. It does not turn plugins on or off, and it does not
claim that all host runtimes share the same context limit. Its job is to make
the exposure contract explicit and fail CI before a configured profile grows
past its own safe budget. Keep a small general profile always available and
load specialist packs intentionally. Do not place a vendor package or another
runtime's embedded `.claude` or `.codex` tree below a recursively discovered
root unless that duplication is deliberate and budgeted.

You do not need to choose between a complete library and a usable startup
context. Install one router into the native skill root:

```bash
./skillsync.py install-catalog-router desktop --profile full-library --reviewed
```

The router searches the full catalog, reads one exact audited `SKILL.md`, and
keeps every unrelated skill outside the current conversation. It does not
enable a disabled plugin or grant its tools. A skill can therefore remain
available as guidance while plugin activation stays an explicit host decision.

When a local target already has sound instruction bodies but bloated or missing
discovery descriptions, normalize that metadata separately:

```bash
./skillsync.py compact-descriptions codex --max-chars 160 --reviewed
```

This is intentionally a reviewed mutation. It changes only the simple YAML
`description` scalar, adds a safe fallback when it is missing, and leaves each
skill's instruction body byte-for-byte intact.

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

## Grok Bot: bidirectional skill-folder sync

Grok Bot's user-created skills live on its Linux box at
`/home/box/agent-data/workflows/<slug>/SKILL.md`. This is a separate machine from
your Mac. The `grokbot` target defaults to that directory; configure
`targets.grokbot` or pass `--target-dir` to override it. Cursor-managed skills
and plugin skills are read-only and must stay outside the configured root.
Symlinked skills and read-only folders are skipped rather than followed or written.

Use the new `sync` command for Agent Skills folders. It copies `SKILL.md`, all
helper files and `LICENSE` as raw bytes, including every frontmatter key. It does
not add stamps, strip YAML or translate prose. Legacy flat `skills/<name>.md`
Core ports continue using the existing `sync-exact` workflow. Bundle skills use
`source_dir/<slug>/SKILL.md` in the same configured source directory.

### Fresh Linux box setup

Install Python 3.9+ and Git (for example `sudo apt-get install python3 git` on
Debian/Ubuntu). Node and npm are not required. Download the released CLI:

```bash
mkdir -p "$HOME/.local/bin"
curl -fL https://raw.githubusercontent.com/nordsym/skillsync/v0.8.0/skillsync.py \
  -o "$HOME/.local/bin/skillsync.py"
```

Create a **private skill-data repository** on GitHub with an initial commit and
clone the same repository on the box and Mac. Do not push personal skills to the
public `nordsym/skillsync` software repository. A dedicated sync branch is also
supported through normal Git branch tracking.

For a private repository, create a fine-grained GitHub token restricted to that
repository with **Contents: read and write**. Set `SKILLSYNC_GIT_TOKEN` through
your secret manager or a hidden prompt; do not put it in the remote URL, config,
command history or committed files. Git authentication stays with Git:

```bash
mkdir -p "$HOME/.local/libexec"
cat > "$HOME/.local/libexec/skillsync-git-askpass" <<'SH'
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' 'x-access-token' ;;
  *Password*) printf '%s\n' "$SKILLSYNC_GIT_TOKEN" ;;
  *) exit 1 ;;
esac
SH
chmod 700 "$HOME/.local/libexec/skillsync-git-askpass"
export GIT_ASKPASS="$HOME/.local/libexec/skillsync-git-askpass"
export GIT_TERMINAL_PROMPT=0
# SKILLSYNC_GIT_TOKEN must already be present in the environment.
git clone https://github.com/YOUR-ACCOUNT/YOUR-PRIVATE-SKILLS.git "$HOME/skills-sync"
cd "$HOME/skills-sync"
git config user.name 'Grok Bot skill sync'
git config user.email 'YOUR-GIT-COMMIT-EMAIL'
mkdir -p skills
cat > skillsync.json <<'JSON'
{
  "source_dir": "./skills",
  "targets": {
    "grokbot": "/home/box/agent-data/workflows"
  }
}
JSON
```

Keep each machine's config local: add `skillsync.json` to this data repository's
`.gitignore` and commit that ignore rule before using `--git`. The command
resolves relative paths against the config file's directory.

### Exact box command

```bash
python3 "$HOME/.local/bin/skillsync.py" sync --target grokbot --git \
  --config "$HOME/skills-sync/skillsync.json"
```

Run the same command with `--dry-run` first to preview local changes. Dry-run
never pulls, commits, pushes or writes; fetch/pull manually first if you need a
preview against the latest remote revision. `--direction import` selects box to
repo, `--direction export` selects repo to box; the default reconciles both.
The summary reports added, changed, skipped and conflicting skills. Conflicts
produce a nonzero exit status and leave the conflicting copies untouched.

`--git` uses the source repository's configured upstream: it requires a clean
working tree, fast-forward pulls, syncs, commits only imported skill-folder
changes and pushes. It never force-pushes, rebases or stashes unrelated work.
A push failure leaves a recoverable local commit. Resolve the Git problem and
run `git push` in the data checkout before rerunning sync; `--git` refuses
unpublished local commits. A conflict must be resolved deliberately by making the two skill
folders identical, then rerunning sync. There is no force-overwrite flag.

Each clone keeps its own last successful content hashes outside tracked skill
content. On the first run, differing copies conflict rather than guessing which
is newer. On subsequent runs, one-sided edits propagate; edits on both sides
conflict. Git timestamps are never used as proof of freshness. Removing a whole
skill on one side does not delete the other copy automatically.

On the Mac, use the same private data remote and configure a narrow user-owned
local skill root as another existing `targets` entry, for example:

```json
{
  "source_dir": "./skills",
  "targets": { "agents": "~/.agents/skills/my-synced-skills" }
}
```

Then run `python3 "$HOME/.local/bin/skillsync.py" sync --target agents --git
--config "$HOME/skills-sync/skillsync.json"` as one shell line. Only that explicit
root participates; installed plugin or Cursor caches are not sync destinations.
