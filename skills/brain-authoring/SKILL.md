---
name: brain-authoring
description: >
  Create and edit vault-tier notes in the user's local second-brain via the
  `brain` CLI. Use this skill when the user wants to start a new note, open or
  create today's daily entry, rename an existing note (with reference
  refactoring), edit a note's frontmatter or body, add/remove tags, ask the
  LLM to propose tags, or quarantine / un-quarantine a note as draft. Covers
  brain note new / brain daily / brain note rename / brain edit / brain tag /
  brain mark-draft / brain mark-published.
  MANDATORY TRIGGERS: new note, create a note, start a note, scaffold a note,
  daily note, today's daily, daily entry, rename this note, rename my note,
  edit this doc, edit this note, tag this, untag this, auto-tag, propose
  tags, mark as draft, mark as published, quarantine this note, unhide this
  note.
---

# Brain Authoring

Use the `brain` CLI to author and maintain vault-tier notes. Vault notes are
the user's first-class writing (research notes, journal, drafts) — distinct
from `ingested` mirrors of Krisp/Slack/Gmail (those are managed by
`ingest-brain`).

For searching, see `consult-brain`. For ingestion, see `ingest-brain`. For
health and bulk repairs, see `brain-maintenance`.

## Starting new notes

### `brain note new <title>` — a fresh note from a template

```bash
brain note new "Hiring framework for staff engineers"
brain note new "Q3 OKRs draft" --folder okrs --tag okrs --tag draft
brain note new "Quick capture" --no-edit            # skip $EDITOR; just scaffold
```

Flags:

| Flag | Purpose |
|---|---|
| `--folder/-f <name>` | Subfolder under `<vault>/`. Defaults to root. |
| `--template/-T <name>` | Pick `_templates/<name>.md`. Default `note`. |
| `--tag/-t <name>` *(repeatable)* | Overrides the template's `tags:`. |
| `--no-edit` | Don't open `$EDITOR`. Useful for scripts and MCP-driven creation. |
| `--vault <path>` | Override `BRAIN_VAULT_PATH`. |

The file lands at `<vault>/<folder>/<slug(title)>.md`. Errors loudly if it
already exists. Frontmatter is force-applied: `id` UUID, `title`, `created`,
`updated`, `kind: vault`, `tags`. Template placeholders `{{title}}` /
`{{date}}` / `{{datetime}}` / `{{slug}}` are substituted before write.

After write, `sync_one_file` indexes the note into the DB and (unless
`--no-edit`) `$EDITOR` opens. The note re-syncs on editor exit.

### `brain daily` — today's daily note

```bash
brain daily                       # today, opens in $EDITOR
brain daily --date 2026-05-13     # specific date
brain daily --no-edit             # just resolve / create, no editor
```

Idempotent — opens the existing file if today's daily already exists. New
files render `_templates/daily.md`. The file lands at
`<vault>/daily/<YYYY>/<YYYY-MM-DD>.md`. Every invocation also refreshes
`<vault>/daily/index.md` (reverse-chrono autogen) so the wiki's daily index
stays current.

## Renaming notes

### `brain note rename <id-prefix> <new-title>`

Renames a vault note, including:

- Frontmatter `title` + `updated` rewrite
- File move (when slug changes)
- Every `[[old-title]]` reference across the vault rewritten to `[[<new-slug>|<new-title>]]`
- Atomic — every file we touch is snapshotted to `tempfile.mkdtemp(prefix="brain-rename-")` first. On any error the snapshots are restored byte-for-byte and the snapshot dir path is logged.

```bash
brain note rename ab12cd "Hiring framework v2" --dry-run     # preview the plan
brain note rename ab12cd "Hiring framework v2"
```

Flags:

| Flag | Purpose |
|---|---|
| `--dry-run` | Print the plan (files to rewrite, target path) without writing. |
| `--no-link-refactor` | Skip rewriting `[[old]]` refs in other files. Source still moves. |
| `--vault <path>` | Override `BRAIN_VAULT_PATH`. |

**Vault-tier only** — ingested-tier rows can't be renamed (they're mirrors of
external sources). Source-prefixed links (`[[brain:...]]`, `[[krisp:...]]`)
are bound to ids/externals and are never affected by a title rename. After a
rename, run `brain vault sync` once to re-resolve any previously-unresolved
references that now match the new title.

