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
| R2 | If any id's document still exists, the batch exits 1 (`INVALID_PARAMS`), and nothing is removed | The repair is for deleted documents. A live document's graph would only be re-indexed by its next reconcile; to delete it, use `brain rm`. **Checked twice** (review round): a pre-check fails fast, and a re-check runs under the tenant graph write lock in the removal's own transaction. The re-check catches a document restored in between, e.g. a vault file put back with its frontmatter id. |
| R3 | Idempotent: an id whose vertex is already gone is a reported no-op | The batch still recomputes the tenant aggregates and the `CO_OCCURS` mirror, which converges to the same graph. |
| R7 | **One batch, one transaction** (review round) | `reconcile.remove_documents` deletes every id's relational rows, then runs the tenant-wide steps once: aggregate recompute, orphan GC, `CO_OCCURS` rebuild. It also counts each id's `Document` vertex. `remove_document` is its one-id wrapper, so W2's lock discipline is unchanged. The first version called `remove_document` per id and paid the tenant-wide pass N times. A failure now rolls the whole batch back. |
| R8 | **Report the AGE vertex only; `graph_rows_removed` dropped** (review round) | A deleted document cannot keep relational graph rows: every per-document graph table cascades on the `documents` delete, by FK. So the count the first version reported was provably always 0, and it would never have worked as a race signal. Dropped, with `delete_doc_relational` back to returning `None`. `had_graph_state` became `had_document_vertex`, the only state a deleted document can still have. |
| R4 | Counts only | The data flow carries ids and counts, never a title or an entity name. |
| R5 | Exit codes as the other `brain graphrag` admin commands | `2` for a bad argument (`BadParameter`), `1` for a refused run or an AGE-less database (`_require_age_or_exit`), `0` otherwise. |
| R6 | New modules, lazily imported | `cli.py` and `mcp_server.py` are over the 800-line ceiling. Each grows only by its hook (the import plus `register_graphrag_repair(graphrag_app)`) and the MCP wrapper, recorded inline at the growth and in its module docstring. The graph package pulls networkx, so it is imported inside the command (`tests/test_import_hygiene.py`). |

**The watcher's repair text (review round).** PR #56 had put the shared
constant `LOST_REMOVES_REPAIR` in `vault/graph_lane.py`. Both the watcher's
shutdown WARNING (`watch.py`) and the lane's no-connection ERROR
(`graph_lane.py`) log it after the ids. It now leads with
`brain graphrag remove <ids>` and keeps `brain graphrag build --force` as the
fallback. Only the constant changed; `watch.py` itself did not. The W4 test
(`tests/test_vault_watch_startup_blind.py`) pins both commands in the WARNING.
`test_the_watcher_repair_text_names_this_command_first` pins the order, and
that the command it names is registered.

**The CLAUDE.md size table.** The first version of this plan left the rows for
the user. The coordinator added them in `11908fe`: `cli.py` 8,816 at `1484f8f`,
and `mcp_server.py` 5,047 at `1484f8f`, each with a growth note. The `cli.py`
row was resolved as two sibling legs in the master merge `d6b38dc` (master's
8,802 at `155e61d`, and this PR's 8,816 at `1484f8f`, joining at the merge). The
review round changes `mcp_server.py`'s count again (the tool docstring and the
`INTERNAL_ERROR` mapping). That hop goes to the coordinator to apply, and
`cli.py` is unchanged by it.

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
| R2 batch: [ghost, live] exits 1 and the ghost keeps its vertex (CLI) | `test_a_live_id_refuses_the_whole_batch_and_touches_no_ghost` |
| R2 batch, MCP: a live id with graph state, both untouched | `test_mcp_twin_refuses_a_batch_with_a_live_id` |
| R2 re-check under the lock: a row restored after the pre-check is refused | `test_a_document_restored_after_the_pre_check_is_still_refused` |
| R7: a batch pays the tenant-wide steps once | `test_a_batch_pays_the_tenant_wide_steps_once` |
| R1: an empty MCP list is `INVALID_PARAMS` | `test_mcp_twin_rejects_an_empty_list` |
| R1: one UUID in two spellings is one id | `test_the_same_uuid_spelled_twice_is_one_id` |
| the watcher's repair text leads with this command, which exists | `test_the_watcher_repair_text_names_this_command_first`, plus W4 in `tests/test_vault_watch_startup_blind.py` |

The ghost is built the way the bug leaves it. A document is reconciled into
AGE, then its `documents` row is deleted with no graph remove. The
relational rows cascade away, and the AGE vertex and edges stay.

The parity map (`tests/test_graphrag_cli_mcp_parity.py`), the discovery gate's
WRITE set (`tests/test_mcp_confidential_discovery_gate.py`) and the
`brain-graph` skill list the new tool.

## Results

### Review round (at `42e928e`, on top of the master merge `d6b38dc`)

**RED first,** with `src/` at `d6b38dc` and the review round's tests, 8 fail:
- **the re-check:** the restored document's vertex was removed, with no refusal;
- **batch cost:** two `CO_OCCURS` rebuilds for two ids;
- **the repair text:** the old `LOST_REMOVES_REPAIR` named only `build --force` (the pin and W4);
- **the new report shape:** four tests on its wording and keys.

The re-check test's first version went red for the wrong reason, a Cypher
call on a connection that had not run `LOAD 'age'`. Its remover connection
now loads AGE, as `connect_age` does. It then fails on the missing refusal
itself.

**GREEN.** The remove tests (14) pass 5 runs of 5. `repair.py` and
`cli_graphrag_repair.py` are at 100% branch coverage. The graph, MCP, docs,
GraphLane, startup-blind and watcher suites pass, run serially. `ruff check`
and `mypy src/` are clean.

**Mutations.** Each ran in a fresh archive copy of `42e928e`. The five
lock/command/watcher files (43 tests) pass unmutated.

| mutation | red |
|---|---|
| M23 drop the batch pre-check | none: an **equivalent mutant**. The re-check under the lock refuses the same ids; the pre-check only fails fast, before taking the lock. |
| M28 drop the re-check under the lock | the restored-row test |
| M29 the per-id check inside the loop, still in one transaction | the batch-cost test only. Inside one transaction a refusal rolls back the ids already removed, so [ghost, live] holds by atomicity. |
| M32 per-id checks AND per-id transactions (the reviewer's surviving mutant) | both [ghost, live] tests (CLI and MCP), the restored-row test and the batch-cost test |
| M30 one transaction per id | the batch-cost test |
| M24 accept prefixes | R1 (CLI, MCP, and the dedupe test) |
| M25 report 0 vertices | the ghost, several-ids, MCP-removes, dedupe and batch-cost tests |
| M26 never DETACH DELETE the vertices | the four tests that check the vertex is gone |
| M27 drop the `cli.py` hook | all eight CLI tests, and the repair-text pin |
| M31 revert the repair text's lead | the repair-text pin and W4 |
| M3 `remove_documents` without the graph lock | only `test_w2_remove_document_takes_the_lock` |

The MCP `GraphReconcileError -> INTERNAL_ERROR` mapping is defensive. No path
in the batch raises a `GraphReconcileError` other than `DocumentStillExists`,
so it is unmutated and uncovered by design.

### Round one (at `1484f8f`)

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
