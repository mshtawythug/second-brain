---
name: brain-maintenance
description: >
  Operate and maintain the user's local second-brain — health checks,
  re-embedding, corpus backfills, summary mirror sync, derived-link rebuild,
  People Hub regeneration, and embedder swaps. Use this skill when the user
  asks whether the brain is healthy, asks to rebuild embeddings, asks to
  resync summaries to the wiki, asks to regenerate the people pages, asks
  to refresh derived links or directory, or reports a brain CLI error that
  suggests an ops-level repair. Covers brain doctor / status / reembed /
  init / backfill / vault sync-summaries / vault relink-derived / vault
  directory refresh.
  MANDATORY TRIGGERS: brain doctor, is my brain healthy, brain status,
  rebuild embeddings, reembed, brain reembed, backfill the brain, backfill
  tags, backfill search, sync summaries, sync summaries to wiki, regenerate
  people pages, refresh derived links, refresh directory, relink, switch
  embedder, ollama down, postgres down, brain init.
---

# Brain Maintenance

Operational commands for the brain corpus. Most of these are idempotent and
safe to re-run.

For searching, see `consult-brain`. For ingestion, see `ingest-brain`. For
authoring, see `brain-authoring`. For action items, see `brain-todo`.

## Health and status

### `brain doctor`

The single entry point for "is the brain healthy?". Verifies:

- `.env` present, required vars set (`DATABASE_URL`, plus `VOYAGE_API_KEY` if
  `BRAIN_EMBEDDER=voyage`).
- Postgres + pgvector reachable.
- `chunks.embedding` column shape (type, NOT NULL, HNSW index when applicable).
- Embedder runtime: Ollama daemon + model loaded (`arctic` / `qwen3`), or
  Voyage API key (`voyage`).
- `gws` CLI present (used by Gmail ingest + directory refresh).
- `npx` present (soft check — Quartz / wiki render is optional).
- Enrich model loaded in Ollama (`BRAIN_ENRICH_MODEL`, default `llama3.1:8b`).

Exit 1 on hard failure. Missing Ollama models, missing `npx`, missing enrich
model are soft warnings (exit 0).

```bash
brain doctor
```

When it flags something, the printed lines name the remediation step:

- `embedding model not loaded` → `ollama pull <model>`
- `enrich model not in /api/tags` → `ollama pull llama3.1:8b`
- Postgres unreachable → check `docker compose ps` and `docker compose up -d`.

### `brain status`

Counts of documents / chunks / sources, last ingest timestamp, breakdown by
source kind. Use after a bulk ingest to confirm rows landed.

## Re-embedding — `brain reembed`

Backfills `chunks.embedding` for any row where it's NULL. After backfill
leaves zero NULL rows, finalizes the column: `SET NOT NULL`, plus an HNSW
cosine index when `embedder.dim ≤ 2000` (arctic, voyage). For Qwen3 (4096
dim) the index is skipped — pgvector caps HNSW at 2000 for `vector`.

```bash
brain reembed                                # backfill NULLs only
brain reembed --dry-run                      # report what would happen
brain reembed --limit 1000 --batch-size 100  # cap the run
brain reembed --all                          # re-embed EVERYTHING — required after embedder swap
brain reembed --no-finalize                  # backfill but skip NOT NULL + index step
```

Idempotent. Safe to re-run after a crash. The `--all` flag is the one to
reach for after switching `BRAIN_EMBEDDER` (existing embeddings live in the
wrong vector space and must be regenerated).

## Backfills (one-shot corpus repairs)

All under `brain backfill <name>`. All idempotent. Re-running after
convergence is a no-op.

| Command | When to run |
|---|---|
| `brain backfill normalize-tags` | After a tag-casing/separator drift is discovered. `--dry-run` first; `--mapping <file.json>` for synonym collapses (`{"recruiters": "recruiter"}`). Rewrites both DB rows and vault frontmatter. |
| `brain backfill source-rows` | Sets `source_id='manual'` for legacy markdown docs with `source_id IS NULL`. Run once after upgrading from a pre-source-row schema. |
| `brain backfill search` | Repopulates `chunks.title_text` / `tags_text` / `search_extras` after migration 009. Auto-runs from `brain init` when that migration just landed; rerun manually if a corpus-level retitle/retag bypassed the write path. |

## Summary mirror sync — `brain vault sync-summaries`

Q2-SUMMARY-WIKI: when `documents.summary` is populated (by ingest or `brain
enrich --backfill`) but the on-disk vault mirror's `summary:` frontmatter is
missing or stale, this command rewrites the frontmatter atomically.