## Editing notes

### `brain edit <id-prefix>` — tier-aware

**Vault-tier docs:** the underlying `.md` is opened directly in
`$VISUAL`/`$EDITOR`/`vi`. The file owns frontmatter — mutating flags
(`--title`, `--content-*`, `--metadata`) are rejected. `brain edit` re-syncs
the file via `sync_one_file` on editor exit.

**Ingested-tier docs:** either a JSON-header + body editor opens (no flags),
or a targeted update happens (with flags):

```bash
brain edit 7a3f9c --title "Better title"
brain edit 7a3f9c --content-file ./new-body.md
brain edit 7a3f9c --metadata '{"source": "manual", "redacted": true}'   # shallow-merge
brain edit 7a3f9c --metadata '{...}' --replace-metadata                 # full replace
```

Body changes re-chunk + re-embed atomically (Voyage / Ollama failure rolls
back the chunk wipe). Auto-mirror writeback regenerates the on-disk file with
`force=True` so frontmatter-only edits propagate to the vault.

For multi-line content, prefer `--content-file` or `--content-stdin` over
inline shells.

## Tagging

### Manual add/remove — `brain tag <id-prefix> +tag -tag …`

```bash
brain tag 7a3f9c +interview-prep +career-2026 -outdated
```

Idempotent. Tags are auto-normalized at write time:

- `casefold()`-lowercase
- `[\s_]+` → `-`
- Hyphen runs collapsed
- Deduped (first-seen order)

So `+Interview_Prep` and `+interview-prep` are the same tag. When the doc has
a `vault_path`, the change is mirrored to the file's frontmatter so the next
`brain vault sync` does not overwrite the DB with stale on-disk tags.

`--regenerate-file` recreates a missing `_ingested/` mirror from the DB
before tagging. Rejected for `kind='vault'` notes (restore from backup / git
instead).

The "updated tags on …" line ends with one of `(file)` / `(file regenerated)`
/ `(db only)` / `(db only, file missing)` so you know whether the on-disk
file was touched.

### LLM-proposed tags — `brain tag <id-prefix> --auto`

Requires `documents.summary IS NOT NULL` (run `brain enrich --backfill` first
if missing). The enricher fetches the existing tag vocabulary, proposes
up to 1 new tag plus existing-vocab matches, and displays a partitioned
`[existing]` / `[new]` proposal. Prompts `a`ll / `s`ome / `r`eject.

```bash
brain tag 7a3f9c --auto                    # interactive
brain tag 7a3f9c --auto --accept-all       # auto-accept everything
```

Mutually exclusive with positional `+tag/-tag`. Banned formats (`pdf`,
`transcript`, `email`, etc.) are filtered at the Python layer — the LLM
can't add format-tier tags. If Ollama is unavailable, exits 1.

## Draft toggle

Quarantine and un-quarantine notes from the wiki without deleting them.

```bash
brain mark-draft <id-prefix>          # documents.draft = TRUE; frontmatter `draft: true` added
brain mark-published <id-prefix>      # documents.draft = FALSE; frontmatter `draft` removed
```

Both are idempotent (`<short-id> is already draft` / `is already published`).
The doc stays in the DB and remains visible to `brain search` / `brain show`
/ `brain list` — only the wiki's Quartz contentIndex filters drafts out of
Explorer / Graph / Search.

Use `mark-draft` when:

- A note is half-finished and shouldn't be public on the wiki yet
- A Gmail draft thread shouldn't surface in the rendered site
- A note is being held for review

## Safety rules

- **Don't auto-create notes silently.** When the user says "save this idea",
  scaffold the note but `--no-edit` and confirm the path back.
- **Don't rename ingested-tier docs.** The CLI will reject it; don't try
  `brain edit --title` as a workaround.
- **Don't add format-tier tags.** `pdf`, `transcript`, `email`, `markdown`
  are content_type, not user-facing tags. The `--auto` mode already filters
  these.
- **Always `--dry-run` rename first** when the user might have linked to the
  old title from many places.
- **Vault-tier file edits go through `$EDITOR`, not `--content-*` flags.** If
  the user pushes for inline body changes, edit the file via the Edit tool
  and let `brain vault sync --watch` pick it up.
