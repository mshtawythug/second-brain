# `graph_entities` deadlock between graph writers: plan and proof

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `fix/graph-entities-deadlock`, base `second-brain/master` @ `94dd1cb`.

## Problem

On 2026-10-22 `brain graphrag build --backfill --concepts` crashed with psycopg
`DeadlockDetected` on an index-tuple insert into `graph_entities`:

```
cli.graphrag_build -> graph_rag/build.py build_graph
  -> reconcile.py reconcile_document -> concepts.py upsert_concept_entities
```

The other side of the deadlock was the vault watcher daemon, whose ingest path
(`ingest_document` -> `GraphSyncer.reconcile`) reconciles a document's graph
entities after each write. A user memory records the same class of failure for
a full-corpus `brain vault relink-derived` against the watcher and `brain-mcp`.
That command calls `GraphSyncer.reconcile` once per document.

## Root cause

Read at `94dd1cb`: `graph_rag/reconcile.py`, `concepts.py`, `aggregates.py`,
`relational.py`, `build.py`, `sync.py`, `aliases/__init__.py`, `cross_type.py`,
`communities.py`, `communities_summary.py`, and migrations 012/013.

Every non-deferred `reconcile_document` (the watcher, MCP, CLI ingest, relink)
does this, in ONE transaction:

1. upsert its person entities, one `INSERT ... ON CONFLICT DO UPDATE` per
   person. An existing row is row-locked; a new row is an uncommitted index
   tuple that a second inserter of the same key must wait on.
2. call the concept extractor, which is an Ollama model call, still inside the
   transaction.
3. upsert its concept entities, again one statement per entity.
4. `_recompute_aggregates`: `UPDATE graph_entities SET doc_count = ...
   WHERE tenant_id = %s`. This row-locks **every entity row in the tenant**, in
   scan order. It then deletes and re-inserts all of the tenant's
   `graph_relationships`, garbage-collects orphan entities, and rewrites the
   tenant's AGE `CO_OCCURS` edges.

The backfill (`build_graph`) runs steps 1-3 per document with
`defer_tenant_refresh=True`, then one `refresh_aggregates` at the end.

Step 4 is the cause. A transaction that runs it must lock every row any
concurrent graph writer holds, while still holding the rows it locked in steps
1 and 3. Any concurrent writer that then needs one of those rows closes a cycle.
The reported crash, reproduced exactly by
`test_backfill_concept_upsert_vs_watcher_recompute_does_not_deadlock`:

| step | backfill (T1, deferred) | watcher (T2, non-deferred) |
|---|---|---|
| 1 | upserts person `alpha` (existing row: locked) | |
| 2 | inside the extractor (model call) | inserts new concept `gamma` (uncommitted) |
| 3 | | step 4 `UPDATE ... doc_count` waits on `alpha` (T1) |
| 4 | inserts `gamma`: waits on T2's index tuple | |

The result is `DeadlockDetected ... while inserting index tuple ... in relation
"graph_entities"` in `upsert_concept_entities`, which is the reported stack. The
same cycle forms between two non-deferred writers, for example the watcher and
`brain-mcp`, or the watcher and `relink-derived`. Each one holds its own entity
row and then sweeps the other's in step 4
(`test_two_full_reconciles_do_not_deadlock_in_the_tenant_recompute`).

Step 2 makes it far likelier. The extractor runs while the row locks from step
1 are held, so the window is a model call wide, not a few milliseconds.

### Why the candidate "sort the keys" fix is not enough

Sorting the upsert keys (candidate 1) orders the per-entity upserts. It does
not order step 4. The tenant-wide `UPDATE` takes the row locks in heap/scan
order, whatever order the upserts used, so it can never agree with every other
writer's key order. The reported interleaving involves ONE concept key, so there
is nothing to sort at all. Candidate 2 (per-document commit) is already true
of the backfill: each `reconcile_document` is its own top-level transaction.
Candidate 4 (retry) treats the symptom and would redo the model call on every
retry.

