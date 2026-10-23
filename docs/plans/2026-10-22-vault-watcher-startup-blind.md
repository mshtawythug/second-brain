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
  - pending `remove`s are drained, **but only if the in-flight reconcile
    finishes within the timeout.** They are cheap, and the next startup does
    NOT redo them: it only removes graph rows for pruned documents. So when
    `close` times out, the removes still queued are not applied.
    `GraphLane.undrained_removes()` names them, and `run_watcher` logs a
    WARNING with their count, their document ids and the real repair options.
    These were verified against `cli.graphrag_build`, `cli.graphrag_refresh`
    and `build.build_graph`:
    - **The ghost AGE `Document` vertex** (and its `MENTIONED_IN` edges) stays
      until `brain graphrag build --force`. That clears the tenant's AGE mirror
      and rebuilds it, and with concepts on it re-extracts every document.
    - **Aggregates and orphan entities** heal at the next non-skipped graph
      reconcile, for that reconcile's aspects, or with `brain graphrag
      refresh`.
    - **Neither of these works:** a bare `brain graphrag build` only prints
      usage, and `--backfill` skips the deleted document. In exactly this PR's scenario (an 8-10 minute
    extraction in flight), a routine launchd restart or reinstall hits this
    case. That made it the review's LOW-MEDIUM finding.
  - requests after `close` are ignored.
  - an in-flight reconcile is allowed to finish. `close` waits up to
    `_LANE_CLOSE_TIMEOUT_S` (10s) and returns whether the thread exited. If it
    did not, `run_watcher` logs and leaves the daemon thread. That is the same
    trade-off the worker's 10s join already makes, and an aborted transaction
    rolls back.
  - an IDLE lane closes at once: `close` notifies the waiting thread (L10, W1).

**Ruling: no startup self-heal for a lost remove** (the review's preferred
option, evaluated and declined).

- **The cheap query does not exist.** Every per-document relational graph table
  (`graph_entity_mentions`, `graph_edge_contributions`, `graph_index_state`) is
  `REFERENCES documents(id) ON DELETE CASCADE`. Once a document row is gone,
  its relational graph rows went with it, and nothing relational is left to
  say "this document still has graph state".
- **What a lost remove leaves stale** is the AGE mirror (the `Document` vertex
  and its edges) plus the tenant aggregates and orphan entities, which only a
  remove or reconcile recomputes. Finding those needs a Cypher scan through
  the graph backend. That is `graph_rag`'s domain, and this PR does not touch
  `graph_rag`.
- **Applying the removes at shutdown** on a second connection would put two
  graph writers in one process (the deadlock class PR #53 addresses), or wait
  out the in-flight reconcile.
- **So** the lost removes are named in a WARNING with the real repair options
  (above).
- **Deferred follow-up** (in the handoff): a cheap, targeted
  `brain graphrag remove <id>` that calls `remove_document` for one id.
  `remove_document` is robust whether or not the `documents` row still exists.

**Lane robustness (review items 2-4):**

- **A failed connect** is logged (`describe_failure`, class only), and the lane
  marks itself closed and clears its queue, so later submits are no-ops. The
  ERROR line says graph sync is off until a restart, and names the count and
  ids of the removes it dropped (audit G2), since a later startup does not
  redo them (L9).
- **A failing request** is logged by class only: an exception message can
  quote row values (L7).
- **`lane.start()` runs LAST**, after `observer.start()` and the signal
  handlers, right before the stop-wait whose `finally` closes it. So no setup
  failure can leak the lane's thread and connection (W3: a failing observer
  factory; W3b: a failing `observer.start()`). Requests just queue until then
  (audit G3; round 2 had started it before `observer.start()`, outside the
  `try`).
- **The `except` around the lane's `conn.close()` is gone** (audit F2). psycopg's
  `close` is idempotent and does not raise, so the branch was dead.

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
| W1 | while the startup reconcile is blocked, the observer starts, and an injected event's file is synced to the DB. After shutdown, no lane thread is alive, and no "still finishing" warning was logged | the watcher is not blind during a slow startup reconcile (RED at `8ad72f6`), and `run_watcher` closes its lane cleanly |
| W2a | X's reconcile is IN FLIGHT when X is edited: X is reconciled again afterwards and sees the edit, and every reconcile ran on the lane thread | same-path ordering; the worker never runs graph syncs |
| W2b | X's reconcile is still PENDING when X is edited: X is reconciled exactly once, on the edited content | same-path ordering, deduped |
| L1 | lane: a reconcile for a pending document is deduped; FIFO order is kept | dedupe |
| L2 | lane: a reconcile requested while that document's reconcile is in flight runs again afterwards | no lost edit |
| L3 | lane: `remove` is never deduped and keeps its FIFO position | ordering |
| L4 | lane: `close` drops pending reconciles, drains pending removes, and ignores later requests | shutdown |
| L5 | lane: `close(timeout)` returns False while a reconcile is in flight, and the thread then finishes and exits | bounded shutdown |
| L6 | lane: the syncer is called with the LANE's connection (autocommit), not the caller's | its own connection, never the worker's |
| L7 | lane: a request that raises is logged by class only, and the next request still runs | one failure never stops the lane; no content in logs |
| L8 | lane: `close(0.05)` with a reconcile in flight and a remove queued reports the remove as undrained | a lost remove is never silent |
| L9 | lane: a `conn_factory` that raises leads to a logged error, an exited thread, and no-op submits *(superseded by R5 of `docs/plans/2026-10-22-vault-watcher-db-reconnect.md`: the lane now retries its connect with backoff and requests queue; `close` still ends it, and L9 was rewritten to say so)* | a failed connect cannot wedge the lane |
| L10 | lane: an idle lane closes at once (`close(1.0) is True`) | `close` wakes the thread (audit F1) |
| L11 | lane + PR #53: a real `GraphSyncer` reconcile on the lane waits while another session holds the tenant graph write lock, writes nothing until it is released, then completes | the lane's autocommit connection composes with the tenant lock (merge of master `8711ff0`) |
| W3 | `run_watcher` whose observer factory raises leaks no lane thread | the lane starts only after setup |
| W3b | `run_watcher` whose `observer.start()` raises leaks no lane thread | the lane starts last |
| W4 | a delete's remove queued behind a blocked reconcile at shutdown is logged with its document id and `brain graphrag build --force` | undrained removes are named |

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

- **The graph lags while the backlog runs.** It is best-effort: the lane catches
  up by itself, and `brain graphrag build --backfill` reconciles whatever is
  behind its watermark (`--force` for a full mirror rebuild). File content and
  search are never deferred.
- **New in-process concurrency: document writes (worker) alongside graph writes
  (lane), including a lock cycle.** The graph mention tables reference
  `documents ON DELETE CASCADE`. The lane's write transaction
  (`rewrite_doc_relational`) DELETEs a document's graph mention rows, then
  INSERTs rows whose FK check locks `documents` row X. The worker's
  `_handle_delete` DELETEs `documents` X, and its cascade reaches the same graph
  rows. Each can hold what the other needs, so this is a deadlock cycle, not just
  a lock wait. *(This bullet said "though not in a cycle" until the PR #54
  review of `8073bfb`, which reproduced the cycle with millisecond windows;
  Postgres picked the worker as the victim in the reproduction.)* It was not
  fixed in #54. **FIXED by PR #55** (`fix/graph-reconcile-doc-key-share`), in
  `graph_rag`: `ReconcilePlan.is_current` reads `documents` X `FOR KEY SHARE`,
  so the lane locks X before it touches any graph row, and the worker's DELETE
  waits for that one write transaction. That also closes #53's same-document
  residual. Its regression test is
  `tests/test_graphrag_deadlock.py::test_a_rewrite_and_a_concurrent_document_delete_do_not_deadlock`.
  The same cycle existed between processes (MCP or ingest writing documents
  while the watcher reconciles); the same graph_rag fix closes it. There is
  still only one graph WRITER per watcher process.
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

### Round 2: the PR #54 review and audit

New tests L8, L9 and W3/W4 went RED against `64e5911` first, each for its
reason:

- L8: no `undrained_removes`;
- L9: the failed connect was not logged;
- W3: "leaked a graph lane thread";
- W4: no `_LANE_CLOSE_TIMEOUT_S`.

L10 and the W1 clean-close assertion were added for audit F1.

**Two weak tests found by the matrix, both fixed:**

- **W1 caught MB (`close` without `notify`) only by timing.** A lane still busy
  at shutdown sees `_closing` without needing a wake-up. W1 now parks the lane
  with a barrier before stopping, so a `close` that does not wake it times out
  every time.
- **L9 called `close()` before its late submit,** which masked H2 (a failed
  connect that keeps accepting work). The lane now marks itself closed BEFORE
  it logs the connect failure. L9 waits for that log record and submits
  without closing first.

**The matrix run of record:** strict judge, snapshot `4b7562a`, `git archive`
copy only. All 50 mutants are OK.

| Mutant | Edit | Failing tests (exactly) |
|---|---|---|
| MB | `close` does not `notify` (audit F1) | L10 "closing an idle lane did not wake its thread"; W1 "run_watcher's close of an idle graph lane timed out" |
| H1 | the connect is outside the guard | L9 "the failed connect was not logged" |
| H2 | a failed connect keeps accepting work | L9 "a submit after a failed connect was queued" |
| H3 | `undrained_removes` returns `[]` | L8 "the undrained remove was not reported"; W4 "an undrained graph remove was not logged with its id and the repair" |
| H4 | the lane starts before the setup that can raise | W3 "run_watcher failed during setup and leaked a graph lane thread" |
| H5 | the shutdown warning omits the ids | W4 |
| H6 | a request failure logs the exception itself, not `describe_failure` | L7 "the failure was not logged by class only" |

The run also confirmed five further G kills, each at a targeted assertion, and
they are now predicted:

- G1 → W4 "the observer never started";
- G3 → W4 "the startup graph reconcile never ran";
- G2 → W4 (its removes bypass the lane);
- G9 → L8 and W4 (removes dropped);
- G10 → L9 (the late submit is accepted).

`graph_lane.py` is at 100% branch coverage over the lane and startup tests.

**Ceiling:** `watch.py` is now 1,198 lines: 1,184 after round 1, 1,195 after round 2, then +3 in round 3 for the reworded repair line. The extra
lines are the `_LANE_CLOSE_TIMEOUT_S` constant, the undrained-removes warning,
and starting the lane last. `graph_lane.py` stays a small leaf, well under
800 (take its count from `wc -l`). The CLAUDE.md growth record covers both.

### Round 3: the PR #54 review and re-audit of `f0ed695`

- **The repair line was wrong.** It named `brain graphrag build`, which
  repairs nothing: bare, it prints usage, and `--backfill` skips the deleted
  document. The warning now names the real options, verified against
  `cli.graphrag_build`, `cli.graphrag_refresh`, `build.build_graph`,
  `aggregates.refresh_aggregates` and `reconcile.reconcile_document`:
  - `build --force` for the ghost AGE `Document` vertices, re-extracting
    concepts for every document when concepts are on;
  - `refresh`, or the next non-skipped reconcile, for aggregates and orphan
    entities.

  W4 asserts `brain graphrag build --force`.
- **Audit G1:** the CLAUDE.md trail now carries 1,184 (`5e3c0f2`) and 1,195
  (`f0ed695`), both verified with `git show "${sha}:src/brain/vault/watch.py"
  | wc -l`, and a descriptive hop for this commit.
- **Audit G2:** a failed connect's ERROR names the count and ids of the queued
  removes it drops (L9).
- **Audit G3:** the lane starts LAST, after `observer.start()` and the signal
  handlers. W3b (a failing `observer.start()`) pins it.

**The matrix of record:** strict judge, snapshot `9c31e98`, `git archive` copy
only. All 53 mutants are OK.

| Mutant | Edit | Failing tests (exactly) |
|---|---|---|
| H2 (re-anchored) | a failed connect keeps accepting work | L9 "a submit after a failed connect was queued" |
| H4 | the lane starts before `observer.start()` (round 2's position) | W3b "observer.start() failed and run_watcher leaked a graph lane thread" |
| H4b | the lane starts right after the startup sync (round 1's position) | W3, W3b |
| R1 | the warning names a plain `brain graphrag build` | W4 |
| R2 | a failed connect's ERROR hides the dropped removes | L9 "the failed connect did not name the removes it dropped" |

### Merge of master `8711ff0` (PR #53, the graph write lock)

PR #53 makes every graph write transaction take a per-tenant advisory lock
(`brain.graph_rag.write_lock.acquire_graph_write_lock`), and splits
`reconcile_document` into a short read transaction, an extraction with no
transaction open, and a write transaction that takes the lock. The lane composes
with it unchanged. The lane is the watcher's only graph writer, and its reconciles
wait behind any tenant-wide writer (`graphrag refresh`, `build --force`). That wait
happens on the lane thread. The event worker never waits on the model call or on the
tenant lock; a delete of a document being written waits for that one write transaction
(before PR #55 it could deadlock as its victim; see Risks). Only the handoff
conflicted, and the merge kept both sides.

L11 runs a real `GraphSyncer` (with #53's `NullBackend`) on the lane while another
session holds the tenant lock. Mutation proof, in a `git archive` copy:

| Mutant | L11 result |
|---|---|
| none | passes |
| M2: the lock statement becomes a no-op | red at "the lane's reconcile did not wait on the tenant write lock" |
| M1: the lane leaves `autocommit` off | **survives, and this is expected.** #53's contract makes `reconcile_document` open a top-level `conn.transaction()` as its first database action, even on an idle `autocommit=False` connection. So the lock composes either way. The lane's autocommit matters only for the AGE bootstrap (`GraphSyncer._age_ready` runs only on autocommit), and L6 pins it. |
