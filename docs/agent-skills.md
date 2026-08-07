# Agent skills

> Part of the [Second Brain](../README.md) docs — see [docs/README.md](README.md) for the full index. This page covers the Claude Code skills bundled in `skills/`: which one covers what, how to install them, and how to add one.

A **skill** is a Markdown file that teaches an agent *when* to reach for a
feature and *how* to drive it — the routing layer between a user's plain-English
ask and the `brain` CLI. The CLI works without any of them; skills are what make
an agent reach for `brain timeline` on "how did this change over time" instead of
hand-rolling three searches.

Skills cover **Claude Code** (the CLI). For **Claude Desktop**, register the
`brain-mcp` MCP server instead — see
[Claude Desktop setup](guides/claude-desktop-setup.md).

## Which skill covers what

Eleven skills ship in `skills/`. Each is one directory containing a single
`SKILL.md`.

| Skill | Commands it covers | Use a sibling instead when… |
|---|---|---|
| [`consult-brain`](../skills/consult-brain/SKILL.md) | `search`, `show`, `explain`, `rate`, `ask`, `audio` | the ask is about themes or connections → `brain-graph` |
| [`brain-ask`](../skills/brain-ask/SKILL.md) | `ask`, `audio` | you want ranked documents to read yourself rather than one synthesized answer → `consult-brain` |
| [`brain-graph`](../skills/brain-graph/SKILL.md) | `graphrag search/themes/entity/entities/stats/communities`, `owner` | it's a single-doc lookup or a quote → `consult-brain` |
| [`brain-proactivity`](../skills/brain-proactivity/SKILL.md) | `brief`, `resurface`, `review`, `timeline`, `connect`, `gaps`, `capture` | the user asked a specific question → `consult-brain` |
| [`brain-capture`](../skills/brain-capture/SKILL.md) | `capture`, `capture review` | the thought belongs in a specific note you already know → `brain-authoring` |
| [`brain-memory`](../skills/brain-memory/SKILL.md) | `capture --json`, the Stop-hook write-back protocol | you are retrieving rather than recording → `consult-brain` |
| [`brain-authoring`](../skills/brain-authoring/SKILL.md) | `note new/rename`, `daily`, `edit`, `tag`, `mark-draft`/`mark-published`, `backlinks`, `links`, `orphans`, `graph` | content already exists elsewhere → `ingest-brain` |
| [`brain-maintenance`](../skills/brain-maintenance/SKILL.md) | `doctor`, `status`, `analyze`, `reembed`, `init`, `backfill`, `vault sync-summaries`/`relink-derived`, `setup`, `demo`, `eval` | — |
| [`brain-todo`](../skills/brain-todo/SKILL.md) | `todo`, `enrich --krisp-action-items` | — |
| [`ingest-brain`](../skills/ingest-brain/SKILL.md) | `ingest`, `ingest-dir`, `ingest-stdin`, `ingest-gmail` | the thought has no source document yet → `brain-proactivity` (`capture`) |
| [`elicit-brain`](../skills/elicit-brain/SKILL.md) | `elicit`, `elicit list` | the gap is a *failed search* rather than unwritten knowledge → `brain-proactivity` (`gaps`) |

Routing between skills is carried in each file's frontmatter `description`,
which ends in a `MANDATORY TRIGGERS:` list of natural-language phrases and names
the sibling to prefer for adjacent asks. Trigger phrases are kept unique across
skills so two of them never compete for the same request.

## Two installers, two different things

This trips people up, so it is worth stating plainly. The repo has **two** ways
to put a skill on disk and they share no code.

### `bin/brain-skills-sync` — all repo skills (what you want)

Enumerates every immediate subdirectory of the repo's `skills/` and copies each
into `$BRAIN_SKILLS_DEST` (default `~/.claude/skills/`). There is deliberately
no hardcoded list, so adding a directory is enough to ship a new skill.

```bash
bin/brain-skills-sync                # install/update all eleven
bin/brain-skills-sync --check        # report drift, copy nothing; exit 1 if stale
bin/brain-skills-sync --dest <dir>   # install somewhere else
```

Each skill reports `installed`, `updated`, or `unchanged`; `--check` reports
`in sync`, `STALE`, or `MISSING`. It touches only the brain-family skills and
never anything else in `~/.claude/skills/`. Updates are a wholesale replace, so
deleting a skill from the repo removes it from the destination on the next sync.

**It copies rather than symlinks.** That keeps behaviour predictable across
upgrades, but means an edit to a repo skill does not reach your agent until you
re-run the script. After pulling, `--check` will exit non-zero until you do —
that is the intended signal, not a bug.

### `brain claude install-skill` — one packaged cheat-sheet

A different thing entirely. It reads a **single** file from package data
(`brain/templates/skill/SKILL.md`) and writes it to
`~/.claude/skills/brain/SKILL.md`. That file is a short, condensed CLI
cheat-sheet shipped inside the wheel so that `pipx` / `uvx` users — who have no
repo checkout and therefore no `skills/` directory and no `bin/` — still get
something. It does not read `skills/` and is unaffected by anything added there.

```bash
brain claude install-skill              # write the packaged cheat-sheet
brain claude install-skill --force      # overwrite without the confirm prompt
brain claude install-skill --uninstall  # remove it
```

| | `bin/brain-skills-sync` | `brain claude install-skill` |
|---|---|---|
| Source | the repo's `skills/` directory | one file in package data |
| Installs | all eight skills, under their own names | one file at `~/.claude/skills/brain/` |
| Needs | a repo checkout | nothing beyond the installed CLI |
| Audience | contributors and source installs | `pipx` / `uvx` users |

If you have a checkout, use `bin/brain-skills-sync` — it is a strict superset.

## Adding a skill

1. Create `skills/<name>/SKILL.md`.
2. Match the frontmatter contract below. `name:` **must** equal the directory
   name, or the sync script installs it under a name the agent won't resolve.
3. Run `bin/brain-skills-sync`, then `bin/brain-skills-sync --check` to confirm.

**No registration anywhere.** Not in `pyproject.toml`, not in the sync script,
not in `cli_claude.py`. The sync script and its tests both discover skills by
enumerating `skills/`, so a new directory is picked up automatically.

### Frontmatter contract

```yaml
---
name: <exactly the directory name>
description: >
  What the skill does and when to use it, in a folded block. Name the sibling
  skill to prefer for adjacent asks, so two skills never fight over one request.
  MANDATORY TRIGGERS: comma-separated natural-language phrases, lowercase,
  ending in a period.
---
```

Checklist for a new skill — these mirror what the house format already enforces
across all eight:

- [ ] `name:` matches the directory name exactly.
- [ ] `description:` is a folded `>` block ending in a `MANDATORY TRIGGERS:` sentence.
- [ ] No trigger phrase is already claimed by another skill.
- [ ] The description names the sibling skill to prefer for adjacent asks.
- [ ] The body opens with a single `#` H1, then `##` sections.
- [ ] Recipes are fenced `bash` blocks with inline `#` comments.
- [ ] It closes with a `## Safety rules` or `## Operational notes` section for anything destructive.
- [ ] Length lands in the 100–300 line house band.
- [ ] Every flag shown has been checked against the command's real `--help`.
- [ ] Examples use synthetic names and `example.com` addresses only — never real personal data.

The last two are not stylistic. A skill that documents a flag which does not
exist actively misleads an agent, and skills are checked into a public repo.