```bash
brain vault sync-summaries                     # full corpus
brain vault sync-summaries --dry-run           # preview
brain vault sync-summaries --limit 50          # cap
```

Idempotent. Non-destructive (only `summary:` is touched). Output line:
`inspected N, [would update | updated] M, unchanged K, missing F, errored E`.
Exit 1 if errored > 0.

When to run:

- After `brain enrich --backfill` enriched docs that already had vault mirrors.
- After importing a vault folder that pre-existed the summary feature.
- After a migration / batch update changed many `documents.summary` rows.

## Derived links + People Hub — `brain vault relink-derived`

Full-corpus rebuild in five steps inside one autocommit connection:

1. Gmail directory rescan — walks every Gmail doc and upserts
   `(display_name, email)` pairs.
2. Krisp `_participant_keys` backfill — re-derives from Krisp bodies so
   pre-pre-insert-hook rows get caught.
3. Calendar (windowed) + Contacts (full) refresh via `gws` CLI + `<vault>/_people.yml` reload.
4. Linker pass — `rebuild_derived_for(corpus)` rebuilds `derived_links` rows
   (R1 `shared_thread`, R2 `shared_participant`, R3 `same_day_participant`).
5. Phase D fence rewrite (auto-generated "Related" sections in `_ingested/`
   mirrors) + People Hub emit (`<vault>/people/<slug>.md` per emittable
   person + `index.md` roster).

```bash
brain vault relink-derived
```

Output: "Touched docs / Inserted edges / Affected docs / Fence files rewritten
/ Pages written / Pages deleted" plus two Rich tables. Runs unconditionally —
the People Hub index page always emits even on an empty corpus.

When to run:

- After ingesting a batch of Gmail / Krisp.
- After editing `<vault>/_people.yml`.
- After updating the threshold (`BRAIN_PEOPLE_HUB_MIN_DOCS`, default 3).

### Lighter alternative — `brain vault directory refresh`

Directory rebuild only (steps 1 + 3 above). No linker pass, no fence rewrite,
no People Hub emit. Use when you only edited `_people.yml` and don't need a
full relink. Soft-fails on missing `gws` and exits 0.

## Wiki build (Quartz blue/green)

Run via the bin scripts:

```bash
bin/brain-up                                   # start watcher A (DB↔vault) + watcher B (vault→Quartz)
bin/brain-status                               # pid + current symlink + .build-id
bin/brain-rebuild                              # force rebuild without bouncing watchers
bin/brain-rebuild --clean-cache                # wipe parser cache and rebuild from cold
bin/brain-down                                 # stop watchers (Caddy keeps serving last-good)
```

If the wiki is misbehaving, `bin/brain-status` is the first stop. If a stale
build is showing, `bin/brain-rebuild` swaps in a fresh one atomically.

## Init / migrations — `brain init`

Apply `migrations/*.sql` in name order, then `ensure_embedding_column` to
align `chunks.embedding`'s declared dim against the active `BRAIN_EMBEDDER`.

```bash
brain init
```

- Fresh DB → drops + re-adds `chunks.embedding` at the right dim.
- Existing chunks at a different dim → errors with a destructive-reset hint.

Never run `brain init` against the live production DB if chunks already
exist. The schema is additive-only by policy.

## Switching embedders (destructive)

Embeddings cannot be re-projected across models. Switching `BRAIN_EMBEDDER`
requires a full reset:

```bash
docker compose down
rm -rf data/postgres                            # NOT `docker compose down -v` — Postgres is a host bind-mount
# edit .env to set BRAIN_EMBEDDER=arctic|voyage|qwen3
docker compose up -d
brain init
brain ingest-dir <…>                             # re-ingest the corpus
brain reembed
```

**Stop and confirm with the user before executing this.** It wipes every
document. There is a memory file (`feedback_db_safety.md`) documenting a
prior accidental wipe; do not repeat it.

## Safety rules

- **Never run destructive ops on the production DB without explicit user approval.** That includes `DROP`, `TRUNCATE`, unbounded `DELETE`, `docker compose down -v`, `rm -rf data/postgres/`.
- **Migrations are additive-only.** Schema changes go in a new numbered file under `migrations/`. Never edit a shipped migration.
- **`brain doctor` is the diagnostic; the remediation is in the printed line.** If the line doesn't tell you what to do, ask the user before guessing — don't go straight to a reset.
- **Soft warnings are warnings, not failures.** Missing `npx`, missing Ollama enrich model — note them, don't escalate.
- **Idempotency means run-once vs. run-many is the same.** All backfills and the relink are safe to rerun. If a run errors midway, fix the root cause and rerun.
