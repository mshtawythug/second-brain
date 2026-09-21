# Vault Model — Design Spec

**Date:** 2026-04-28
**Owner:** Pat Morgan
**Status:** Draft — pending approval

## Goal

Turn the brain into a headless Obsidian-style vault: a folder of plain Markdown files on disk is the source of truth for authored content, and Postgres becomes a derived index rebuilt from the vault. No editor, no UI — any text editor is the front end. A separate static-site generator (Quartz) handles the wiki-style display when the user wants one.

The shift moves the brain from a **read-only retrieval index** to a **read-write thinking surface**: daily notes, synthesis notes that link to ingested artifacts, and a personal knowledge graph that grows as the user authors into it. Hybrid search and the existing MCP server keep working — they just operate over a richer corpus.

## Why

Today's brain answers "what did I say to Jane Doe in March?" but has nowhere for "here's what I think about Jane Doe overall." A vault adds a thinking surface and connects isolated artifacts via wiki-links + backlinks. Concrete wins:

- **Authoring** — daily notes, synthesis notes, idea capture in `$EDITOR`
- **Connective tissue** — `[[wiki-links]]` between notes; `[[brain:<id>]]` from notes to ingested artifacts; backlinks emerge automatically
- **Synthesis layer for Claude** — hand-curated notes are higher signal than raw transcripts; Claude (via MCP) reads notes that already digest the underlying material
- **Durability** — vault folder is git-able, iCloud-able, readable in any editor on any device. If Postgres breaks, the data survives
- **Refactor-able knowledge** — rename, restructure, merge notes; brain re-indexes

## In scope (vault model v1)

- Vault folder as the source of truth for authored notes
- YAML frontmatter contract (id, title, created, updated, tags, aliases)
- Two-tier corpus: vault notes (file-backed, editable) + ingested artifacts (DB-authoritative, materialized as read-only files in `_ingested/` for portability)
- Wiki-link parser (`[[Title]]`, `[[Title|alias]]`, `![[embed]]`, `[[brain:<id>]]`, `[[<source>:<external_id>]]`)
- Backlinks, outgoing links, orphans, link graph export (json/dot/mermaid)
- One-shot DB→vault export (Phase 1 — also serves as a backup feature)
- Vault→DB sync, both one-shot and watch mode
- Authoring commands: `brain note new`, `brain daily`, `brain note rename` (with link refactor)
- Templates folder for daily notes / new-note scaffolds
- Quartz integration for wiki-style HTML rendering (Phase 6)
- New MCP tools so Claude can query backlinks, propose notes, and write into the vault with approval

## Non-goals

