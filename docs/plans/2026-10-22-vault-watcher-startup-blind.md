# Vault watcher: no blind hours after a restart: plan and proof

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `fix/vault-watcher-startup-blind`, base `second-brain/master` @ `8ad72f6`
(PR #52 merged).

## Problem (production, found by ops on 2026-10-22)

After a 15:24 restart, `run_watcher` was still inside its startup `sync_vault`
80+ minutes later. It was in step 7, the graph pass:

```python
for doc_id in sorted(seen_doc_ids):
    graph_syncer.reconcile(conn, doc_id)
```

That day's ~45 new Krisp documents had no concepts watermark, so each one's
reconcile ran Ollama concept extraction, at about 8-10 minutes per document.
The observer and the worker only start once the startup sync returns, so vault
edits went unwatched for about six hours.

## Options weighed, and the ruling

The constraints come from the code:

- **The startup FILE reconcile must come before the observer.**
  `tests/test_vault_watcher.py::test_startup_sync_runs_before_observer_starts`
  pins it deliberately. Starting the observer first would also turn the startup
  sync's own disk writes (id write-back, fence rewrites) into a flood of watch
  events.
- **The worker is one serial queue.** Anything queued on it runs between live
  file jobs. One 10-minute job blocks every live edit behind it.
- **Graph writes must not gain in-process concurrency.** Today the worker is the
  watcher's only graph writer. Two concurrent in-process writers would be the
  `graph_entities` deadlock class that PR #53 (under review) addresses with a
  per-tenant advisory lock. This design must not depend on #53 landing.
- **`GraphSyncer` reads the document from the DB at reconcile time.** So a
  reconcile that runs late still reflects the latest committed content.

| Option | Verdict | Why |
|---|---|---|
| (a) start the observer and worker first, run the startup sync in the background | rejected | It breaks the pinned file-reconcile-first order. A background file sync also races the worker's `sync_one_file` on the same files, over two connections. |
| (b) skip the expensive reconcile at startup and enqueue it as WORKER jobs | rejected in that form | The worker is serial, so a live edit waits behind each 8-10 minute reconcile. The watcher would still be functionally blind. |
| (c) split `sync_vault` so the graph pass runs after the observer starts, on the main thread | rejected in that form | The worker's live `sync_one_file` also reconciles the graph. That gives two in-process graph writers, the deadlock class above. |
| **(b) + (c) through one GRAPH LANE** | **chosen** | Every graph write the watcher makes (the startup step 7 AND the live syncs) goes through ONE background thread with its own connection. |

**What the chosen design gives:**

- The startup sync's step 7 only enqueues, which is instant. The watcher starts
  observing as soon as the file reconcile is done.
- The worker never blocks on Ollama.
- There is still exactly one graph writer per watcher process, as today.
- `sync_vault` and the `brain vault sync` CLI keep their synchronous behaviour.
  The CLI passes the real `GraphSyncer`; only the watcher passes the lane.

## Design: `brain/vault/graph_lane.py` (new module)

- **`GraphSyncTarget`**, a `typing.Protocol` with `reconcile(conn, document_id)`
  and `remove(conn, document_id)`. `GraphSyncer` satisfies it structurally.
  `sync.py` and `watch.py` type their `graph_syncer` parameter with it, a
  type-only change. No `graph_rag` file is edited.
- **`GraphLane(syncer, conn_factory)`** satisfies `GraphSyncTarget`.
  - `reconcile` and `remove` IGNORE the caller's connection. They append the
    request to a FIFO and return at once.
  - One thread, `brain-vault-watcher-graph`, opens its own connection
    (autocommit, like the worker's) and runs each request through the real
    syncer, in order.
- **Dedupe, on the LAST pending request for the document.** A `reconcile` is
  dropped only when the document's last pending request is already a
  reconcile. That one will read the latest DB state when it runs.
  - Checking for ANY pending reconcile would be unsafe. For reconcile b, remove
    b, reconcile b, the final reconcile would fold into the one BEFORE the
    remove, and the document would end up removed (L3).
  - A request for a document whose reconcile is IN FLIGHT is queued again: the
    in-flight one has already left the queue. So an edit landing mid-reconcile
    is still reconciled afterwards.
  - `remove` is never deduped, and keeps its FIFO position relative to
    reconciles.
- **One failing request never stops the lane.** Each request runs in its own
  `try`. `GraphSyncer` never raises, but the lane does not rely on that (L7).
- **Shutdown (`close(timeout)`)**, called by `run_watcher` after the worker has
  joined, so every live request is already in:
  - pending `reconcile`s are DROPPED. The next startup's step 7 reconciles every
    walked document again, so nothing is lost; the graph only lags until the
    next start.
  - pending `remove`s are DRAINED. They are cheap, and the next startup only
    removes graph rows for pruned documents, so a dropped remove would leave
    stale graph rows.
  - requests after `close` are ignored.
  - an in-flight reconcile is allowed to finish. `close` waits up to `timeout`
    and returns whether the thread exited. If it did not, `run_watcher` logs and
    leaves the daemon thread. That is the same trade-off the worker's 10s join
    already makes, and an aborted transaction rolls back.

### Same-path ordering: a file edited while its startup reconcile is pending

- **File content is never deferred.** The live edit is synced by the worker
  (unchanged). Search sees it at once.
- **Graph reconcile for that document:**
  - **pending (not started):** the live request is deduped into it. When it
    runs, it reads the edited content.
  - **in flight:** the live request is queued again, and runs after it, reading
    the edited content.
  - Either way, the last reconcile of the document happens after its last edit
    committed.
- **Delete then recreate:** a `remove` keeps its FIFO position, so remove
  happens before reconcile. A reconcile of a deleted document is the same
  never-raise no-op it is today.

## Tests (TDD, RED first at `8ad72f6`)

The lane unit tests (L1-L7) are in `tests/test_vault_graph_lane.py`, with no DB.
The watcher tests (W1, W2a, W2b) are in `tests/test_vault_watch_startup_blind.py`,
which imports nothing from the new module, so they can run RED at `8ad72f6`.
They are DB-backed, and use an
injected slow syncer (a `GraphSyncTarget` whose `reconcile` blocks on an
`Event`) and the existing fake observer.

| Id | Test | Claim |
|---|---|---|
| W1 | while the startup reconcile is blocked, the observer starts, and an injected event's file is synced to the DB. After shutdown, no lane thread is alive | the watcher is not blind during a slow startup reconcile (RED at `8ad72f6`), and `run_watcher` closes its lane |
| W2a | X's reconcile is IN FLIGHT when X is edited: X is reconciled again afterwards and sees the edit, and every reconcile ran on the lane thread | same-path ordering; the worker never runs graph syncs |
| W2b | X's reconcile is still PENDING when X is edited: X is reconciled exactly once, on the edited content | same-path ordering, deduped |
| L1 | lane: a reconcile for a pending document is deduped; FIFO order is kept | dedupe |
| L2 | lane: a reconcile requested while that document's reconcile is in flight runs again afterwards | no lost edit |
| L3 | lane: `remove` is never deduped and keeps its FIFO position | ordering |
| L4 | lane: `close` drops pending reconciles, drains pending removes, and ignores later requests | shutdown |
| L5 | lane: `close(timeout)` returns False while a reconcile is in flight, and the thread then finishes and exits | bounded shutdown |
| L6 | lane: the syncer is called with the LANE's connection (autocommit), not the caller's | its own connection, never the worker's |
| L7 | lane: a request that raises is logged, and the next request still runs | one failure never stops the lane |

## Ceiling

`watch.py` and `sync.py` are both in CLAUDE.md's over-800 table.

- **`watch.py`:** gains the lane wiring. The lane itself lives in the new module.
  The row's descriptive hop (the PR #52 move-identity commits) is promoted to
  SHA-bound hops, a new descriptive hop is added, and the growth is recorded
  inline and in the module docstring.
- **`sync.py`:** gets a type-only change (`GraphSyncer` becomes
  `GraphSyncTarget` on two parameters, plus its import). It is net-zero or close
  to it. Its row is checked and promoted per the rules.

## Risks

- **The graph lags while the backlog runs.** It is best-effort and recomputable
  (`brain graphrag build`), and file content and search are never deferred.
- **New in-process concurrency: document writes (worker) alongside graph writes
  (lane).** The graph mention tables reference `documents ON DELETE CASCADE`, so
  a worker delete and a lane reconcile of the same document can lock-wait on
  each other, though not in a cycle. This is the same pattern that already
  exists between processes (MCP or ingest writing documents while the watcher
  reconciles). There is still only one graph WRITER per watcher process.
- **An in-flight reconcile at shutdown** can outlive `run_watcher` as a daemon
  thread. The process exit aborts it, and the transaction rolls back.

## Proof

### TDD: RED at `8ad72f6`, then GREEN

The watcher tests (W1, W2a, W2b) import nothing from the new module. They ran
against the unmodified `8ad72f6` code, and again in its `git archive` copy with
their final versions dropped in. All three went RED at the production symptom:
"the observer did not start while the startup graph reconcile was running".
With the lane, all three pass, and so do L1-L7.

### What the first mutation run found (all test defects, all fixed)

The first run (snapshot `a5dc18b`) failed 7 of 43 mutants. Not one of them
showed a production defect; every failure was a test-design defect:

1. **L2 and L3 waited for a call COUNT.** A mutant that changed the count made
   them time out in the backstop instead of failing at their targeted
   assertion (G5, G7). Every lane test now asserts after a FIFO barrier: a
   `remove`, which is never deduped, waited on before the full-list
   assertion.
2. **W1's "lane closed" check counted lane threads left alive by OTHER failing
   tests in the same session** (G5, G7). It now compares against a snapshot
   taken before its own watcher starts. The unit tests close their lanes in a
   `finally`.
3. **W2b raced.**
   - It released the syncer once the edit's DB commit was visible. But the
     worker submits its lane request only after `sync_one_file` returns, so X
     could run twice. That made W2b red under G9, G10, MG and MH, which are
     unrelated mutants.
   - It also snapshotted before the lane had drained, so G2 survived.
   - It now waits for the worker's `processed` count to advance, then
     barriers the lane through `_WatcherState.graph_lane` (the one seam this
     needed, exposed the way tests already read other watcher state).

### Mutation matrix (five clauses), in a `git archive` copy only

The run of record used snapshot `26d20e1`. The judge is the strict one: it
matches the JUnit failure message only, and the set of failing tests must
equal the prediction. The live worktree was never mutated.

**All 43 mutants OK:** G1-G12 new, plus all 31 from PRs #49, #51 and #52
re-run, with no new flakes. The control run is clean, both negative controls
are rejected, and the archive was restored byte-identical.

| Mutant | Edit | Failing tests (exactly), with the message |
|---|---|---|
| G1 | the startup sync gets the real syncer, not the lane | W1, W2a, W2b "the observer did not start while the startup graph reconcile was running" |
| G2 | the worker gets the real syncer | W2a "a graph reconcile ran off the lane thread"; W2b "X's pending reconcile was not deduped onto the edited content" |
| G3 | the lane is never started | W1, W2a, W2b "the startup graph reconcile never ran" |
| G4 | `run_watcher` never closes the lane | W1 "run_watcher returned with its graph lane still running" |
| G5 | dedupe on ANY pending reconcile, ignoring removes | L3 "a remove was deduped or reordered" |
| G6 | no dedupe | L1 "a pending reconcile was not deduped"; W2b (twice) |
| G7 | the in-flight request still counts as pending (peek, pop after) | L2 "a reconcile requested while in flight was dropped"; L4; W2a "X was not reconciled again after its in-flight reconcile" |
| G8 | `close` keeps pending reconciles | L4 |
| G9 | `close` drops pending removes too | L4 |
| G10 | requests are accepted after `close` | L4 |
| G11 | the lane's connection is not autocommit | L6 |
| G12 | one failing request stops the lane | L7 "the lane stopped after one failing request" |

**Each claim has its own killing mutant (clause e):**

- "the watcher is live during a slow startup reconcile": G1 / G3 → W1;
- "`run_watcher` closes its lane": G4 → W1;
- "the worker never runs graph syncs": G2 → W2a;
- "same-path ordering, in flight": G7 → W2a, L2;
- "same-path ordering, pending, deduped": G6 → W2b, L1;
- "remove keeps its place": G5 → L3;
- "shutdown semantics": G8 / G9 / G10 → L4;
- "its own autocommit connection": G11 → L6;
- "one failure never stops the lane": G12 → L7.

### Stability and suites

- The new tests passed 5 runs in a row: 10 passed each, at load average 23-31.
- Run once, serially: the watcher, sync, graph-wiring and docs-guard files gave
  438 passed.
- The full suite was not run locally; CI is the full-suite evidence.
- `ruff check` and `mypy src/` are clean. The new files are format-clean, and
  so are the lines this change added to `watch.py` and `sync.py`.

### Ceiling

- **`watch.py`:** 1,167 (`4762b42`, the last commit to touch it before
  `8ad72f6`) to 1,184 (+17). That is the wiring plus the `_WatcherState.graph_lane`
  field. The lane itself lives in the new leaf `graph_lane.py` (125 lines).
- **`sync.py`:** 1,817, net-zero (type annotations only).
- Both CLAUDE.md rows are promoted, with descriptive hops for this commit.
