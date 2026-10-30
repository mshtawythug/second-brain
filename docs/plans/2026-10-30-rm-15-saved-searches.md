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

- A semantic check holds its row lock for one embed (accepted by the ticket).
- A match outside the seed's top 50 that is later edited is reported once (it is new in the
  window, and was never reported before).
- `brief.py` grows (≈ 315 → ≈ 400 with the renderers); it stays well under 800.

## Mutation proofs (run in a `git archive` copy)

Recorded in the PR body: `ON CONFLICT DO NOTHING` removed; `GREATEST` removed; the `FOR UPDATE`
removed; the window filter removed; the fallback skip removed; the mark-every-pending-key set
narrowed to shown hits; `exclude_confidential` dropped from the unnotified view; the per-watch cap.
