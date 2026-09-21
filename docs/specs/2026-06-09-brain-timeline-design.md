# Spec: `brain timeline` — Temporal Evolution (implementation)

**Source plan:** `docs/plans/2026-06-03-brain-next-features/05-brain-timeline-temporal-evolution.md`
**Cross-plan lock:** that folder's `README.md` → migration `021_timeline_doc_date.sql` (Plan 05, optional Phase 3).
**Date:** 2026-06-09 · **Author:** p05-timeline teammate

---

## Scope

Bucket entity mentions by document date to show how a theme/entity rose or fell
over time, with per-bucket co-topics, doc titles, and optional Ollama synthesis.
Three phases (matching the plan):

- **Phase 1** — `timeline.py` value objects + `build_timeline()` (inline
  `COALESCE(sent_at, ingested_at)`), config knobs, CLI `--json`.
- **Phase 2** — co-topics, Rich terminal bar chart, `--person` scope.
- **Phase 3** — `summarize_bucket()` synthesis, migration 021 + auto-fallback
  detection, MCP `brain_timeline`.

## Key implementation decisions

1. **Relational-only, no AGE.** The timeline query joins migration-012 relational
   tables (`graph_entity_mentions`, `graph_edge_contributions`, `graph_entities`)
   + `documents`. These are pure-SQL tables created in stock Postgres — **no
   Apache AGE / Cypher**. `build_timeline()` uses a plain `connect()` connection.
   The CLI/MCP gate is `cfg.graph_enabled` (not AGE availability), so timeline
   works on the relational mirror even on the stock pgvector image.
2. **Temporal anchor** = `COALESCE(documents.sent_at, documents.ingested_at)`
   computed inline. When migration 021's generated `doc_date` column is present
   (detected via `information_schema.columns`), the query swaps to `d.doc_date`.
3. **Multi-entity merge in SQL.** One query with `gem.entity_id = ANY(%s)`,
   `COUNT(DISTINCT d.id)`, `SUM(gem.mention_count)`, `ARRAY_AGG(DISTINCT d.id)`
   — shared docs across matched entities are counted once at the bucket level.
4. **`--since` inclusive, `--until` inclusive of the named month.** `--since
   2024-01` → `doc_date >= 2024-01-01`; `--until 2024-06` → `doc_date <
   2024-07-01` (the production refinement the plan's "simplified" SQL allows).
5. **`--limit` trims, never gaps.** Zero-doc buckets never appear (GROUP BY only
   emits buckets with docs). `--limit` trims to N buckets; `BRAIN_TIMELINE_TRIM`
   (`oldest`|`sparsest`) picks which end. The trimmed count is reported
   (`buckets_omitted`).
6. **Co-topics** aggregate `graph_edge_contributions` for a bucket's docs +
   seed entity ids, excluding the seed entities and the corpus owner (owner
   resolved to `person` entity ids via `cfg.owner_participants`, mirroring
   themes-mode suppression). Top-3 by co-occurrence.
7. **Synthesis is best-effort, never raises.** `summarize_bucket()` catches
   `EnrichmentError` (incl. `OllamaUnavailable`) → `None` + WARN. Only the top
   `BRAIN_TIMELINE_SYNTH_LIMIT` densest buckets are synthesized; `0` disables.

## Modules / files

- **New** `src/brain/timeline.py` — `EntityRow`, `TimelineBucket`,
  `TimelineContext`; `build_timeline()`; pure helpers `_validate_granularity`,
  `_parse_month`, `_bucket_label`, `_trim_buckets`; DB helpers `_resolve_entities`,
  `_scope_to_person`, `_query_buckets`, `_top_cotopics`, `_bucket_doc_titles`,
  `_doc_date_expr`.
- **New** `migrations/021_timeline_doc_date.sql` — additive `doc_date` generated
  STORED column + `idx_documents_doc_date`; idempotent (`IF NOT EXISTS`). Touches
  only `documents` (cols from 001/007); references no 018–020 objects.
- **New** `tests/test_timeline.py` — unit (pure helpers) + integration (real
  Postgres) + CLI tests. `test_migration_021_doc_date_generated` carries
  `@pytest.mark.phase3` and is excluded from the default suite.
- **Modified** `src/brain/config.py` — four `BRAIN_TIMELINE_*` knobs.
- **Modified** `src/brain/enrichment.py` — `OllamaEnricher.summarize_bucket()`.
- **Modified** `src/brain/format.py` — `timeline_context_json()`,
  `timeline_renderable()`.
- **Modified** `src/brain/cli.py` — `@app.command("timeline")`.
- **Modified** `src/brain/mcp_server.py` — `brain_timeline` tool.
- **Modified** `pyproject.toml` — register `phase3` marker + add to `addopts`
  `-m` exclusion (analogous to `eval`).

## Error handling

- Graph disabled (`cfg.graph_enabled` false) → CLI exit 1 / MCP `INVALID_PARAMS`
  with the actionable message.
- Zero matching entities → friendly "no entities found" message, exit 0, empty
  context.
- `PersonNotFound` / `PersonAmbiguous` → clean red CLI error exit 1 / MCP
  `INVALID_PARAMS` (mirrors `graphrag`).
- Person resolves but no co-docs → empty context (exit 0) + WARN.
- Bad `--granularity` / `--since` / `--until` → `ValueError` → CLI `BadParameter`
  (exit 2) / MCP `INVALID_PARAMS`.

All SQL is parameterized and tenant-gated (`tenant_id = %s` from
`resolve_tenant(cfg, tenant)`).
