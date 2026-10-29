# RM-09 — One search pipeline for every user-facing surface

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-09 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(wave 2; G4; eval policy in §2.7). The ticket is the spec; this is the task list.
**Base:** `second-brain/master` `4e08dc6` (RM-01..RM-08 merged).

## Goal

Every surface a person or agent searches through composes the same stages in the same order, so
a later stage (the reranker RM-14a, the document leg RM-20, engagement RM-22b, the rewrite
RM-54) is wired once, in `search_pipeline.build_stages`, and reaches all six surfaces. With no
stage configured (the only state this ticket ships), every surface's output is unchanged, apart
from one new always-present key, `stages: []`, in the `brain search --json --meta` envelope and
in MCP `brain_search`.

## Files

| File | Change |
|---|---|
| `src/brain/search_pipeline.py` (new) | `SearchStage` Protocol, `StageReport`, `StageOutcome`, `PipelineResult`, `apply_stages`, `run_search`, `build_stages` (returns `()`), `stage_reports_json`, and `pipeline_json` (the four `fallback*` keys then `stages`, which MCP splats). |
| `src/brain/cli_search.py` | `search` and `explain` call `run_search` in place of `search_with_fts_fallback`; the envelope gets the stage reports. Net zero. |
| `src/brain/format_search.py` | `search_envelope_json` gains a keyword-only `stages` (default `()`) and emits `**stage_reports_json(stages)` after the four `fallback*` keys. |
| `src/brain/mcp_server.py` | `brain_search` swaps `search_with_fts_fallback` for `run_search` (passing its minted `session_id`) and returns the `stages` key. Net zero, recorded in the ceiling table. |
| `src/brain/ui/routes_search.py` | The route calls `run_search` with `limit=fetch_limit` and its `session_id`; the paging line below. No new response key. |
| `src/brain/recall.py` | `recall.recall` calls `run_search`. |
| `src/brain/ask.py` | `_retrieve_hybrid` calls `run_search(…, fallback=fallback)`; the direct `hybrid_search` call on the `fallback=False` branch goes. |
| `tests/test_search_pipeline.py` (new) | Pure tests: `run_search` / `apply_stages` / `stage_reports_json` with a fake `search_fn` and fake stages. AC3. |
| `tests/test_search_pipeline_surfaces.py` (new) | AC2: one test per surface that a reordering stage is applied; AC3 on one surface end to end; AC5 for the UI route; the `stages` key on the envelope and MCP. |
| `tests/test_search_pipeline_boundary.py` (new) | The AST guard and its synthetic `tmp_path` modules (AC4). |
| `tests/test_search_output_unchanged.py`, `tests/test_cli_search_meta.py` | The two exact-key-set assertions gain the `stages` key, derived from `stage_reports_json`. Both files say a key may be added deliberately but not silently; this is that edit. Nothing else in either file changes. |
| `CLAUDE.md` | `mcp_server.py` row: a SHA-bound, net-zero RM-09 hop. |
| `docs/cli-reference.md`, `docs/guides/claude-desktop-setup.md` | The `stages` key beside the `fallback*` keys. |
| `CHANGELOG.md` | Unreleased entry. |
| Spec Appendix B | Follow-up commit once the PR number is known: RM-09 → `DONE (#PR)`. |

`search.py` is not touched. `eval/runner.py`, `graph_rag/fuse.py` and `demo/__init__.py` are out
of scope and keep calling `hybrid_search` directly.

## Design (as the ticket states it, with the choices it leaves open)

- `run_search(search_fn, conn, *, embedder, query, limit, session_id=None, fallback=True,
  stages=None, **kwargs)` asks `search_fn` for
  `max(limit, *(s.candidate_limit(limit) for s in stages))` rows, through
  `search_with_fts_fallback` when `fallback` is true and straight through `search_fn` otherwise,
  runs `apply_stages`, and truncates to `limit`. `limit` is a named parameter, so it cannot also
  arrive in `kwargs` (Python refuses the call).
- `apply_stages` runs the stages in order. It names each report after the stage and measures its
  `ms` itself, so every report is timed the same way. A stage that returns `applied=False` has its
  input passed on unchanged even if it returned other rows; a stage that raises is reported as
  `applied=False` with `skipped_reason=describe_failure(exc)` (class only), and logged at INFO,
  for the reason `search_fallback` gives (the CLI configures no logging, so a WARNING would print
  a second, unformatted notice).
- `stage_reports_json(reports)` returns `{"stages": [{name, applied, skipped_reason, ms}, …]}`,
  always present, so it is splatted like `fallback_json`.
- Session ids: MCP, the UI and `ask` pass the session id they already mint (`ask_no_loop` and
  `ask` thread theirs through `_retrieve` and `_retrieve_hybrid`, review round 1); the CLI and
  `recall` have none and pass `None`.
- A stage whose `candidate_limit` raises contributes `limit` to the fetch, is **not applied** (a
  stage that cannot say how many rows it needs is not run on a guess), and is reported as skipped
  with its class, in its position, through the same `apply_stages` path as a stage whose `apply`
  raises (review round 1).
- `pipeline_json(result)` is `{**fallback_json(result.fallback), **stage_reports_json(result.stages)}`.
  MCP `brain_search` returns it in place of `fallback_json`, which is what keeps `mcp_server.py`'s
  import and return lines changing in place (net zero).

## Deviations from the ticket

The spec text is left as written (§0); these are recorded here and in the PR body.

