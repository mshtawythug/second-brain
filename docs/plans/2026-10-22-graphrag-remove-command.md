# `brain graphrag remove`: a targeted repair for lost graph removes

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `feat/graphrag-remove-command`, base `second-brain/master` @ `edaba1b`
(PR #55 merged).

## Problem

When the vault watcher's shutdown times out behind a long graph reconcile
(PR #54, `docs/plans/2026-10-22-vault-watcher-startup-blind.md`), the graph
removes queued behind it are not applied. The shutdown WARNING names their
document ids. Those documents' rows are already gone, and every per-document
relational graph table cascades on the delete, so nothing relational records
the lost remove. The AGE `Document` vertex and its `MENTIONED_IN` edges stay.
The only repair was `brain graphrag build --force`, which, with concepts on,
re-extracts every document through Ollama. This was the handoff's deferred
follow-up 6.

## What was built

- `brain/graph_rag/repair.py` (new) is the core. `parse_document_ids`
  validates the ids. `remove_deleted_documents` checks every id, then calls
  `reconcile.remove_document` (W2 in
  `docs/plans/2026-10-22-graph-entities-deadlock.md`; it takes the tenant graph
  write lock) for each.
- `brain/cli_graphrag_repair.py` (new) holds `brain graphrag remove
  DOCUMENT_ID... [--tenant]`.
- `mcp_server.brain_graphrag_remove(document_ids, tenant=None)` is the MCP
  twin. The other graphrag admin commands (`build`, `refresh`, `aliases
  apply`, `communities build`) have MCP twins, so this one does too.
- `remove_document` now reports what it removed:
  `ReconcileResult.document_vertices_removed` / `graph_rows_removed`.
  `delete_doc_relational` returns its row count.

## Rulings

| # | Ruling | Why |
|---|---|---|
| R1 | Full UUIDs only; a prefix exits 2 (`INVALID_PARAMS`) | A deleted document has no row to resolve a prefix against. |
| R2 | An id whose document still exists exits 1 (`INVALID_PARAMS`), and nothing is removed | The repair is for deleted documents. A live document's graph would only be re-indexed by its next reconcile; to delete it, use `brain rm`. Every id is checked before any is removed. |
| R3 | Idempotent: an id with no graph state is a reported no-op | `remove_document` still recomputes the tenant aggregates and the `CO_OCCURS` mirror, which converges to the same graph. |
| R4 | Counts only | The data flow carries ids and counts, never a title or an entity name. |
| R5 | Exit codes as the other `brain graphrag` admin commands | `2` for a bad argument (`BadParameter`), `1` for a refused run or an AGE-less database (`_require_age_or_exit`), `0` otherwise. |
| R6 | New modules, lazily imported | `cli.py` and `mcp_server.py` are over the 800-line ceiling. Each grows only by its hook (the import plus `register_graphrag_repair(graphrag_app)`) and the MCP wrapper, recorded inline at the growth and in its module docstring. The graph package pulls networkx, so it is imported inside the command (`tests/test_import_hygiene.py`). |

**Not done here: the watcher's WARNING text.** `vault/watch.py`'s shutdown
WARNING still names only `brain graphrag build --force`. It should name
`brain graphrag remove <ids>`. PR #56 is editing `watch.py` and the lane, and no
shared repair-string constant exists outside `watch.py` to put it in. Update
the WARNING once both have landed; this is recorded in the handoff.

**Not done here: the CLAUDE.md size table.** `cli.py`'s and `mcp_server.py`'s
rows end in SHA-bound hops (`885b5a6`, `db1b60e`) and say to enumerate later
commits with `git log`, so they remain true. Adding this change's hop to each
row is an edit to `CLAUDE.md`. That needs the user's say-so, not an agent's, so
it is left for the user.

## Tests

`tests/test_graphrag_remove_command.py` runs against the AGE test instance with
the real `AgeBackend`. AGE counts go through independent raw Cypher.

| claim | test |
|---|---|
| a ghost Document vertex and its edges are removed, and orphaned entities too; a second run is a no-op | `test_remove_clears_a_ghost_document_and_a_second_run_is_a_no_op` |
| several ids, each reported | `test_remove_takes_several_ids_and_reports_each` |
| R1 | `test_remove_needs_full_uuids` |
| R2 | `test_remove_refuses_a_document_that_still_exists` |
| R4 | `test_remove_prints_counts_only` |
| MCP twin: removes, idempotent | `test_mcp_twin_removes_a_ghost_and_is_idempotent` |
| MCP twin: R1 and R2 as `INVALID_PARAMS` | `test_mcp_twin_rejects_a_prefix_and_a_live_document` |

The ghost is built the way the bug leaves it. A document is reconciled into
AGE, then its `documents` row is deleted with no graph remove. The
relational rows cascade away, and the AGE vertex and edges stay.

The parity map (`tests/test_graphrag_cli_mcp_parity.py`), the discovery gate's
WRITE set (`tests/test_mcp_confidential_discovery_gate.py`) and the
`brain-graph` skill list the new tool.

## Results

**RED first.** At `edaba1b` with the new tests, all 7 fail. The CLI tests fail
with "No such command" and the MCP tests with no `brain_graphrag_remove`. The
ghost setup itself passes.

**GREEN.** All 7 pass, 5 runs of 5. The graph, MCP and docs suites pass, run
serially. `ruff check` and `mypy src/` are clean.

**Mutations.** Each ran in a fresh archive copy of `1484f8f`, with its own
venv. The four lock and command files (30 tests) pass unmutated.

| mutation | red |
|---|---|
| M23 skip the live-document check | R2 (CLI and MCP) |
| M24 accept prefixes | R1 (CLI and MCP) |
| M25 `remove_document` drops the vertex count | the ghost, several-ids and MCP-removes tests |
| M26 never call `remove_document` (report a vertex anyway) | the same three: the vertex stays |
| M27 drop the `cli.py` hook | all five CLI tests |
| M3 (existing) `remove_document` without the graph lock | only `test_w2_remove_document_takes_the_lock` |

R4 has no natural mutation, because nothing in the data flow carries a name to
print. Its test is a guard against a future change that adds one.

**Local-only note.** `tests/test_skills_token_routing.py::test_skills_sync_is_not_drifted`
fails on this machine while the branch is unmerged: the repo's
`skills/brain-graph/SKILL.md` now differs from the installed copy. It skips in
CI. Run `bin/brain-skills-sync` after the merge.