### Other defects of the same unserialized design, found while reading

These were not reported, but the same fix closes them:

- **Lost mention.** A writer's orphan GC (`_gc_orphan_entities`) cannot see
  another writer's uncommitted mention of an entity. It can delete that entity,
  and the `ON DELETE CASCADE` then removes the other writer's mention when it
  commits.
- **Nothing makes AGE `MERGE` unique under concurrency.** The `entity_uuid`
  property index is a plain btree (`_age_helpers._property_index_ddl`), so two
  writers that MERGE the same vertex at once can each create it. Separately,
  `refresh_cooccur_edges` rewrites the whole tenant's `CO_OCCURS` edges in
  every non-deferred reconcile.

## Ruling: candidate 3, a transaction-scoped advisory lock per tenant

Every transaction that writes the entity graph takes
`pg_advisory_xact_lock(GRAPH_WRITE_LOCK_CLASS, hashtext(tenant_id))` before
its first graph write. The lock is released when the transaction commits or
rolls back. It is defined in the new module `brain/graph_rag/write_lock.py`
(`acquire_graph_write_lock`).

- It is the only fix that orders step 4 against everything else. Graph writes
  in one tenant serialize, so no cycle can form among them. Every graph
  writer's first graph lock is the same lock.
- Writers already waited on each other's rows through step 4. They now wait
  on one lock, before they hold anything, so waiting cannot become a deadlock.
  *(Corrected in the PR #53 review round: this line used to say "It changes no
  outcome". That was false. At `94dd1cb` a second reconcile of the same
  document waited on the first one's row locks. Under the lock alone, with the
  extraction outside it, an older plan could commit over a newer edit: audit
  finding F1. The plan re-check below closes that.)*
- It is per tenant, so tenants do not serialize against each other.
- It is re-entrant within a transaction, so nested writers are fine:
  `merge_aliases` -> `refresh_aggregates`, and `collapse_cross_type_concepts` ->
  `merge_aliases`.
- The two-`int4` key form (`classid`, `objid`) cannot collide with the
  single-`bigint` advisory keys in `db.py` and `maintenance.py`. Postgres keeps
  the two forms apart.
- `acquire_graph_write_lock` refuses to run outside a transaction. On an
  autocommit connection outside one, the lock would be released at the end of
  its own statement, which would be a silent no-op.
- **Long holders.** Only W1 and W2 are per-document. W3, W4, W6 and W8 hold the
  lock for their whole tenant-wide transaction. Every other graph write in the
  tenant waits behind them, the watcher's and `brain-mcp`'s graph sync
  included, and the wait has no timeout. It is a wait, never a deadlock. A
  `brain graphrag refresh` on a large tenant stalls the watcher's graph sync
  for its duration.

**Companion change: plan, then extract, then write.** `reconcile_document`
now runs in three steps. The read phase moved into the new module
`brain/graph_rag/reconcile_plan.py`, which also keeps `reconcile.py` under the
ceiling (797 -> 716 lines).

1. **Plan,** in a short read transaction that commits before step 2 (watermarks,
   persons, the body when the concept aspect is stale).
2. **Extract,** with NO transaction open. The first version of this PR
   (`b998798`) ran the extraction before the lock but inside the open
   transaction. Its comment said the model call "never holds a lock", which was
   false: the reads before it held `AccessShareLock` on `documents` and the
   `graph_*` tables through the model call. That blocks DDL, and migration
   028's 3s `lock_timeout` would fail behind a backfill (PR #53 review, item
   1). It is pinned by `test_extraction_runs_with_no_transaction_or_lock_open`
   (no `pg_locks` row and `state = idle` during the call) and by
   `test_concept_extraction_holds_no_graph_lock`.
