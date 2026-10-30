# RM-15 — Saved searches and watch lists, notified through the daily brief

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

- **Ticket:** RM-15 in `docs/specs/2026-10-23-roadmap-completion-design.md` (the ticket is the
  spec; this plan is the task list). Migration **029** (§2.8).
- **Branch:** `feat/rm-15-saved-searches`, from `second-brain/master` at `52019a3` (PR #76 merged).
- **Depends on:** RM-06 (registration seams), RM-09 (`search_pipeline.run_search`), RM-08
  (settings leaves). All merged.

## Tasks, in order

### T1 — Step 1, the `brief --wiki` extraction (behaviour-identical)

- New leaf `src/brain/brief_publish.py`:
  `build_brief(cfg, *, on_date, since_hours, todo_since_days, enrich, wiki, connect=db.connect,
  embedder=None) -> tuple[BriefData, Path | None]`. The whole two-audience sequence of `cli.brief`
  moves there with its comment: one connection; the permissive payload; under `wiki` the gated one;
  `withheld` while both carry `suggestions=[]`; suggestions unless `enrich` is false (the gated page
  gets its own call only when `withheld`); `write_brief_to_vault` before any `--json` return.
- `brief_publish` reaches `assemble_brief` / `suggest_next_steps` / `write_brief_to_vault` through
  the `brain.brief` MODULE at call time, as `cli.brief` did through its function-local import:
  `tests/test_cli_vault_emit_confidential.py` patches `brain.brief.suggest_next_steps`.
- `cli.brief` keeps parsing, the call and the printing. `cli.py` shrinks.
- **Proof:** `tests/test_brief.py`, `tests/test_cli_vault_emit_confidential.py`,
  `tests/test_mcp_brief.py` pass unchanged (AC1). New `tests/test_brief_publish.py` calls
  `build_brief` directly with the injected `connect`.

### T2 — Migration 029 and its test

- `src/brain/migrations/029_saved_searches.sql`, exactly the ticket's DDL, in `BEGIN`/`COMMIT`,
  every statement `IF NOT EXISTS`.
- `tests/test_migration_029_saved_searches.py`: tables, columns, the `mode` CHECK, both cascades,
  the partial index, and an idempotent re-apply (`fresh_schema`).

### T3 — Settings leaf

- `src/brain/settings/watch.py`: `WatchSettings(min_cosine: float)`,
  `load_watch_settings(env)`; `BRAIN_WATCH_MIN_COSINE`, default 0.45, in `[0, 1]`.
  `tests/test_settings_leaf_guards.py` covers the leaf automatically.

### T4 — Core: `saved_search.py` (definitions, filters, hit reads) and `saved_search_check.py` (matching)

- `saved_search.py`: `SavedSearch`, `WatchFilters`, `WatchHit`, `WatchNote`, errors
  (`SavedSearchError` ⊂ `BrainError`, `SavedSearchNotFound`, `SavedSearchExists`); `add` (insert
  only), `get`, `list_watches`, `remove`, `set_enabled`; `window_hits` (the terminal set),
  `unnotified_view` (the published set: ≤ 5 per watch, "N more", and the full pending key set
  including confidential and over-cap hits), `mark_notified(keys)`, `recent_hits`,
  `watch_tables_present`.
- `saved_search_check.py`: `check_watch` (one transaction: `FOR UPDATE`, search through
  `run_search` with the saved filters plus `updated_after = last_checked_at − 1 h`, `INSERT … ON
  CONFLICT DO NOTHING RETURNING`, `last_checked_at = GREATEST(…, now())`); `peek_watch` (same
  search, no writes, optional F6 lens); `check_enabled` (every enabled watch); `seed_watch`.
  Lexical → `fts_only=True`; semantic → `explain=True`, keep `vector_cosine ≥ min_cosine`; a
  fallback (or a `none` embedder) → `skipped`, nothing recorded, `last_checked_at` unchanged.

### T5 — CLI `brain watch` (`cli_watch.py`, registered through `cli_registry.EXTENSION_REGISTRARS`)

- `add NAME QUERY [--source] [--tag] [--person] [--kind] [--without-tag] [--semantic]`,
  `list [--json]`, `check [NAME] [--json] [--peek]`, `hits [NAME] [--since 7d] [--json]`,
  `rm NAME`, `pause NAME`, `resume NAME`. Filters validated with `validate_source_kind` and
  `resolve_person_to_keys`, as `brain search` does.

### T6 — The brief section

- `BriefData.watch_hits` / `watch_notes` (default empty) and their `to_dict` keys; the terminal
  and Markdown renderers list them only when there is something to list.
- `assemble_brief` reads the terminal set (`first_seen_at` inside `since_hours`, notified or not)
  through its own `exclude_confidential`, so MCP `brain_brief` gets it through its lens with no
  `mcp_server.py` edit and runs no check.
- `build_brief`: checks every enabled watch FIRST on its connection, then assembles; under `wiki`
  the gated payload's hits are replaced by the unnotified view; after the page is written, every
  pending key is marked notified on a second connection.

### T7 — MCP (`mcp_tools/saved_search.py`)

- `brain_watch_list()`, `brain_watch_add(...)`, `brain_watch_check(name=None,
  include_confidential=False)` (peek only), `brain_watch_remove(name, confirm=False)`.

### T8 — Docs, ceiling, tracker, memory

- `docs/cli-reference.md`, `docs/configuration.md` (knob + four MCP tools), `CHANGELOG.md`,
  `skills/brain-proactivity/SKILL.md`; the backlog row (BL-9) → SHIPPED; Appendix B row → DONE in a
  follow-up commit; CLAUDE.md `cli.py` ceiling hop (net negative, bound to its SHA by a later commit
  that does not touch `cli.py`); memory `schema.md`, `cli.md`, `types.md`.

## Decisions made here (none edits the ticket text)

- **D1, `build_brief` takes an `embedder` keyword.** The ticket's signature has none, but a semantic
  watch needs one. `None` builds `make_embedder(cfg)` lazily, and only when an enabled semantic
  watch exists, so a lexical-only brief never constructs an embedder. Tests inject a fake.
- **D2, `watch_notes` beside `watch_hits`.** The published section needs "and N more" and
  "skipped, embedder unavailable" per watch; a hit row cannot carry either. `WatchNote(watch, more,
  skipped)`; `to_dict` gains both keys.
- **D3, the terminal set is read inside `assemble_brief`.** That is the only way MCP
  `brain_brief` "reads the terminal set through its MCP lens" without growing `mcp_server.py`.
  `build_brief` then swaps the published payload's hits for the unnotified view.
- **D4, `withheld` ignores the watch fields.** They differ between the two payloads by
  construction (window vs. unnotified), and they are not in the suggestion prompt; comparing them
  would buy a second Ollama call for nothing and change the extraction's behaviour.
- **D5, `build_brief`'s connection is autocommit.** Each check is its own committed transaction
  (`conn.transaction()`), so the row lock is released per watch, not held through the brief's
  reads. The reads are unchanged by it.
- **D6, the brief skips the watch step when migration 029 is not applied**
  (`to_regclass('public.saved_searches')`). The launchd install is reinstalled after a merge
  without `brain init`, and the 07:00 brief must not start failing. `brain watch` itself says
  "run `brain init`".
- **D7, a check runs no ranking stages (`stages=()`).** A watch asks "does this match", which a
  reranker cannot change; it would only add a network call per watch per brief.
- **D8, the check fetches 50 rows** (`search.CANDIDATE_LIMIT`, each leg's own cap).
- **D9, seeding.** `add` seeds the unwindowed top 50 plus everything in the first check's window
  (`updated_after = now − 1 h`), so an immediate check reports nothing (AC2). A semantic `add`
  whose seed search falls back is refused (exit 1 / `INVALID_PARAMS`): it cannot seed, and the
  first brief would flood.
- **D10, no confidential-derived counts on MCP.** `brain_watch_add`, `brain_watch_list` and
  `brain_watch_remove` return no hit or seed counts (a count that includes confidential matches is
  the membership oracle `mcp_server._confidential_lens` describes). `brain_watch_check` puts the
  lens in the search predicate.
- **D11, filters are stored canonical.** Tags go through `brain.tags.normalize_tag`; the person is
  stored as typed and resolved at every check (the directory can change). A person that no longer
  resolves skips that watch, like a fallback: nothing recorded, `last_checked_at` unchanged.
- **D12, `check NAME` on a paused watch runs it.** The name is an explicit request; `check` with no
  name and the brief check enabled watches only.

## Risks

- **A check fetches at most 50 documents** (`CHECK_LIMIT`, each leg's candidate cap). When more
  newly match in one window (a bulk import), the rest are never recorded: the window moves on.
  A lexical check counts them exactly (`SearchDiagnostics.total_documents`) and a semantic one
  says it filled the limit; the brief, `brain watch check` and MCP report it (review round, D14).
- A semantic check holds its row lock for one embed (accepted by the ticket).
- A match outside the seed's top 50 that is later edited is reported once (it is new in the
  window, and was never reported before).
- `brief.py` grows (≈ 315 → ≈ 400 with the renderers); it stays well under 800.

## Review round (rev-78, at `2dbbd38`; fixed in `82bc809`)

- **D6 strengthened, a bad knob cannot break the brief.** `BRAIN_WATCH_MIN_COSINE` was read when
  the matcher was built, so a bad value made every `brain brief` raise, even with no watches or
  without 029. `build_matcher` now reads it lazily, only when a semantic watch is checked, and a
  `ConfigError` there skips that watch (`SKIP_MIN_COSINE`); `build_brief` builds the matcher only
  when 029 is present. With the knob lazy, nothing on the CLI or MCP path raises a `ConfigError`
  any more: a semantic `add` is refused through the existing seed-failure path (exit 1 /
  `INVALID_PARAMS`, naming the variable, never the value), and a semantic check reports the skip.
- **D14, the fetch limit is reported** (the risk above). `CheckResult.unrecorded` / `capped`,
  `saved_search_check.limit_note`, `WatchNote.limit`. The published page gives the line without
  its count, because the count is taken over both tiers (the F6 membership oracle); the coordinator accepted
  this. No `ConfigError` handler was added to the CLI or MCP: with the knob lazy it would be
  unreachable (ruling).
- **D13 revised, the seed marker is a column.** Migration 029 (unshipped, never applied to prod,
  confirmed by the coordinator) gains `saved_search_hits.seeded BOOLEAN NOT NULL DEFAULT FALSE`,
  written by `add`'s seed; `NOT_SEED_SQL` is `NOT h.seeded`. The earlier marker
  (`notified_at = first_seen_at`) would have hidden a real hit recorded and marked in one
  transaction, which RM-16 could do; a regression test pins it.
- `brain_brief`'s MCP docstring names `watch_hits` (`mcp_server.py`, reworded in place, net zero;
  its CLAUDE.md row gains the hop). A test pins that the suggestion prompt carries no watch hit,
  which is what keeps D4 safe.
- **Recorded, no code change.** (1) A second `--wiki` run on the same day rewrites the page, and
  the hits the first run marked drop off it; AC6 requires that, so it is backlog row BL-20 ("brief
  page: include hits notified on the same date"). (2) `saved_search_hits` has no index on
  `document_id`; the ticket's DDL has none, and the `ON DELETE CASCADE` from `documents` then
  scans the table per deleted document, which is cheap at this table's size. Add one if it grows.

## What the end-to-end run found, and the fix

The e2e run (real CLI, a scratch database on 5434, `BRAIN_EMBEDDER=none`, a scratch vault) showed
the matches `brain watch add` seeds as already seen listed in the terminal brief and in
`brain watch hits` as if a check had found them. **D13, a seed is not a hit:** a seed is the only
row recorded seen and notified in one transaction, so its `notified_at` equals its
`first_seen_at`; `hits_since` leaves those rows out (`saved_search.NOT_SEED_SQL`). A hit a check
finds is inserted unnotified and marked by a later transaction, so the two always differ.
Regression tests in `tests/test_saved_search_check.py` and `tests/test_saved_search.py`. (The review round replaced this marker with the explicit `seeded` column; see above.)

## Mutation proofs

Run at `fcb3779` in a `git archive` copy (`PYTHONDONTWRITEBYTECODE=1`, `__pycache__` purged
before each run, `PYTHONPATH` pinned to the copy's `src`, the import path checked by realpath to be
the copy's). Each mutation is asserted to have applied (the original text gone, the mutant
present) before its test runs; the copy is restored from a second pristine archive between runs.
Baseline: the 13 target tests pass; after the last restore they pass again.

| Id | Mutation | Killed by |
|---|---|---|
| M1 | `ON CONFLICT … DO NOTHING` removed from the hit insert | `test_a_new_document_is_reported_once` |
| M2 | `GREATEST(last_checked_at, %s)` → `%s` | `test_last_checked_at_never_moves_backwards` |
| M3 | `FOR UPDATE` removed from `lock_watch` | `test_two_concurrent_checks_report_each_hit_once` |
| M4 | the check's window dropped (`updated_after=None`) | `test_an_old_untouched_match_outside_the_window_is_not_a_hit` |
| M5 | the semantic fallback skip removed | `test_a_semantic_check_that_falls_back_records_nothing_and_keeps_its_window` |
| M6 | `pending` collects only rows that pass the lens | `test_unnotified_view_withholds_confidential_but_still_marks_them` |
| M7 | the lens in `unnotified_view` disabled | `test_wiki_lists_five_gated_hits_marks_all_and_a_second_run_lists_none` |
| M8 | the per-watch cap disabled | `test_unnotified_view_caps_each_watch_and_counts_the_rest` |
| M9 | `NOT_SEED_SQL` dropped from `hits_since` | `test_seeded_matches_are_not_hits_in_the_terminal_set` |
| M10 | `withheld` compares the watch fields too | `test_watch_fields_alone_do_not_cost_a_second_suggestion_call` |
| M11 | MCP `brain_watch_check` records (`check_*` for `peek_*`) | `test_check_peeks_and_never_records_even_with_the_embedder_down` |
| M12 | MCP `brain_watch_check` drops the lens | `test_check_applies_the_lens` |
| M13 | `assemble_brief` reads hits without the migration-029 guard | `test_a_brief_without_migration_029_still_builds` |
| M14 | `build_brief` never marks | `test_wiki_lists_five_gated_hits_marks_all_and_a_second_run_lists_none` |

M10 survived its first run: the old test's two payloads carried equal watch fields when
`withheld` was computed. The test now uses a skipped semantic watch, whose note is on the terminal
payload only at that point (`fcb3779`), and M10 is killed.

### Review-round mutation proofs

Run at `90c7b18`, with the same harness as above. The baseline of 9 target tests passes, and it passes again after the last restore.

| Id | Mutation | Killed by |
|---|---|---|
| R1a | `build_matcher` reads the knob eagerly | `test_a_bad_min_cosine_leaves_a_lexical_brief_working`, `test_a_bad_min_cosine_skips_semantic_watches_only` |
| R1b | `_cosine_floor` lets the `ConfigError` out | `test_a_bad_min_cosine_skips_semantic_watches_only`, `test_a_bad_min_cosine_skips_a_semantic_watch_with_a_note` |
| R1c | `build_brief` checks watches without migration 029 | `test_a_bad_min_cosine_without_migration_029_still_builds` |
| R2a | the lexical overflow count is dropped | `test_a_lexical_check_over_the_limit_counts_what_it_could_not_record` |
| R2b | `capped` is never set | `test_a_semantic_check_that_fills_the_limit_says_more_may_exist` |
| R2c | the page gets the count | `test_a_check_over_the_limit_is_noted_with_its_count_only_in_the_terminal` |
| R3a | `NOT_SEED_SQL` reverts to `notified_at IS DISTINCT FROM first_seen_at` | `test_a_real_hit_marked_in_its_checks_transaction_is_still_a_hit` |
| R3b | the seed is written with `seeded = FALSE` | `test_seeded_matches_are_not_hits_in_the_terminal_set` |
