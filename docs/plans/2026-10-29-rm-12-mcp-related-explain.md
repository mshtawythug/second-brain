# RM-12 — MCP `brain_related` and `brain_explain`

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-12 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(wave 3; BL-6; backlog rank 5). The ticket is the spec; this is the task list.
**Base:** `second-brain/master` `a873074` (RM-01..RM-10 merged).

## Goal

An agent can ask for the documents related to one it has open, and can ask why a search ranked
what it did, and gets the same answers as `brain ui`'s related panel and `brain explain --json`.
Both tools are the first real module behind RM-06's seam (`src/brain/mcp_tools/`), so
`mcp_server.py` does not grow.

## Files

| File | Change |
|---|---|
| `src/brain/mcp_tools/related.py` (new) | `brain_related`, `brain_explain`, `register(app, ctx)`, `RELATED_LIMIT_CEILING = 20`. Under 400 lines (the seam's per-module budget). |
| `src/brain/format_search.py` | `explanation_json(explain: SearchExplanation \| None) -> dict[str, Any] \| None`: the explain object, moved out of `cli_search.explain`'s inline literal. |
| `src/brain/cli_search.py` | `explain --json` calls `explanation_json`. Net negative (−18 lines; spec §2.2 budget). |
| `tests/test_mcp_related.py` (new) | DB tests for `brain_related`: shape, prefix resolution, root gate, candidate lens, ceiling, parity with `GET /api/notes/{id}/related`. |
| `tests/test_mcp_explain.py` (new) | DB tests for `brain_explain`: shape, parity with `brain explain --json`, confidential lens, ceiling, fallback keys, filters. |
| `tests/test_explanation_json.py` (new) | `explanation_json` pure tests (key set and order, `None`). |
| `tests/test_mcp_tools_seam.py` | Carry-forward (aud-68): the two tests that iterate the real tool modules assert the list is non-empty and names `brain.mcp_tools.related`, so they can no longer pass vacuously. |
| `tests/test_mcp_server_protocol.py` | AC7: `brain_related` is called over `python -m brain.mcp_server` against a seeded test DB. The stdio-equals-in-process test now also asserts both new tools are in the served list (carry-forward). |
| `tests/test_mcp_tool_registration.py` | Docstring of the probe-only guard updated (an extension tool now ships); no logic change. |
| `docs/configuration.md` | MCP tools table: two rows. |
| `docs/guides/claude-desktop-setup.md` | MCP tool table: two rows. |
| `docs/cli-reference.md` | One line under `brain explain`: its MCP twin. |
| `CHANGELOG.md` | Unreleased entry. |
| `skills/consult-brain/SKILL.md` | Its `brain explain` section names the two MCP twins, in one added line: the skill is at the 300-line house ceiling (`tests/test_skill_frontmatter.py`). |
| `docs/plans/2026-10-23-backlog-reconciliation.md` | Rows 1.2, 6.1, Q1-B D13, BL-6, rank 5 → SHIPPED (#PR). |
| Spec Appendix B | Follow-up commit once the PR number is known: RM-12 → `DONE (#PR)`. |

No over-ceiling file is touched: `mcp_server.py`, `cli.py`, `config.py` and `search.py` are
unchanged, so no ceiling-table row moves.

## Design (as the ticket states it, with the choices it leaves open)

- **`brain_related(id, limit=DEFAULT_RELATED_LIMIT, include_confidential=False)`.**
  `limit < 1` → `INVALID_PARAMS`; `limit > RELATED_LIMIT_CEILING` (20) → `INVALID_PARAMS` through
  `mcp_limits.check_ceiling`. The id prefix resolves with `queries.resolve_document_prefix`, its four
  exceptions mapped to `INVALID_PARAMS` (as `brain_show`'s `_resolve_id` does). With the strict
  lens, the root's tier is read FIRST and a confidential root is refused with `INVALID_PARAMS`
  "document is confidential; pass include_confidential=true", naming no title and no candidate,
  before `compute_related` runs. Then `compute_related(conn, id, limit=…,
  vector_sim_floor=cfg.vector_sim_floor, exclude_confidential=not include_confidential)`.
  Returns `{id, related: [{id, title, vault_path, source, score, snippet}], vector_sim_floor}`.
  The snippet is `compute_related`'s (already cut at `related.SNIPPET_LENGTH`); no truncation flag.
- **`brain_explain(query, limit=10, source=None, tag=None, since_days=None,
  include_confidential=False)`.** `limit < 1` → `INVALID_PARAMS`; above `cfg.search_max_limit`
  (`BRAIN_SEARCH_MAX_LIMIT`, the ceiling `brain_search` already enforces for the same pipeline)
  → `INVALID_PARAMS`. An unknown `source` → `INVALID_PARAMS` (`source_kinds.validate_source_kind`,
  as the CLI validates `--source`). Runs `search_pipeline.run_search(hybrid_search, …,
  explain=True, sensitivity=ctx.confidential_lens(include_confidential))` with the same
  config-derived kwargs `brain explain` passes. Returns `{query, results: [{id, title, source_kind,
  score, content_type, tags, explain}], fallback, fallback_reason, fallback_transient,
  fallback_note, stages}`. Each result is `search_results_json`'s row minus `snippet`, plus
  `explain` from `explanation_json`, the same function `brain explain --json` now uses.
- **Errors.** `psycopg.Error` → `ctx.wrap_db_error` (class-only `INTERNAL_ERROR`). No
  `except EmbedError` on `brain_explain`: `run_search`'s fallback absorbs the query embed failure,
  which shows in the `fallback*` keys. `brain_related` never embeds (it averages stored vectors).

### Decisions

- **RM-12.D1 (ticket):** one module, `mcp_tools/related.py`.
- **RM-12.D2, the root-tier read.** `brain_related` reads the root's tier with
  `queries.fetch_document(...).sensitivity`, the read `brain_show`'s F6 gate already uses.
  Rejected: `brain.ui.queries.document_sensitivity` (the UI route's one-column read). Importing it
  runs `brain/ui/__init__`, which loads `brain.ui.context` and, through it, Typer,
  `brain.doctor_runtime` and `brain.bin.launchd` into the MCP server process (measured: about 60
  modules the server does not otherwise import). Also rejected: a second copy of that SQL in the
  tool module. The cost of `fetch_document` is one row, body included, which is small beside
  `compute_related`'s own work ("roughly one `brain search`", its docstring).
- **RM-12.D3, `brain_explain`'s ceiling.** `cfg.search_max_limit`, the existing
  `BRAIN_SEARCH_MAX_LIMIT` knob, since the tool runs the same pipeline `brain_search` caps with
  it. No new knob, and `config.py` is not touched.
- **RM-12.D4, `brain_explain`'s `source`.** Validated against `source_kinds`, as the CLI's
  `--source` is. `brain_search`'s `none` bucket is not offered: the CLI twin does not offer it,
  and the ticket's parity target is the CLI.
- **RM-12.D5, the error for a confidential root** is `INVALID_PARAMS` (the ticket's wording), not
  a new code: the caller can fix it by passing `include_confidential=true`.

## Tests (TDD, red first)

1. `explanation_json`: `None` → `None`; a `SearchExplanation` → its 14 fields in the CLI's order.
2. `brain_related`: shape; prefix resolves; the result equals `GET /api/notes/{id}/related`'s ids in
   order (AC1); confidential root refused before ranking (a spy on `compute_related` records no
   call) and answered with `include_confidential=true` (AC2); a confidential candidate absent by
   default, present when opted in (AC3); `limit=21` → `INVALID_PARAMS`, `limit=0` →
   `INVALID_PARAMS` (AC5); a bad prefix → `INVALID_PARAMS`; a DB error → class-only
   `INTERNAL_ERROR`.
3. `brain_explain`: per result, `explain` equals `brain explain --json`'s object for the same
   query and corpus (AC4); no `snippet` key; confidential documents absent by default and present
   when opted in; ceiling (AC5); unknown source; the four `fallback*` keys and `stages` present;
   a failed query embed falls back and says so; filters reach the search.
4. Seam carry-forward (aud-68): `test_every_real_tool_module_is_bound_to_the_importable_server`,
   `test_modules_filter_leaves_real_tool_modules_untouched` and
   `test_stdio_tool_list_equals_in_process_list` each assert they see this module's tools, so
   a mutation that breaks registration turns each red (proved in a `git archive` copy).
5. AC6 is the existing registration guard, now covering a real module.
6. AC7: `brain_related` over stdio against a seeded test database.

## Risks

- The lens: pinned by tests 2 and 3, both directions.
- A second root-tier read could drift from the UI's: both read `documents.sensitivity` and apply
  `brain.sensitivity.is_confidential`, and AC2's test pins the refusal.
- The stdio test seeds rows in the shared test DB from the test process while the server process
  reads them: the seed is committed before the server starts, and the `test_db` fixture
  truncates after.
