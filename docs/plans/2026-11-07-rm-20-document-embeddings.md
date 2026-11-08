# RM-20 — Document-level embeddings as a re-scoring stage (default OFF)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-20 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(wave 5; roadmap 3.6; backlog rank 12). The ticket is the spec; this is the task list.
**Base:** `second-brain/master` `34ef76cd` (RM-09, RM-14a/b, RM-19 merged; RM-14c NOT done).
**Branch / worktree:** `feat/rm-20-document-embeddings`, `.claude/worktrees/impl-rm20`.

## Goal

A "what is this document about" signal: each document's `title + summary` (or `title + head of
content`) is embedded once into a new table, and a pipeline stage re-scores the candidates a search
already returned by the cosine between that vector and the query vector. It can lift a document
whose best chunk ranks poorly but whose summary fits the query. It ships **off**
(`BRAIN_DOC_LEG=false`). While off, every surface's output and `brain eval`'s `config_signature`
are byte-identical to master, so `ci.json` is not re-recorded (spec §2.7).

## The RM-14c dependency (D1, for the coordinator to rule on)

The spec lists RM-14c as a dependency because the **measurement** is to be taken "on top of the
pipeline RM-14c left". RM-14c needs the owner's machine and prod, which is blocked. Nothing in the
**code** depends on RM-14c: the stage composes with whatever `build_stages` returns, reranker or not.

- **Recommended:** ship the code now, default OFF, with no `config_signature` key while off. The
  measurement becomes a recorded **owner step** (below, "Eval"), run after RM-14c's ruling. The
  backlog row and Appendix B say "built, off; measurement owed (owner step, after RM-14c)", not
  SHIPPED-and-adopted, exactly as RM-14a's row did.
- **Alternative:** hold RM-20 until RM-14c lands. Costs a wave-5 slot for no code benefit, and the
  measurement would then be taken on the same machine in the same way anyway.

## Files

| File | Change | Size / ceiling |
|---|---|---|
| `src/brain/migrations/031_document_embeddings.sql` (new) | The table (below). Pure SQL, `IF NOT EXISTS`, additive. | new |
| `src/brain/embedding_targets.py` | Allowlist gains `("document_embeddings", "embedding")`. | 54 → 55 |
| `src/brain/db.py` | New `ensure_relational_embedding_columns(conn, embedder)`; `RELATIONAL_EMBEDDING_TARGETS` tuple. | 649 → ~690 |
| `src/brain/cli.py` (**over 800**) | `init`: the chunks-only `ensure_embedding_column(conn, embedder)` becomes `ensure_relational_embedding_columns(conn, embedder)`; the import line changes name. The AGE-gated graph call stays. | **net zero**; row promoted + hop added |
| `src/brain/backup/restore.py` | Same one-call swap + import. | 690, net zero (not in the table) |
| `src/brain/demo/__init__.py` | Same swap at both call sites + import. | 452, net zero |
| `src/brain/search.py` (**over 800**) | Two lines: an inline growth comment and `cached_query_embedding, embedder_identity = _query_embed, _embedder_identity` (D10). Docstring: promote the descriptive "the RM-24 commit" hop to `8951620` / 832, add an RM-20 descriptive hop. | +2 code, +~3 docstring; recorded in both places |
| `src/brain/settings/doc_leg.py` (new leaf) | `DocLegSettings(enabled: bool, weight: float)` frozen; `load_doc_leg_settings(env)`. `BRAIN_DOC_LEG` (`env_bool`, default false), `BRAIN_DOC_LEG_WEIGHT` (`env_float`, 1.0, `[0.0, 10.0]`, D17). Validated even while off. | ~50 |
| `src/brain/search_doc_leg.py` (new) | `DOC_LEG_STAGE_NAME = "doc_leg"`, `DocLegStage`, pure `doc_leg_order(...)`, the one vector-lookup statement. | ~180 |
| `src/brain/search_pipeline.py` | `build_stages(env, *, embedder, rerank=None)` (D4) adds `DocLegStage` **before** the reranker (D9); `override_stages(rerank, *, embedder)`; the text-only gate (D5): `stage_needs_query_vector(stage)`, `search_was_text_only(...)`, `apply_stages(..., text_only=False)`, `TEXT_ONLY_SKIP_REASON`. | 342 → ~395 |
| `src/brain/eval/runner.py` | `build_stages(env, embedder=embedder)`; `_apply_stages` passes `text_only=not produces_embeddings`. | +~3 |
| `src/brain/cli_search.py` | Two `override_stages(rerank)` → `override_stages(rerank, embedder=embedder)`. | 739, +0..2 of the §2.2 budget |
| `src/brain/doc_embeddings.py` (new) | Basis + hash (pure), the selection and upsert statements, `backfill(conn, embedder, *, stale, limit, batch_size) -> BackfillReport`. | ~250 |
| `src/brain/cli_docembed.py` (new) | `brain docembed backfill [--stale] [--limit N]`; thin Typer over `doc_embeddings`. | ~110 |
| `src/brain/cli_registry.py` | One `EXTENSION_REGISTRARS` entry + import. | +2 |
| `docs/ceiling-table.md` | `cli.py` row: promote its trailing descriptive hop (if any) to SHA + count, add an RM-20 net-zero hop. `search.py` row: its last hop is already bound (`8951620`); add an RM-20 +2 descriptive hop. Trail rules read first. | — |
| `tests/conftest.py` (over 800, tests) | One `pytest_plugins` entry + its one-line comment, with the docstring growth note its own convention uses (as RM-14a/16/32/38/41 did). | +2 + note |
| Tests (new) | Listed under "Tests". | each < 800 |
| Tests (changed) | Every `build_stages(...)` / `override_stages(...)` call in `tests/` (13 today; re-derive with `grep -rn "build_stages(\|override_stages(" tests`) passes `embedder=`. Allowlist test for `embedding_targets`. | — |
| `docs/configuration.md`, `docs/cli-reference.md`, `CHANGELOG.md` (Unreleased), `skills/brain-maintenance/SKILL.md` (if under its ceiling) | Knobs, command, entry. | — |
| `docs/plans/2026-10-23-backlog-reconciliation.md` row, spec Appendix B row | "built, off; measurement owed (owner step)" + PR number (follow-up commit once known). | — |

No new `documents` read is added to `search.py`. `config.py`, `mcp_server.py`,
`ingest/__init__.py` and `queries.py` are not touched.

## Migration `031_document_embeddings.sql`

```sql
BEGIN;
CREATE TABLE IF NOT EXISTS document_embeddings (
  document_id UUID PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  embedding   vector(1024),          -- house starting dim; init reconciles it (below)
  basis       TEXT NOT NULL CHECK (basis IN ('title_summary', 'title_head')),
  basis_hash  TEXT NOT NULL,         -- SHA-256 hex of the embedded text
  embedder    TEXT NOT NULL,         -- search.embedder_identity() of the backend (D10)
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMIT;
```

Additive (one new table, no change to an existing one), re-runnable (`IF NOT EXISTS`), no Python
reference, a header comment in 030's style. The column is nullable, as `chunks.embedding` is after
`ensure_embedding_column`'s drop/re-add; the stage reads `embedding IS NOT NULL` only. No index
beyond the PK (spec RM-20.D2, D3 here). Backups need no change: `brain backup` dumps the whole
database, and restore reconciles the column (below).

## Dimension reconciliation and each backend

`db.ensure_relational_embedding_columns(conn, embedder)` reconciles
`RELATIONAL_EMBEDDING_TARGETS = (("chunks", "embedding"), ("document_embeddings", "embedding"))`
with `ensure_embedding_column`'s semantics, in **two passes** (D14): first a read-only preflight
that raises the existing destructive-reset `BrainError` if **any** target holds vectors at another
dim, then the resizes. Without the preflight, an empty `chunks.embedding` would be resized and a
populated `document_embeddings` would then refuse, leaving the database half reconciled.
`init`, `restore` and the demo (two sites) call it; the AGE-gated graph call in `init` stays separate.

| Backend | `dim` | Column after `init` | Backfill | Stage |
|---|---|---|---|---|
| `arctic` (Ollama, local) | 1024 | `vector(1024)`, no-op | embeds every document, confidential included (local: no egress) | runs |
| `voyage` (hosted) | 1024 | `vector(1024)`, no-op | **skips confidential documents** (F6 veto, `is_hosted_embedder`); they stay vectorless | runs; vectorless rows hold their index (D6) |
| `qwen3` (Ollama, local) | 4096 | resized to `vector(4096)` while empty; refuses with the reset hint once populated at 1024 | as arctic | runs. No HNSW is possible above 2000 dims (pgvector 0.8.x) and none is wanted (D3): the stage reads ≤ 50 vectors by primary key, so 4096 costs nothing extra |
| `none` (`NullEmbedder`, FTS-only) | 1024 | `vector(1024)`, no-op | **refuses, exit 1** ("BRAIN_EMBEDDER=none produces no vectors") | built if `BRAIN_DOC_LEG=true`, but always reported skipped `text_only_search` (D5) |

A switch between same-dim backends (arctic ↔ voyage) is not a reset: old rows keep the other
backend's `embedder` value, the stage ignores them (D11), and `--stale` re-embeds them (AC4).

## Backfill (`doc_embeddings.py`, `brain docembed backfill`)

- **Basis (pure).** `summary` non-blank → `("title_summary", title + "\n" + summary)`; else
  `("title_head", title + "\n" + first 512 cl100k tokens of the content head)`. The content head is
  read in SQL as `left(content, DOC_HEAD_CHAR_CAP)` (8,192 chars) so a 300-chunk document is not
  pulled whole; the basis is defined on that prefix, so it stays deterministic (D13). The whole
  basis is then cut to `INGEST_CHUNK_TARGET_TOKENS + INGEST_CHUNK_OVERLAP_TOKENS` (700) with
  `token_budget.truncate_to_token_budget(cost=embedder.count_tokens)`: no basis is longer than
  the longest chunk the embed window is already validated for (`MIN_OLLAMA_EMBED_NUM_CTX`), so a
  long title cannot make one document overflow on every run (D13). `basis_hash` = SHA-256 hex.
- **Default mode:** documents with **no** `document_embeddings` row. **`--stale`:** also rows whose
  `embedder` ≠ the active `embedder_identity`, or whose `basis_hash` ≠ the recomputed one. The hash
  is token-based, so `--stale` walks every document by keyset pages over `documents.id` and
  compares in Python; each page carries only `title`, `summary`, the capped head, `sensitivity`,
  and the stored `basis_hash` / `embedder`.
- **Hosted embedder:** every selection statement adds `AND d.sensitivity <> 'confidential'`
  (through `sensitivity.not_confidential_sql`), mirroring `iter_chunks_missing_embedding`'s
  `exclude_confidential`. The report counts the skipped documents and says why, as `brain reembed`
  does. Drafts are included on purpose: their chunks are embedded too, and the stage only ever sees
  rows the search predicate already admitted. Both reasons are written inline at the statement.
- **Writes:** one `embed(texts, input_type="document")` call per batch (32), then per row
  `INSERT INTO document_embeddings (...) SELECT %s, ... FROM documents WHERE id = %s ON CONFLICT
  (document_id) DO UPDATE SET ...`: a document deleted mid-run inserts nothing instead of raising
  a foreign-key error. Each batch commits in its own `conn.transaction()`; `--limit N` caps the
  number of documents embedded in the run.
- **Failure (D16):** any `EmbedError` stops the run. The batches already written stay (a re-run
  resumes), stderr gets one class-only line (`describe_failure`), and the exit is 1. `NullEmbedder`
  (`produces_embeddings is False`) is refused before any read. A missing table (031 not applied)
  → "run `brain init`", exit 1.
- **Output:** `embedded N document(s)`; `M confidential document(s) skipped (hosted embedder)` when
  M > 0; `K document(s) still without a vector for <backend>`. Counts and ids only; never basis
  text, titles or summaries in logs (CLAUDE.md "never log content"). `ANALYZE document_embeddings`
  after a run that wrote rows.
- **No ingest hook and no daemon line (D15).** New and edited documents get or refresh a vector
  the next time the owner runs `backfill` / `backfill --stale`. Until then a new document is
  vectorless (neutral) and an edited one carries its previous vector.

## The stage (`search_doc_leg.py`)

`DocLegStage(embedder: Embedder, weight: float, name="doc_leg")`, frozen dataclass, implements
`SearchStage`, plus `needs_query_vector = True` (D5).

- `candidate_limit(limit) = max(limit, DOC_LEG_DEPTH)`, `DOC_LEG_DEPTH = search.CANDIDATE_LIMIT`
  (50): to lift a document from rank 30 the stage must see rank 30 (D8).
- `signature() = {"doc_leg_weight": weight}` (the spec's key; depth is a constant, D8).
- `apply`:
  1. `q = search.cached_query_embedding(embedder, query)`: a cache hit on the vector
     `hybrid_search` just computed (same identity, `input_type="query"`, same text), so no second
     embed (proven by a counting fake, M12).
  2. One statement: `SELECT document_id::text, 1 - (embedding <=> %s::vector) FROM
     document_embeddings WHERE document_id = ANY(%s::uuid[]) AND embedder = %s AND embedding IS
     NOT NULL`. It returns no content and carries no sensitivity predicate, with the reason written
     inline: it reads vectors only for ids the gated search already returned, and computes in
     Postgres. A foreign-identity row is ignored, as if absent (D11).
  3. `doc_leg_order(n, cosines, weight)` (pure, D6/D7): the **slots** are the indices with a vector.
     Among them, `j` = rank by cosine, descending (stable); `fused = rrf_contribution(i) + weight ·
     rrf_contribution(j)` with `i` the row's pipeline index, k = 60; the slotted rows are re-sorted
     by `fused`, descending, stable, and written back into the same slots. **A vectorless row keeps
     its exact index**: neither promoted nor demoted, the spec's "neutral" (AC3).
  4. No vectors among non-empty candidates → `applied=False`, `skipped_reason="no_document_vectors"`
     (D12), so an un-backfilled corpus shows as skipped and `brain eval` refuses the row instead of
     quietly measuring "off". Empty input → `applied=True`, unchanged.
  5. `SearchResult.score` is not overwritten and no `explain` field is added (D7; RM-14a.D1's
     precedent; a field would grow `search.py` again).
- Every filter and the sensitivity exclusion hold by construction: the stage only reorders rows the
  search returned (spec RM-20.D1, D2 here).

## The pipeline changes (`search_pipeline.py`)

- `build_stages(env, *, embedder, rerank=None)`: reads `load_doc_leg_settings(env)` (a bad knob is
  a `ConfigError` even while off) and `load_rerank_settings(env)`. Returns `(DocLegStage, RerankStage)`
  in that order, each only when enabled (D9). Off/off → `()`.
- **Text-only gate (D5).** `search_was_text_only(embedder, fallback, kwargs)` is true when the
  search fell back, ran `fts_only=True`, or the embedder produces no vectors. `apply_stages(...,
  text_only=False)` then reports each stage whose `stage_needs_query_vector(stage)` is true as
  `applied=False`, `skipped_reason=TEXT_ONLY_SKIP_REASON` (`"text_only_search"`) **without
  calling it**. `stage_needs_query_vector` is the one reader of the duck-typed flag (default
  `False`), the `is_hosted_embedder` / `produces_embeddings` precedent, so `RerankStage`,
  `_UnsizedStage` and the test fakes are unchanged. Without the gate the stage would re-embed after
  a failed embed (`lru_cache` does not cache exceptions): a second full timeout on every fallback
  search, and an embed call under `--fts-only`.
- `run_search` passes `embedder` to `build_stages` and `text_only` to `apply_stages`.

## Eval wiring

- `run_eval(stages=None, env=...)` builds `build_stages(env, embedder=embedder)`; `eval_cmd` and
  `cli.py` are untouched (spec §2.2). `_apply_stages` passes `text_only=not
  getattr(embedder, "produces_embeddings", True)` (eval never falls back and never runs `fts_only`).
- `config_signature` gains `doc_leg_weight` only when the stage is built; off it is byte-identical
  (the existing "after the base keys" loop).
- A row where the stage skipped (`no_document_vectors`, an SQL error, `text_only_search`) is
  `stages_skipped` and refused like a degraded one, by the RM-14a machinery as it stands.
## Owner step (after RM-14c)

Recorded here and in the PR body; not run by this ticket. On the owner's machine, after RM-14c's
ruling has fixed the pipeline the leg is measured on top of, and never during another ticket's run
of record:

1. `brain init` (applies 031), then `brain docembed backfill --stale` until it reports
   `0 document(s) still without a vector`; `brain status` to confirm the document count the
   baseline was recorded on.
2. Two runs in the same session, under RM-14c's adoption rule (spec §2.7): `brain eval --baseline
   ci --diff` with `BRAIN_DOC_LEG=false`, then with `BRAIN_DOC_LEG=true` (weight 1.0; optionally one
   alternative weight). Both sets of numbers go in a follow-up PR.
3. **Adopted** → the adopting PR flips the default, re-records `ci.json`
   (`brain eval --record-baseline ci`) and names the trigger in its commit message: "ranking change
   (document leg adopted)". It also carries the D15 follow-ups below. **Not adopted** → the numbers
   are recorded and the leg stays off.

The tracker row (spec Appendix B) moves to DONE with "(measurement owed after RM-14c)".

## Tests (TDD: each lands red first)

Pure (no Docker):
1. `tests/test_settings_doc_leg.py`: defaults; spellings; weight bounds; a bad value is a
   `ConfigError` naming the variable and not quoting the value; the leaf guard
   (`test_settings_leaf_guards.py`) covers the new module.
2. `tests/test_doc_leg_order.py`: identity at weight 0; a low-pipeline/high-cosine row rises; ties
   keep order; vectorless indices are fixed points; the RRF arithmetic on a hand-computed case.
3. `tests/test_doc_embeddings_basis.py`: both bases, blank summary → head, the 512-token head, the
   700-token whole-basis cap on a long synthetic title, hash stability, the char cap.
4. `tests/test_doc_leg_build_stages.py`: off → `()`; on → `(DocLegStage,)`; on + reranker →
   doc leg first; bad knob while off → `ConfigError`; `stage_needs_query_vector` default; the gate
   skips without calling `apply` and still runs other stages.

Real DB (port 5434, `second_brain_test_rm20`; need Docker):
5. `tests/test_migration_031_document_embeddings.py`: applies; re-run is a no-op; CHECK refuses an
   unknown basis; `ON DELETE CASCADE`.
6. `tests/test_db_relational_embedding_columns.py`: a 4096 fake resizes both while empty; refuses
   when either holds vectors; preflight leaves chunks untouched when only the doc table refuses;
   through `brain init` (CliRunner), `backup.restore` (its `embedder=` seam) and the demo seed (AC5).
7. `tests/test_doc_embeddings_backfill.py`: missing-only default; `--limit`; `--stale` on a changed
   summary and on a foreign `embedder` value (AC4); untouched rows not re-embedded; hosted fake
   never receives a confidential basis and the report counts it; a local fake does embed it (AC3);
   NullEmbedder refused; a transient `EmbedError` keeps earlier batches and exits 1 class-only; a
   document deleted between select and write; no synthetic summary secret in caplog or output.
8. `tests/test_doc_leg_stage.py`: the summary-matches/chunks-barely corpus moves the document up
   (AC2); filters still exclude what they excluded (`source_kind`, `tag`, `sensitivity`, draft)
   (AC2); a confidential vectorless row under the hosted fake keeps its index (AC3);
   foreign-identity rows ignored; `no_document_vectors` skip; one embed call per search (counting
   fake).
9. `tests/test_doc_leg_surfaces.py`, mirroring `test_rerank_surfaces.py`: `brain search`, `brain
   explain`, MCP `brain_search`, `GET /api/search`, `brain recall`, `ask`'s retrieval each move the
   document and report `doc_leg` in `stages`/timings; a failed embed → fallback notice and
   `doc_leg` skipped `text_only_search` with the embedder called once; `--fts-only` → skipped,
   embedder never called; **off → no `doc_leg` on any envelope** (AC1).
10. `tests/test_eval_runner_doc_leg.py`: `run_eval(env={BRAIN_DOC_LEG: true})` scores the fused
    order and the signature gains exactly `doc_leg_weight`; `env={}` signature equals master's four
    keys (AC1); a skipped stage marks the row `stages_skipped`.
11. `tests/test_cli_docembed.py`: help, output lines, exit codes 0/1/2, missing-table hint.
12. `tests/docleg_isolation_plugin.py` + `tests/test_docleg_isolation.py`: `BRAIN_DOC_LEG=false`
    and no weight knob for every test (set, not deleted, for the `setdefault` reason
    `rerank_isolation_plugin` gives), and the plugin is registered.

Named runs always include `tests/test_log_privacy_rules.py`, `tests/test_log_privacy.py`,
`test_import_hygiene`, `test_vault_delete`, `tests/test_thread_wait.py`,
`tests/test_search_pipeline_boundary.py`, `tests/test_search_fallback_eval_untouched.py`, and
`scripts/check_ceiling_table.py`. New exception classes (if any) end in `Error`; none is stored on
an attribute. New files are scanned for raw bidi/control characters before commit.

## Mutation proofs (each in a `git archive` copy, deleted after use)

Each is a pair: the test that turns red when the guard is removed, and the test that proves the
ordinary path still works (so the guard is not "refuse everything").

| # | Guard removed | Refuses (goes red) | Keeps ordinary behaviour (stays green) |
|---|---|---|---|
| M1 | hosted `not_confidential_sql` in the backfill selection | hosted fake receives a confidential basis | local fake embeds the confidential doc |
| M2 | `AND embedder = %s` in the stage lookup | a foreign-identity vector reorders rows | a same-identity vector reorders them |
| M3 | slot preservation in `doc_leg_order` (fuse all, vectorless get 0) | confidential vectorless row's index changes | vectored rows still reorder |
| M4 | the text-only gate in `apply_stages` | fallback search calls the embedder twice; `--fts-only` calls it at all | a normal search applies `doc_leg` |
| M5 | `settings.enabled` check in `build_stages` | off → a `doc_leg` stage / signature key appears | on → exactly one stage |
| M6 | the preflight pass | chunks resized although the doc table refuses | both empty → both resized |
| M7 | the 700-token whole-basis cap | long-title basis exceeds the cap | a short basis is unchanged |
| M8 | the `embedder` comparison in `--stale` | AC4 row not re-embedded | an up-to-date row is not re-embedded |
| M9 | `SELECT ... FROM documents WHERE id = %s` guard on the upsert | deleted-mid-run doc raises a FK error | a present doc is written |
| M10 | the `NullEmbedder` refusal | backfill reaches `embed()` and raises | a vector fake backfills |
| M11 | `cached_query_embedding` (stage calls `embedder.embed` directly) | counting fake sees 2 embeds per search | the stage still reorders |
| M12 | the `no_document_vectors` skip | un-backfilled corpus reports `applied=True` | partial coverage applies |
| M13 | validate-while-off in `load_doc_leg_settings` | bad weight with the leg off is accepted | a good weight loads |

## Security and privacy

- **F6 egress:** under a hosted embedder no confidential document's basis is sent (M1); the stage
  sends nothing new, because its query vector is the one search already computed (M11). The
  stage never demotes or promotes a confidential vectorless row (M3).
- **Confidential gating at read:** the stage re-scores only rows the gated search returned and
  returns no content, so the sensitivity lens, drafts and every filter hold (spec RM-20.D1). Each
  new statement that reads `documents` carries a sensitivity predicate or an inline reason. None
  filters `draft`, so none enters the F6 residue grep set; the backfill's inclusion of drafts is
  reasoned inline anyway.
- **Logging:** counts, ids, stage names and `describe_failure` class names only; no basis text,
  titles, summaries or queries at any level (G1 guard stays green without a new allowlist entry).
- **No PII:** every fixture synthetic, built from named constants. No golden-corpus content read
  or printed.

## Risks

- **Summary-dominated signal over-promotes long documents** (spec): bounded by the weight knob,
  the slot rule, and the owner measurement; off by default.
- **Stale vectors between edits and `--stale`:** an edited document ranks by its old basis until
  the next run (D15). Visible via the "still without a vector" count only for missing rows.
- **Cache miss on a surface whose `search_fn` alters the query:** costs one extra embed, never a
  wrong vector (identity-keyed cache).
- **Dim mismatch before `init`** (backend switched, init not re-run): the lookup raises, the
  pipeline's broad catch reports `doc_leg` skipped (class-only); search unaffected.
- **Sensitivity flipped mid-backfill under hosted:** a document marked confidential between the
  page read and the embed call could be sent. Same window as `brain reembed`; accepted, recorded.
- **`build_stages` signature change** touches 13 test call sites and two `cli_search.py` lines;
  mypy catches any missed caller.

## Docker / environment

- **No Docker needed:** this plan; pure tests 1–4; `ruff check`, `mypy src/`,
  `scripts/check_ceiling_table.py`; the mutation proofs for M3, M5, M7, M13.
- **Docker needed (test Postgres on 5434 only):** tests 5–12, the remaining mutation proofs, and
  the DoD-17 end-to-end transcript (`brain init` on a qwen3-dim test DB, `brain docembed backfill`,
  `brain search --json --meta` with `BRAIN_DOC_LEG=true`) against `second_brain_test_rm20`.
  Starting Docker is the user's call.
- **Owner's machine / prod:** the eval measurement only (owner step, after RM-14c). Never here.
- CI is the full-suite evidence; no local full suite.

## Decisions (each with a recommendation)

- **D1 — Ship before RM-14c.** Recommend: ship default OFF now; measurement is a recorded owner
  step after RM-14c. Alternative: hold the ticket. (Coordinator ruling requested.)
- **D2 — Re-score, not add candidates.** Spec RM-20.D1, inherited.
- **D3 — No HNSW on the new column.** Spec RM-20.D2, inherited; also moot for qwen3's 4096 dims.
- **D4 — `build_stages` takes a required keyword `embedder`.** The stage needs it for the cache
  lookup; required means no silently-off path. Alternative: optional with `None` → stage omitted
  (a silent trap).
- **D5 — A pipeline-level text-only gate** via a duck-typed `needs_query_vector` read by one
  function. Alternative A: a Protocol property (touches `RerankStage`, `_UnsizedStage`, every
  fake). Alternative B: no gate (a second embed timeout on every fallback; breaks `--fts-only`).
- **D6 — Vectorless rows hold their exact index** (slot rule, as RM-14a does for confidential
  rows). Makes the spec's "neutral, never demoted" literally true; the alternative (no
  contribution, others gain) demotes them.
- **D7 — Fusion `rrf(i) + w·rrf(j)` over the slotted rows, stable;** `score` untouched, no
  `explain` field (would grow `search.py`).
- **D8 — Fixed depth = `CANDIDATE_LIMIT` (50), no knob;** signature stays `{"doc_leg_weight"}`.
  Alternative: a `BRAIN_DOC_LEG_DEPTH` knob (then in the signature too). YAGNI until measured.
- **D9 — Order: doc leg, then reranker,** so the leg can lift a document into the reranker's head.
  Alternative: rerank first (the leg would then disturb the cross-encoder's order).
- **D10 — `embedder` column = `search.embedder_identity(embedder)`** (class, model, dim): the one
  existing definition of "same vectors", exported in the same two-line alias as the cache.
  Alternative: `cfg.embedder` (`arctic`), which misses a `QWEN3_MODEL` change and is not reachable
  from `build_stages`.
- **D11 — The stage uses only rows whose `embedder` matches;** others are treated as absent.
- **D12 — Zero vectors among non-empty candidates is a skip (`no_document_vectors`);** partial
  coverage applies. Keeps an un-backfilled run from masquerading as a measurement.
- **D13 — Basis bounds:** head read from the first 8,192 chars; the whole basis capped at 700
  cl100k tokens (the chunk bound). A small, deterministic refinement of the spec's "first 512
  tokens" that rules out a per-document overflow loop.
- **D14 — Preflight every target before any resize,** so `init` never half-reconciles.
- **D15 — No ingest hook, no daemon line.** Manual `backfill [--stale]` while the leg is off; an
  ingest hook or daemon line is for the adopting PR if the measurement adopts it.
- **D16 — Backfill stops on the first `EmbedError`,** keeping committed batches, exit 1,
  class-only. Alternative: skip-and-continue per batch (hides a dead embedder behind a long run).
- **D17 — `BRAIN_DOC_LEG_WEIGHT` in `[0.0, 10.0]`.** 0 is a usable control in eval; the upper
  bound catches a typo such as `100`.
- **D18 — `brain ui`'s `results.js` meta line is not changed.** `timing_ms.doc_leg` and `total`
  already carry the time; showing it is for the adopting PR.

## Rulings (team-lead, 2026-11-07, on `071a726c`)

- **D1 yes**: ship now, off, no signature key while off; the "Owner step (after RM-14c)" section
  above is the recorded measurement. **D2, D3, D4, D6–D9, D11, D13, D14, D16–D18** accepted as
  recommended. Any touched file over 800 lines gets the full ceiling treatment.
- **D5, amended:** the flag is declared in a typed Protocol in `search_pipeline`,
  `class QueryVectorStage(Protocol): needs_query_vector: bool`, so mypy sees the contract;
  `stage_needs_query_vector` remains the only reader, with a documented `getattr(..., False)`
  default, so `RerankStage`, `_UnsizedStage` and the fakes are unchanged. Test: a stage without the
  attribute is treated as `False`.
- **D10:** the identity function (`search.embedder_identity`, alias of `_embedder_identity`) is named
  in the PR body; test: a change of **model alone** (same class, same dim) makes a row stale.
- **D12:** both sides tested: one vector among the candidates → the stage applies; zero → skipped
  (`no_document_vectors`), and `brain eval` refuses the row loudly.
- **D15 follow-ups (for the adopting PR):** an ingest-time hook or a daemon line
  (`brain docembed backfill --stale`), and a `brain doctor` coverage line (vectors / eligible
  documents for the active embedder).
  A document that becomes confidential after a hosted embed keeps its old vector (as its chunks
  do); a future `--stale`-on-sensitivity rule could drop or refresh it.
  Known limitation (sec-127 I6): the UI's wrapped embedders (`ui/ask_service.py`,
  `ui/ingest_progress.py`) carry their own class identity, so `embedder_identity` never matches the
  backfill's rows and the leg would be inert (reported `no_document_vectors`) on those paths. It
  fails safe. The adopting PR unwraps to the inner embedder's identity.

Spec items made explicit:

- (a) `EMBEDDING_TARGET_ALLOWLIST` gains `("document_embeddings", "embedding")`, with a test.
- (b) AC5 at all three call sites: `brain init` on a qwen3-dim DB, `backup.restore`, and the demo
  (both `ensure_*` calls: the seed and the container bring-up).
- (c) AC2's "filters still exclude what they excluded" with at least `--source`, `--tag` and the
  sensitivity lens.
- (d) AC3, both halves: the hosted fake's received texts never contain a confidential basis, and
  the confidential row keeps its index when the stage runs.
- (e) Backfill logs and output carry ids and counts only, never titles, summaries or content
  (caplog + output assertion with a synthetic marker in all three fields).

## Review round 1 (rev-127 3 LOW, sec-127 3 LOW + INFO, on `7d9df323`)

- rev 1: the AC3 index test now uses a confidential **lead** document (its chunk is the query, so
  it ranks above the fit document in plain order) that the fit document is lifted past; M3 now
  demotes it to last (simulated offline for every lead position; the DB run is owed).
- rev 2: the refusal test's comment says what it does (only the document table refuses), and the
  duplicate `pytest.raises` is gone; the preflight's own proof is the next test (M6).
- rev 3: `BackfillReport.foreign` counts documents whose vector another embedder or model wrote;
  the CLI prints a `--stale` hint when a missing-only run leaves any, and says "still without a
  vector from the <backend> embedder" (`cfg.embedder`), restoring the plan's wording.
- sec L1: each hosted batch is re-checked just before the embed call
  (`confidential_lookup.confidential_document_ids`, a new shared leaf that the reranker and the UI's
  snippet redaction now use too, replacing two copies of the same statement).
- sec L2: the hosted received-texts test runs on the default and `--stale` paths.
- sec L3: `_PAGE_SQL` reads `left(title, 2048)` and `left(summary, 8192)`; `document_basis` applies
  the same caps, so the basis is defined on those prefixes.
- INFO: the F6 residue audit records the RM-20 statements; D15 gains the sensitivity-flip residue.

## Execution while Docker is down

1. The no-DB work first: settings, `doc_leg_order`, basis, `build_stages`, the gate; mutations
   M3, M5, M7, M13.
2. The DB tests are written too and validated by CI's own Postgres; push once the no-DB set,
   `ruff`, `mypy` and the ceiling check are clean, then iterate on CI.
3. The DB mutation proofs (M1, M2, M4, M6, M8–M12) and the DoD-17 end-to-end transcript are
   **owed (Docker down)** and listed so in the PR body, as in #123 and #124.

**DB round (2026-11-08, own test DB `second_brain_test_rm20` on 5434, fakes only).** Every owed
DB mutation was run in a `git archive` copy (anchor applied once, `brain` imported from the copy,
copy deleted), each a refuses/keeps pair, and every one held: M1 (the hosted gate), M2
(`embedder = %s`), M3 against the rev-1 DB test, M6 (the preflight), M8 (the `--stale` embedder
comparison), M9 (the upsert's existence guard), M18a/M18b (the page filter on the default and
`--stale` paths, killed by the new sec-127b I1 never-read test while the never-sent test stays
green), M19 (the pre-embed re-check) and M20 (the `--stale` hint). The DoD-17 transcript ran the
real Typer app: `brain init` (qwen3) resized `document_embeddings.embedding` 1024 → 4096;
`brain docembed backfill` embedded 5, `--stale` 0; with `BRAIN_DOC_LEG=true` the fitting
document went from last to first with `doc_leg` applied; `--fts-only` reported it skipped
(`text_only_search`).

## Merge with master at `cfcf327d` (RM-54, RM-55, BL-39 merged)

Three conflicts, each resolved as a union. `CHANGELOG.md` [Unreleased] keeps every entry (RM-20,
RM-55, RM-54, BL-39). `cli_search.py`'s two `run_search` calls pass both
`stages=override_stages(rerank, embedder=embedder)` and RM-54's `rewriter=make_rewriter(...)`.
`search_pipeline.py` keeps `_apply_one` and RM-54's `_search_paraphrases` / `_rewrite_leg`, and in
`run_search` both the text-only flag and the rewrite leg. **Order:** the rewrite fuses the
paraphrases' rankings before the stages, so `doc_leg` (then the reranker) re-scores the fused rows
against the original query, whose vector the original search cached; the rewrite's report leads
`stages`. Two tests guard that order: master's
`tests/test_query_rewrite.py::test_stages_see_the_fused_rows_and_the_original_query`, and
`tests/test_doc_leg_build_stages.py::test_with_a_rewrite_the_vector_stage_runs_after_it_on_the_original_query`,
whose stand-in search returns `doc-para` only for the paraphrase and which asserts the vector stage
saw it (so running the stages before the rewrite leg turns it red).

## Deviations from the spec

D5, D6 (the exact meaning of "neutral"), D8, D12, D13 and D14 fill in or tighten points the ticket
leaves open; none changes an acceptance criterion. Found during implementation:

- **`cli.py` is not net zero.** `init`'s AGE-gated graph call still needs `ensure_embedding_column`,
  so the import list gains `ensure_relational_embedding_columns` rather than swapping one name for
  another. It is the file's tenth growth: one import line with its inline note, plus a three-line
  docstring paragraph (+5), with a ceiling-table hop.
- **`search.py` grows +6, not +2:** the comment and the alias line, the two blank lines PEP 8 puts
  around a top-level statement, and the two-line docstring hop (which also promoted RM-24's
  descriptive hop to `8951620` / 832).
- **`skills/brain-maintenance/SKILL.md` is not edited:** it is at the house cap of 300 lines
  (`tests/test_skill_frontmatter.py`), so the command is documented in `docs/cli-reference.md` only,
  as RM-14a's "only if under its line ceiling" allowed.
- **AC5's demo bring-up site (`demo.provision`) is checked from its source,** because it starts a
  Docker container; the demo seed and `restore_backup` are exercised for real
  (`tests/test_demo_seed.py`, `tests/test_restore_swap.py`).