1. **`run_search`'s `stages` defaults to `None`, meaning `build_stages(os.environ)`**, where the
   ticket's signature writes `stages: Sequence[SearchStage] = ()`. With `()` as the default every
   surface must remember to pass `stages=build_stages(os.environ)`, and a surface that forgets
   silently skips every stage: the defect this ticket exists to remove. RM-14a's text ("`build_stages`
   adds `RerankStage` … so every surface on the pipeline gets it") needs the surfaces to already
   receive `build_stages`' output. A caller that wants no stages passes `stages=()`; the pure tests
   do. `os.environ` is read only after the surface's `Config.load()`, as §2.3 requires.
2. **`ask._retrieve_hybrid(fallback=False)` goes through `run_search(fallback=False)`** rather than
   calling `hybrid_search` directly, so `brain eval --answer` sees the same stages as `brain ask`.
   It still cannot fall back. §2.7's "never calls `run_search`" binds `run_eval` (the retrieval
   eval), which this ticket does not touch.
3. **Two frozen exact-key tests are edited** (see Files). Acceptance criterion 1 says
   `tests/test_search_output_unchanged.py` "stays green"; it cannot without the edit, because its
   MCP envelope test pins the exact key set and says so. The edit derives the new key from its
   helper, as the fallback keys were derived in RM-01..03.

## The flapping-embedder paging note (carried forward from RM-03)

Each `GET /api/search` request tries the query embed afresh (RM-03.D1), so with an embedder that
comes and goes, page 1 can be text-only-ranked and page 2 hybrid-ranked, and a row can repeat or
be skipped across the two pages. **This ticket documents it rather than adding a per-session
sticky fallback**: the UI mints a fresh `session_id` per request, so there is no session to stick
to without new server-side state, and every page's response already carries its own `fallback`
key, so the Notes tab says on each page which ranking it shows. The line is in
`ui/routes_search.py`'s module docstring and in `docs/cli-reference.md` (`brain ui`).

## Tests (RED first)

1. `test_search_pipeline.py` (pure): zero stages returns `search_fn`'s rows unchanged and asks for
   exactly `limit`; a stage's `candidate_limit` raises the fetch and the result is truncated to
   `limit`; stages run in order; a raising stage is skipped with a class-only reason and the
   unstaged rows; `applied=False` passes the input on; reports carry measured `ms`; `fallback=True`
   on an `EmbedError` retries text-only and still applies the stages; `fallback=False` lets the
   `EmbedError` propagate; `stage_reports_json` shape; `build_stages({})` is `()`; `stages=None`
   reads `build_stages`.
2. `test_search_pipeline_surfaces.py` (5434 DB): a reversing stage installed through
   `build_stages` reorders `brain search --json`, `brain explain --json`, MCP `brain_search`,
   `GET /api/search`, `recall.recall` and `ask._retrieve_hybrid`; a raising stage on `brain search
   --json --meta` reports `applied: false` with the class and leaves the rows as without it; the UI
   route hands `search_fn` exactly one `limit`, equal to `fetch_limit`; the `stages` key is `[]` in
   the envelope and MCP and absent from the bare list and the UI response.
3. `test_search_pipeline_boundary.py`: the real tree passes; a synthetic module calling
   `hybrid_search(…)`, `brain.search.hybrid_search(…)`, `search_with_fts_fallback(…)` or either
   through a `from … import … as` alias fails; one that only imports or passes it passes, and so
   does a self-alias (`hybrid_search as hybrid_search`, as `cli.py` re-exports it); the
   `cli_search.hybrid_search` shim is the one function-level exemption.

## Mutation proofs (in a `git archive` copy only)

Run on a `git archive 8017f0b` copy in a scratch directory outside the repo, against its own
sibling database (`second_brain_test_rm09m`), with `PYTHONDONTWRITEBYTECODE=1`, `__pycache__`
purged before and after each run, `PYTHONPATH` pinned to the copy's `src`, and the import checked
by realpath to resolve inside the copy. Each mutation was proven applied (the file's SHA changed and
its text equals the exact replacement), run against the named test files, then restored and
checked byte-identical. The restored copy ran green (59 passed).

| # | Mutation | Result |
|---|---|---|
| M1 | `run_search` skips `apply_stages` | RED, 15 failed |
| M2 | `apply_stages` catches only `ZeroDivisionError` (a stage may raise) | RED, 3 failed |
| M3 | a declined stage's rows are used | RED, 1 failed |
| M4 | no truncation to `limit` | RED, 2 failed |
| M5 | `candidate_limit` ignored | RED, 2 failed |
| M6 | `stages=None` does not read `build_stages` | RED, 10 failed |
| M7 | `ask.py` calls `hybrid_search` directly again | RED, 1 failed (the boundary guard) |
| M8 | the guard skips every module | RED, 5 failed |
| M9 | the guard misses attribute calls | RED, 2 failed |
| M10 | the shim exemption covers the whole of `cli_search.py` | RED, 1 failed |
| M11 | MCP `brain_search` drops `session_id` | RED, 1 failed |
| M12 | the UI route passes `stages=()` | RED, 2 failed |
| M13 | the envelope drops `stages` | RED, 3 failed |
| M14 | `recall` passes `stages=()` | RED, 1 failed |
| M15 | a skipped reason carries the exception's message | RED, 3 failed |

## Risks

- **An output-shape drift on a frozen contract.** The bare `--json` list, `explain --json` and the
  UI response are untouched; only the opt-in envelope and MCP gain `stages`.
- **`mcp_server.py` growth.** Kept net zero by the swap; the ceiling-table hop records it.

## Backlog

G4 is new in the spec (§1.2's row "Ranking stages wired per surface" has no backlog id), so the
backlog doc has no row to close, as with RM-08's G3.