- No live preview, no graph viewer (Quartz handles graph view in HTML)
- No Obsidian plugin compatibility (we conform to Obsidian's *vault format*, not its plugin API)
- No bidirectional sync to a hosted service (vault is local-only; user's responsibility to git/iCloud it)
- No Canvas, Dataview, Tasks, or other Obsidian-plugin features
- No conflict-resolution UI — last-write-wins by file mtime; users rely on git for history
- No automatic note-creation from search ("Claude wrote a note for me without asking") — every vault write goes through explicit approval
- No editor of any kind shipped from brain — `$EDITOR` (vim, VSCode, nano, anything) is the front end

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  User writes in any text editor                               │
│         │                                                     │
│         │  edits .md files                                    │
│         ▼                                                     │
│  ┌────────────────────────┐                                   │
│  │  Vault folder           │  ← source of truth for           │
│  │  ~/brain-vault/         │     vault-tier notes             │
│  │  ├── daily/             │                                  │
│  │  ├── _templates/        │                                  │
│  │  ├── _ingested/         │  ← materialized from DB          │
│  │  │   ├── krisp/         │     (read-only by convention)    │
│  │  │   ├── slack/         │                                  │
│  │  │   ├── gmail/         │                                  │
│  │  │   └── manual/        │                                  │
│  │  └── (your folders)/    │                                  │
│  └─────────┬──────────────┘                                   │
│            │                                                  │
│      brain vault sync                                         │
│            │                                                  │
│            ▼                                                  │
│  ┌────────────────────────────────────────────┐              │
│  │  Postgres + pgvector                        │              │
│  │  - sources                                  │              │
│  │  - documents (kind ∈ {'vault','ingested'},  │              │
│  │               vault_path)                   │              │
│  │  - chunks (embeddings)                      │              │
│  │  - links (src_doc → dst_doc, kind, text)    │              │
│  │  - unresolved_links (dangling [[refs]])     │              │
│  └────────────────────────────────────────────┘              │
│            │                                                  │
│            ├── brain search       (CLI / MCP)                 │
│            ├── brain backlinks    (new)                       │
│            ├── brain graph export (new)                       │
│            └── brain vault render → Quartz → HTML site        │
└──────────────────────────────────────────────────────────────┘
```

**Inversion summary:** today, ingest writes documents to the DB and the file on disk is just a source. In the vault model, `documents` rows still live in Postgres, but for `kind='vault'` rows the file in the vault folder is authoritative — the DB row is a derived index that `brain vault sync` rebuilds. For `kind='ingested'` rows the DB stays authoritative; the file in `_ingested/` is a read-only mirror.

## Two-tier corpus

| Tier | `kind` | Source of truth | Re-write on re-ingest? | Editable by user? |
|------|--------|------------------|------------------------|-------------------|
| Vault | `'vault'` | The `.md` file in the vault | No — DB rebuilt from file | Yes (any editor) |
| Ingested | `'ingested'` | DB row (from Krisp/Slack/Gmail/file) | Yes — overwrites the `_ingested/` mirror file | No by convention; edits lost on re-ingest |

A user can **promote** an ingested doc to a vault doc by moving its file out of `_ingested/`. `brain vault sync` detects the move and updates `kind='vault'`. Demotion is the reverse: move a vault file into `_ingested/`. (Both are intentional, non-magical.)

## Vault layout

```
~/brain-vault/                  # default, override with BRAIN_VAULT_PATH
├── _templates/
│   ├── daily.md                # template for `brain daily`
│   └── note.md                 # template for `brain note new`
├── _attachments/               # binaries (PDFs, images) referenced by notes
├── _ingested/                  # materialized from DB, read-only by convention
│   ├── krisp/<YYYY-MM-DD>-<slug>.md
│   ├── slack/<channel>/<YYYY-MM-DD>-<slug>.md
│   ├── gmail/<YYYY-MM-DD>-<sender-slug>.md
│   └── manual/<original-filename>.md
├── daily/
│   └── <YYYY>/<YYYY-MM-DD>.md
└── <your folders>/             # synthesis notes, projects, references
    └── acme/
        └── jane-doe.md
```

**Conventions** (enforced by sync, documented in vault README):

- Files starting with `_` are tooling-managed (`_templates/`, `_attachments/`, `_ingested/`).
- Note filenames are `<slug-of-title>.md` for human-authored, `<date>-<external-id-or-slug>.md` for ingested.
- Slug = lowercase title, ASCII alnum, dashes for spaces, max 64 chars.

## Frontmatter contract

Every `.md` file in the vault has a YAML frontmatter block. Sync reads + writes it.

```yaml
---
id: 7c2a8b9f-3d4e-4f5a-9b8c-1d2e3f4a5b6c   # canonical, brain-assigned UUID
title: Jane Doe conversation                # mirrors documents.title
created: 2026-04-28T10:00:00Z               # set on first sync
updated: 2026-04-28T11:23:00Z               # set on every content change
tags: [career, acme, jane-doe]              # mirrors documents.tags
aliases: [Jane Doe, jane-doe-talk]          # for [[alias]] resolution
kind: vault                                 # 'vault' | 'ingested' (sync sets this)
content_type: note                          # mirrors documents.content_type
# Ingested-tier extras (only present when kind='ingested')
source: krisp                               # source kind
external_id: <upstream-id>                  # source external id
---
<body>
```

**Required:** `id`, `title`. Sync auto-assigns `id` if missing (writes back to file). Auto-fills `created`, `updated`, `kind` if missing.
**Optional:** `tags`, `aliases`, `content_type`, `source`, `external_id`.
**Source of truth:** the file. Sync does NOT add fields the user didn't ask for; it only fills in the canonical required ones (`id`, `created`, `updated`, `kind`).

**Aliases** are stored in `documents.metadata.aliases` (existing JSONB column) — no schema change needed for aliases themselves.

## Schema additions

New migration `003_vault_model.sql`:

```sql
BEGIN;

-- Document tier marker. 'ingested' is the legacy default for everything
-- predating this migration.
ALTER TABLE documents ADD COLUMN kind TEXT NOT NULL DEFAULT 'ingested'
  CHECK (kind IN ('vault', 'ingested'));

-- Vault-tier files have a relative path within the vault folder. Unique
-- (when not null) so a file can't be claimed by two documents.
ALTER TABLE documents ADD COLUMN vault_path TEXT;
CREATE UNIQUE INDEX documents_vault_path_idx
  ON documents (vault_path) WHERE vault_path IS NOT NULL;
CREATE INDEX documents_kind_idx ON documents (kind);

-- Wiki-link graph. Resolved links — both endpoints exist as documents.
CREATE TABLE links (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  src_document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  dst_document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  link_text       TEXT NOT NULL,    -- the raw [[X]] text as it appeared
  link_kind       TEXT NOT NULL     -- 'wiki' | 'embed'
                  CHECK (link_kind IN ('wiki', 'embed')),
  display_text    TEXT,             -- pipe alias, NULL when none
  UNIQUE (src_document_id, dst_document_id, link_text, link_kind)
);
CREATE INDEX links_src_idx ON links (src_document_id);
CREATE INDEX links_dst_idx ON links (dst_document_id);

-- Dangling [[refs]] that don't yet point at any document. Resolved on
-- every sync pass once new documents appear.
CREATE TABLE unresolved_links (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  src_document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  link_text       TEXT NOT NULL,
  link_kind       TEXT NOT NULL CHECK (link_kind IN ('wiki', 'embed')),
  display_text    TEXT,
  UNIQUE (src_document_id, link_text, link_kind)
);
CREATE INDEX unresolved_links_src_idx ON unresolved_links (src_document_id);
CREATE INDEX unresolved_links_text_idx ON unresolved_links (link_text);

COMMIT;
```

**Why a separate `unresolved_links` table?** The `links` FK is hard — both endpoints must exist. Wiki-links are routinely written *before* the target note exists (Obsidian convention: type `[[Foo]]`, click to create `Foo.md`). We track these dangling refs separately and resolve them during every sync.

**No `aliases` table:** stored in `documents.metadata.aliases` (existing JSONB).

## CLI surface — additions

```
# Vault management
brain vault init [--path PATH]
    Create vault folder + _templates/ + _attachments/ + _ingested/.
    Writes default templates. Idempotent — safe to re-run.

brain vault export --to PATH [--force]
    One-shot dump of current DB to a vault folder. Phase 1 only;
    after Phase 2, regular `brain vault sync` keeps DB ↔ vault aligned.

brain vault sync [--vault PATH] [--watch] [--prune] [--dry-run]
    Reconcile vault → DB. Walks the vault folder, upserts documents
    by frontmatter id, deletes vault-tier rows whose files vanished
    (only with --prune; default is to warn). With --watch, stays
    resident and re-syncs on file events.

brain vault render [--to PATH] [--no-build]
    Invoke Quartz against the vault, output to PATH (default ./dist).
    Fails clearly if Quartz isn't installed; --no-build only writes
    the Quartz config.

# Authoring
brain note new <title> [--folder F] [--template T] [--tag TAG]... [--no-edit]
    Create a new vault note from a template. Prints the path; opens
    $EDITOR unless --no-edit.

brain daily [--date YYYY-MM-DD] [--no-edit]
    Open or create today's daily note (or --date's). Uses
    _templates/daily.md.

brain note rename <id-or-prefix> <new-title> [--no-link-refactor]
    Rename a vault note: rewrite its frontmatter title, rename the
    file (slug change if applicable), and rewrite every [[old-title]]
    in other vault notes to point at the new title (preserving pipe
    aliases as [[new|old]]).

# Link graph
brain backlinks <id-or-prefix> [--json]
    List documents that link TO this one.

brain links <id-or-prefix> [--json] [--unresolved]
    List documents this one links TO. --unresolved adds the dangling refs.

brain orphans [--vault-only] [--json]
    Vault notes with no incoming or outgoing links.

brain graph [--format json|dot|mermaid] [--include-ingested]
            [--root <id-or-prefix>] [--depth N]
    Emit the link graph. Defaults to vault-tier only; --include-ingested
    pulls in linked artifacts. --root + --depth give a focused subgraph.
```

**Existing commands that change behavior:**

- **`brain edit <id>`** — for `kind='vault'` docs, opens the underlying file in `$EDITOR` directly (no JSON-header round-trip; the file *is* the source). For `kind='ingested'` docs, the existing JSON-header + body flow continues unchanged. Sync after editor exit.
- **`brain ingest <file>`** — when a vault is configured, also materializes the extracted text as `_ingested/manual/<slug>.md` so the file is reachable from the vault. The original file at `documents.source_path` stays untouched.
- **`brain rm <id>`** — for `kind='vault'` docs, deletes both the DB row and the vault file (after confirmation). For `kind='ingested'` docs, deletes the DB row and the `_ingested/...` mirror.

**Unchanged:** `brain search`, `brain show`, `brain list`, `brain tag`, `brain status`, `brain doctor`, `brain init`, `brain reembed`, `brain ingest-stdin`, `brain ingest-gmail`, `brain ingest-dir`.

## Wiki-link parser

**Patterns recognized:**

| Syntax | Example | Resolution |
|--------|---------|------------|
| `[[Title]]` | `[[Jane Doe conversation]]` | exact title (case-insensitive), then `metadata.aliases`, then 6+ char id prefix. Vault-tier only. |
| `[[Title\|display]]` | `[[Jane Doe conversation\|Jane]]` | same resolution; `display` saved in `links.display_text` for renderers |
| `[[Title#heading]]` | `[[Jane Doe\#March meeting]]` | same resolution; heading anchor preserved in `links.link_text`, only meaningful in renderers |
| `![[Title]]` | `![[jane-doe]]` | embed marker — same resolution, `link_kind='embed'` |
| `[[brain:<id-prefix>]]` | `[[brain:7c2a8b]]` | direct lookup by document id prefix (vault or ingested) |
| `[[<source>:<external_id>]]` | `[[krisp:abc123]]` | lookup by `(sources.kind, sources.external_id)`. Allows linking to ingested artifacts by their upstream id. |

**Resolution order:** explicit prefix (`brain:` / `krisp:` / etc) → exact title → alias → id prefix. Ambiguity (two notes with the same title) is a sync error — sync logs it and leaves the link in `unresolved_links`. The user resolves by adding an alias or using `[[brain:<id>]]`.

**Markdown links** (`[text](path)`) are **not** wiki-links and are not added to the link graph. They're left as-is for renderers.

## Sync algorithm

```
brain vault sync (one-shot):
  1. Walk vault, building map: vault_path → (frontmatter, body, body_hash)
  2. For each (vault_path, frontmatter, body, body_hash):
     a. If frontmatter.id missing → assign UUID, write back to file
     b. Look up document by id
     c. If not present:
          INSERT documents (kind='vault'|frontmatter.kind, vault_path, ...)
          chunk + embed body, INSERT chunks
     d. If present:
          if content_hash unchanged AND tags/title/metadata unchanged: skip
          else:
            UPDATE documents (title, content, content_hash, tags, metadata)
            DELETE chunks WHERE document_id = id
            chunk + embed body, INSERT chunks
     e. Drop existing links FROM this doc; re-parse [[]]; INSERT into
        links (resolved) or unresolved_links (dangling)

  3. For each kind='vault' doc with vault_path NOT in walked map:
     - With --prune: DELETE document (cascades to chunks + links)
     - Without --prune: warn, leave row in place

  4. Resolve unresolved_links: for each row, look up link_text against
     current vault notes. If resolved, move to links table.

  5. Report: N created, M updated, K deleted, J unresolved.
```

**Watch mode** (`--watch`): same algorithm but triggered by `watchdog` filesystem events, debounced 500 ms. On startup, runs a full pass once to catch changes made while the watcher was off.

**Body hash:** SHA-256 of body text *with frontmatter stripped*. This prevents a sync-induced frontmatter change (e.g., `updated:` timestamp) from triggering a re-embed.

**Atomicity:** each file is upserted in a transaction. A crash mid-sync leaves the DB consistent (every committed file is fully synced); the next run picks up where it left off.

## Quartz integration

Phase 6. Quartz (https://quartz.jzhao.xyz/) is a static-site generator built specifically for Obsidian-style vaults. Native support for `[[wiki-links]]`, backlinks, graph view, full-text search, dark mode.

**Integration:**
- `brain vault render` shells out to `npx quartz build --directory <vault> --output <dist>`.
- We commit a Quartz config (`quartz.config.ts`) at the repo root with sensible defaults (single-page graph, search enabled, dark mode default). The user can override.
- `brain doctor` adds a soft check for `npx` (warning, not failure — Quartz is optional).
- The `_ingested/` folder is rendered like any other vault folder; users see ingested artifacts as part of the wiki.

**Why not build our own renderer?** ~500 lines of Python plus a JS frontend would never match Quartz's polish, and we'd own forever. Pinning Quartz at a known-good version in docs is cheaper.

**Escape hatch:** if Quartz becomes unsuitable, the vault is standard Markdown + `[[wiki-links]]` + frontmatter — any other renderer (MkDocs Material with plugins, Hugo with an Obsidian theme, a custom exporter) can replace it without touching the vault format.

## MCP additions

Phase 7. New tools on the existing FastMCP server:

| Tool | Purpose |
|------|---------|
| `brain_backlinks(id_prefix)` | List documents linking to this one |
| `brain_links(id_prefix, include_unresolved=False)` | List documents this one links to |
| `brain_orphans(vault_only=True)` | Vault notes with no links |
| `brain_note_new(title, body, folder?, tags?, template?)` | Create a vault note. Returns vault_path + document_id. Asks for explicit user approval first (Claude must surface the proposed title + body). |
| `brain_daily(date?)` | Resolve or create today's daily note; returns vault_path + document_id |
| `brain_link_proposal(src_id, dst_id_or_title)` | Propose adding a `[[link]]` from one note to another (writes nothing — returns the diff) |

**Authoring constraint:** `brain_note_new` is the only MCP tool that creates files. It refuses to overwrite an existing path. All edits to existing notes still go through `brain_edit` (which now opens an editor for vault docs — incompatible with MCP). Until we add an `brain_edit_vault_body` tool that takes raw text, vault-tier edits via MCP are scoped to `brain_tag` and `brain_link_proposal`.

## Phased rollout

Each phase is independently shippable; the brain stays usable end-to-end after every phase.

### Phase 1 — Schema + Export (1–2 days)
- Migration `003_vault_model.sql` (kind, vault_path, links, unresolved_links).
- `brain vault init` (creates vault dir + templates).
- `brain vault export --to <dir>` (one-shot DB dump to vault format).
- Tests: round-trip safety (export → re-export idempotent; round-trip preserves all DocumentRow fields).
- Memory: update `schema.md`, `cli.md`.

**Exit:** user can dump the existing DB to a vault folder and back up the entire corpus.

### Phase 2 — Sync engine (3–5 days)
- Frontmatter parser/writer (PyYAML).
- `brain vault sync` (one-shot, no watcher yet).
- Wiki-link parser (`[[]]`, `[[X|Y]]`, `![[X]]`, prefixed forms).
- `links` + `unresolved_links` population.
- Tests: empty vault, new file, modified file, deleted file (with/without --prune), id assignment, content_hash dedup, link parsing round-trip.

**Exit:** user can edit `.md` files in their editor, run `brain vault sync`, and see results in `brain search`.

### Phase 3 — Authoring CLI (2–3 days)
- `brain note new` + `brain daily` + `_templates/` lookup.
- `brain note rename` with link refactor.
- `brain edit` change: open vault file in `$EDITOR` for `kind='vault'`.
- Tests: template rendering, rename rewrites all `[[]]` references, daily note creation, $EDITOR exit codes.
- Memory: `cli.md`, `types.md`.

**Exit:** user can create new notes, write daily notes, rename safely.

### Phase 4 — Link graph queries (1–2 days)
- `brain backlinks`, `brain links`, `brain orphans`, `brain graph`.
- Tests: graph correctness (cycles, isolated nodes, depth filtering), output format stability.
- Memory: `cli.md`, `types.md`.

**Exit:** user can navigate the link graph from the CLI.

### Phase 5 — Watcher (2 days)
- `brain vault sync --watch` via `watchdog`.
- Debouncing, startup full-pass, graceful shutdown.
- Tests: watcher fires on file change, debouncing collapses bursts, recovers from transient DB errors.

**Exit:** vault edits flow into the index live.

### Phase 6 — Quartz render (1–2 days)
- `brain vault render` shells out to `npx quartz build`.
- `quartz.config.ts` committed at repo root.
- `brain doctor` soft-check for `npx`.
- Docs: README section on viewing the wiki locally + deploying.

**Exit:** user can run `brain vault render && open dist/index.html`.

### Phase 7 — MCP additions (2–3 days)
- New MCP tools (`brain_backlinks`, `brain_links`, `brain_orphans`, `brain_note_new`, `brain_daily`, `brain_link_proposal`).
- Approval flow for `brain_note_new` (Claude must surface proposed content; the MCP runtime requires user confirmation per its standard tool-call permission model).
- Tests: MCP integration with vault-tier documents.

**Exit:** Claude can read the link graph and propose new notes during conversation.

**Total estimate:** ~12–18 days of focused work, shippable phase by phase.

## Risks & open questions

- **Sync drift** — DB and disk diverge if the user edits files while a stale connection writes to the DB. Mitigation: every sync run is a full reconciliation; content hash is the truth. Watcher uses a single-writer lock per file.
- **Title collisions** — two vault notes both titled "Jane Doe". Mitigation: sync errors out, logs the conflict, leaves both `[[Jane Doe]]` refs in `unresolved_links` until the user disambiguates with an alias or `[[brain:<id>]]`.
- **Frontmatter parser edge cases** — multiline values, weird YAML. Mitigation: PyYAML in safe mode; reject unparseable files with a clear error message rather than corrupting them. Files that fail to parse are skipped with a warning, not auto-rewritten.
- **File renames vs. content edits** — moving `foo.md` to `bar.md` should preserve the document_id. Mitigation: sync resolves files by frontmatter id first, vault_path second. Renames are detected via id-without-matching-path + new-path-without-matching-id.
- **Re-ingest overwriting `_ingested/` edits** — if a user edits an `_ingested/` file, the next re-ingest of the same upstream artifact wipes those changes. Mitigation: documented contract; the `_ingested/` README warns the user; for thoughts about an artifact, the right pattern is to author a vault-tier note that links `[[brain:<id>]]`.
- **Quartz dependency drift** — Quartz is JS, evolves rapidly. Mitigation: pin a known-good version in docs; the vault format is generic enough that Quartz can be replaced.
- **Vault size** — 10K+ notes might slow naive walk-and-hash. Mitigation: keep a `(vault_path, file_mtime)` cache in a small SQLite or JSON file; only re-read files whose mtime changed since last sync.
- **Embedding cost on bulk re-sync** — if the user edits 1000 notes via find/replace, sync re-embeds them all. Mitigation: body-hash check skips re-embed when only frontmatter changes; for bulk operations, document the existing `brain reembed` flow.
- **Conflict with existing `brain edit` semantics** — switching to in-file editing for vault docs is a breaking change. Mitigation: gate on `kind='vault'`; ingested docs continue to use the JSON-header flow.

## Testing strategy

Per CLAUDE.md: real Postgres test DB, fake embedder, no monkey-patching of production modules.

**Unit tests:**
- `tests/test_frontmatter.py` — parse, write-back, round-trip, missing id assignment, malformed YAML rejection
- `tests/test_wiki_link_parser.py` — every pattern in the resolution table; edge cases (escaped brackets, links in code fences, links spanning lines)
- `tests/test_link_resolution.py` — title vs. alias vs. id prefix, ambiguous matches, unresolved → resolved transitions
- `tests/test_slug.py` — slug generation rules

**Integration tests** (real DB):
- `tests/test_vault_export.py` — DB → vault folder, exported files round-trip
- `tests/test_vault_sync.py` — empty → new files → modified → deleted; --prune semantics; watcher events (using `watchdog` test mode)
- `tests/test_note_rename.py` — rename rewrites all `[[refs]]` across vault; edge cases (alias-form, embed-form, prefix-form)
- `tests/test_backlinks.py` — three-note chain A→B→C; backlinks(B) = [A]; orphans = []
- `tests/test_graph_export.py` — JSON/DOT/Mermaid output stable for a fixed corpus

**End-to-end:**
- `tests/test_cli_vault.py` — full `vault init` → `note new` → edit file → `sync` → `search` finds it → `backlinks` shows incoming
- `tests/test_mcp_vault.py` — MCP `brain_note_new` creates file + DB row + chunks + embeddings

**Coverage targets** (per CLAUDE.md):
- `vault/` modules (parser, sync, links): 95%
- New CLI commands: 85%
- Quartz integration: smoke test only (we don't test Quartz itself)

## Memory & docs updates

After each phase:
- `cli.md` — new commands as they ship
- `schema.md` — after Phase 1 (kind, vault_path, links, unresolved_links)
- `types.md` — new dataclasses (e.g. `VaultNote`, `Frontmatter`, `WikiLink`, `LinkGraph`)
- New `vault.md` memory file — vault format, frontmatter contract, conventions

`CLAUDE.md` (project) gets a new "Vault model" section documenting: where the vault lives, how `brain vault sync` works, the two-tier corpus, and the convention that `_ingested/` is read-only.

`README.md` gets a "Wiki view" section covering Quartz setup and rendering.

## Open questions for review

1. **Vault location default.** Proposed: `~/brain-vault/`, configurable via `BRAIN_VAULT_PATH`. Alternative: `~/Documents/brain-vault/` for iCloud-friendliness on macOS. **Recommendation:** `~/brain-vault/` (cleanest, no implicit cloud sync). User can symlink if they want iCloud.

2. **Pruning policy.** `brain vault sync` defaults to *warn* on missing files (don't delete the DB row). `--prune` deletes. Alternative: prune by default, `--no-prune` to warn. **Recommendation:** default to warn — destructive ops should be opt-in.

3. **Auto-write frontmatter id.** Sync writes a UUID into frontmatter the first time it sees a new file. Alternative: refuse to sync files without an id, force user to run `brain note new` (which writes the id). **Recommendation:** auto-write — lower friction for users who manually drop a `.md` into the vault.

4. **`_ingested/` filename collisions.** Two Krisp calls on the same date with the same slug. Resolution scheme: append `-<short-external-id>` on collision. Alternative: always include external-id in the filename. **Recommendation:** always include short external-id (`<date>-<external-id>-<slug>.md`) for predictability.

5. **Quartz lock-in.** Phase 6 commits a Quartz config; switching renderers later means rewriting that config but not the vault. **Recommendation:** acceptable; vault format is the durable artifact.

6. **MCP write-into-vault scope.** `brain_note_new` creates files, but should `brain_edit` also support editing vault bodies via MCP (no `$EDITOR`)? **Recommendation:** defer to Phase 8 — start with create-only. The current `brain_edit` (DB-only) keeps working for tags/title/metadata; body edits to vault notes flow through the editor.

Pending answers to (1)–(6), this spec is ready for implementation.