3. **Write:** take the lock, then `ReconcilePlan.is_current` re-reads the
   `content_hash`. If the document was edited or deleted since step 1, the
   reconcile returns `skipped` and writes nothing. The newer edit's own
   reconcile, which runs after every write, or the next build indexes it. This
   is audit finding F1, pinned by
   `test_a_stale_reconcile_never_overwrites_a_newer_edit` (adopted from the
   audit's probe) and `test_a_reconcile_whose_document_was_deleted_writes_nothing`.

**No retry (candidate 4).** Graph writers can no longer deadlock with each
other, so a retry would only hide a regression of that invariant. It would also
redo the model call. The only remaining deadlock (Residuals, below) is a
same-document race.

## Closed list of writers

Re-derive with:

```
grep -rn -E "INSERT INTO graph_|UPDATE graph_|DELETE FROM graph_" src/brain
grep -rn -E "\.(clear_tenant|upsert_entities|upsert_mention_edges|refresh_cooccur_edges|detach_delete_entities|detach_delete_documents)\(" src/brain
```

Each transaction that issues one of those statements is listed here, with the
place that takes the lock:

| # | Writer (top-level transaction) | Graph writes | Lock taken |
|---|---|---|---|
| W1 | `reconcile.reconcile_document` | entities, mentions, contributions, index_state, relationships, GC, AGE | first statement of the write transaction (after the read transaction and the extraction) |
| W2 | `reconcile.remove_document` | mentions, contributions, index_state, relationships, GC, AGE | first statement in the transaction |
| W3 | `aggregates.refresh_aggregates` | entities (`doc_count`), relationships, GC, AGE | first statement |
| W4 | `build.build_graph` `force` pre-pass | relationships, GC, AGE `clear_tenant` + restore | first statement |
| W5 | `aliases.apply_aliases`, per-rule transaction | entities, mentions, contributions | first statement |
| W6 | `aliases.merge_aliases`, outer transaction | W5 + AGE + W3 | first statement |
| W7 | `cross_type._restore_winner_display_names` | entities (`name`), AGE | first statement |
| W8 | `communities.build_communities` | `graph_communities`, `graph_community_members` (FK into `graph_entities`) | first statement |
| W9 | `communities_summary._run_summary_phase`, write transaction | `graph_communities` | first statement |
| W10 | `communities_summary._run_embedding_phase`, write transaction | `graph_communities` | first statement |

The per-document `build_graph` loop goes through W1, the end-of-build refresh
through W3, and `collapse_cross_type_concepts` through W6 and W7. Every
`GraphSyncer` call (watcher, MCP, CLI ingest, relink, edit, capture, delete)
goes through W1 or W2. The AGE primitives in the second grep are called only
from inside W1-W7.

W9 and W10 write no entity-graph table. They are in the list because W8
rewrites `graph_communities`. Two concurrent `communities build` runs could
otherwise deadlock one's summary UPDATEs against the other's W8. The rule is
uniform: every graph write transaction takes the lock.

**Deliberately not lock-taking**, each with the reason:

- **The `documents` `ON DELETE CASCADE`** into `graph_entity_mentions`,
  `graph_edge_contributions` and `graph_index_state`, from every document
  delete (`vault/delete.py`, `vault/watch.py`, `_capture_command.py`, the
  ingest content-hash replacement). Each is one autocommit statement that
  touches only the deleted document's rows, and the graph sync runs after it
  commits (W2). Its one deadlock is the residual below. Closing it would need
  every document delete to take the lock, which reaches `vault/watch.py`, out of
  scope here (PR #51 owns that file).
- **DDL:** AGE `bootstrap` (labels and indexes) and
  `db.ensure_embedding_column` on `graph_entities.embedding` /
  `graph_communities.summary_embedding`. These are idempotent catalog changes at
  `brain init` / `reembed` / summary time, not row writers.

## Residuals (recorded, not fixed)

**Metadata-only edits are not re-checked.** `is_current` compares the
`content_hash` only. An edit that changes a document's participants without
touching its body can still be overtaken. An older reconcile's persons, read
before the lock, then commit after the newer edit's. The watermark records the
older person set, so the next build re-indexes it. A full re-plan under the
lock would close this, at the cost of a second resolver pass per document.

**The cross-type collapse window (pre-existing, cosmetic).**
`collapse_cross_type_concepts` runs `merge_aliases` (W6), then
`_restore_winner_display_names` (W7), in two transactions. Another graph
writer can run between them. It sees the winning concept's humanized key as
its `name` for that moment (e.g. `Acmeplatform` for `AcmePlatform`), and an
upsert by that writer can refresh the name, which W7 then restores. Only a
display name is affected, and only until W7 commits. This predates this PR.

**Same-document delete vs. reconcile.**

A document that is **deleted at the same moment it is reconciled** can still
deadlock against the cascade. The reconcile deletes the document's mention rows
and then inserts new ones; the insert's FK check takes `KEY SHARE` on the
`documents` row. If the delete's cascade lands between those two statements, it
waits on the reconcile's mention locks while the reconcile waits on the deleted
`documents` row. The window is between two consecutive statements on one
document. Postgres detects it, and a re-run resumes from the per-document
watermarks. Locking the `documents` row `FOR KEY SHARE` before the graph lock
would order it, but the delete would then wait out a whole reconcile.
Separately, `build_graph` already raises `GraphReconcileError` if a document is
deleted between id enumeration and its reconcile. That is pre-existing and
unchanged here. Both are follow-ups.

## Tests

`tests/test_graphrag_deadlock.py` holds the regressions. Each forces one
interleaving across real connections, using the production DI seams (an
injected `person_resolver` and `extractor`) and, where needed, a gate connection
that holds a row lock. Waits poll `pg_stat_activity` for a lock wait, with a
deadline. Every worker sets `statement_timeout`, so a regression fails in
seconds instead of hanging CI.

| claim | test |
|---|---|
| D1 the reported crash is gone | `test_backfill_concept_upsert_vs_watcher_recompute_does_not_deadlock` |
| D2 two full reconciles serialize instead of deadlocking in step 4 | `test_two_full_reconciles_do_not_deadlock_in_the_tenant_recompute` |
| D3 the extraction holds no graph lock | `test_concept_extraction_holds_no_graph_lock` |
| D4 the extraction holds no lock at all, and no transaction is open | `test_extraction_runs_with_no_transaction_or_lock_open` |
| D5 a stale plan never overwrites a newer edit (audit F1) | `test_a_stale_reconcile_never_overwrites_a_newer_edit` |
| D6 a plan whose document was deleted writes nothing | `test_a_reconcile_whose_document_was_deleted_writes_nothing` |

`tests/test_graphrag_write_lock.py` holds the lock contract. Unit tests cover
the guard and per-tenant scope. One test per writer W1-W10 holds the tenant's
lock on another connection and asserts that the writer fails with
`LockNotAvailable` under a short `lock_timeout`. A control run of the same
writer then succeeds once the lock is released.

## Mutation plan

Mutations are applied only in a `git archive` copy, never in this worktree.

| mutation | expected red |
|---|---|
| base `94dd1cb` (no lock, extraction inside the write section) | D1, D2, D3 |
| `d8baa50` (round one: extraction inside the open transaction, no re-check) | D4, D5, D6 |
| M1 drop the `acquire_graph_write_lock` call in `reconcile_document` | D2, W1 |
| M2 move the extraction into the write transaction, after the lock | D3, D4, D5, D6 |
| M15 move the extraction into the read transaction | D4 |
| M14 drop the `is_current` re-check | D5, D6 |
| M16 make `is_current` treat a deleted document as current | D6 |
| M3-M11 drop the call in each of W2-W10, one at a time | that writer's lock test only |
| M12 key `acquire_graph_write_lock` on a constant instead of the tenant | the per-tenant test |
| M13 remove the not-in-a-transaction guard | the guard test |

D1 is not expected to go red under either single mutation of W1. Either
change alone breaks its cycle: without the lock the backfill holds nothing
during extraction, and without the reorder the watcher parks on the lock before
writing. Its red is the base code.

## Results

### Review round (PR #53, at `a97eb86`)

The reviewer's items 1-4 and audit finding F1 are fixed by `a97eb86`. The
numbers below are for that commit; round one's are kept under them.

**RED first.** `src/` at `d8baa50` with the new tests: D4, D5 and D6 fail, and
the other 16 pass.

- D4: `locks held during the model call: [('relation', 'AccessShareLock'), ...]`.
- D5: "the stale reconcile overwrote the newer edit's graph". This is the
  audit's probe, adopted.
- D6: `ForeignKeyViolation` on `graph_entity_mentions`.

**GREEN.** The two files (19 tests) pass 5 runs of 5. The graph and docs-guard
files (1,246 tests) pass. `ruff check` and `mypy src/` are clean.
`reconcile.py` is 716 lines.

**Mutations.** Each ran in a fresh archive copy of `a97eb86` with its own
venv. Every row of the table above turned exactly its predicted tests red:

- M1: D2 `DeadlockDetected`; W1 `DID NOT RAISE LockNotAvailable`.
- M2: D3 "waited behind a model call"; D5 "the newer reconcile hung"; D6 FK
  violation; D4 `('advisory', 'ExclusiveLock')` among the locks held.
- M15: D4 only (`AccessShareLock`s held).
- M14: D5 "overwrote the newer edit's graph", plus D6.
- M16: D6 only, with `ForeignKeyViolation`.
- M3-M13: the same reds as in round one.

### Round one (at `b998798`)

Measured on 2026-10-22 against the AGE test instance (port 5434), in a sibling
database. The fix is commit `b998798`.

**RED on the base.** Base `src/` at `94dd1cb` with the new tests on top: all
three regressions fail, 5 runs of 5.

- D1 fails with `DeadlockDetected ... while inserting index tuple ... in
  relation "graph_entities"`, with the backfill as the victim. That is the
  reported crash.
- D2 fails with `DeadlockDetected ... while updating tuple`.
- D3 fails with `LockNotAvailable ... while inserting index tuple`: the
  concurrent writer waited behind the extraction.

**GREEN on the fix.** Both new files (16 tests) pass 5 runs of 5. The graph
test files (`tests/test_graphrag_*.py`, `tests/test_graph_*.py`,
`tests/test_cli_graph*.py`, `tests/test_mcp_graphrag.py`: 1,128 tests) pass.
`ruff check` and `mypy src/` are clean.

**Mutations.** Each ran in a fresh `git archive` copy of `b998798`, with its
own editable venv, never in the worktree. The harness checks that each
replacement matches exactly once, that the file changed, and that `brain`
imports from the copy.

| mutation | predicted red | observed red | failing assertion |
|---|---|---|---|
| none | none | none (16 passed) | |
| M1 no lock in `reconcile_document` | D2, W1 | D2, W1 | `DeadlockDetected`; `DID NOT RAISE LockNotAvailable` |
| M2 extraction after the person writes | D3 | D3 | "a graph write waited behind a model call: LockNotAvailable" |
| M3-M11 no lock in W2-W10, one at a time | that writer only | that writer only, each | `DID NOT RAISE LockNotAvailable`; W10: "the embedding write did not wait on the graph lock" |
| M12 constant key | key test, other-tenant test | the same two | key mismatch in `pg_locks`; `LockNotAvailable` across tenants |
| M13 no transaction guard | guard test | guard test | `DID NOT RAISE GraphReconcileError` |

As the mutation plan predicted, D1 stays green under M1 and under M2 alone. Its
red is the base, where both are reverted.
