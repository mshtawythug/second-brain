# Vault watcher: reconnect after a Postgres restart: plan and proof

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `fix/vault-watcher-db-reconnect`, base `second-brain/master` @ `5cb4e4d`
(PR #54 merged).

## Problem (found by the PR #54 reviewer)

The watcher's two long-lived connections never reconnect. These are the
worker's (`_worker_loop`) and, since PR #54, the graph lane's
(`GraphLane._run`). After a Postgres restart, each keeps using its dead
connection, so every later job fails until the watcher process is restarted:

- **Worker:** every edit is logged as `worker failed on upsert ...` and never
  synced.
- **Lane:** every graph request is logged as `graph reconcile failed ...`.

A lane whose first connect failed also turned itself off for good (PR #54's
L9).

## Rulings

- **R1. A connection is lost when psycopg says it is closed, not when the
  exception has a certain class.**
  - After a failure, `conn.closed` is checked. psycopg sets it, together with
    `broken`, once the server is gone.
  - Measured on the test DB (5434) on 2026-10-22: after `pg_terminate_backend`,
    the next statement raises `AdminShutdown` (an `OperationalError`, SQLSTATE
    57P01), and `closed` and `broken` both become True. A later statement
    raises `OperationalError` with no SQLSTATE.
  - Classing by exception would be wrong. `DeadlockDetected`,
    `LockNotAvailable` and `QueryCanceled` are `OperationalError` subclasses
    that are raised on a HEALTHY connection; the same measurement showed
    `closed` stays False after a lock timeout. The victim of the known
    lane/delete lock cycle (`docs/plans/2026-10-22-vault-watcher-startup-blind.md`,
    Risks) gets `DeadlockDetected`. Reconnecting there would log a connection
    loss that never happened, and it would run the job a second time. The state
    check also covers an `InterfaceError`, or any wrapper exception, raised
    after the connection died.
  - **The check runs after the job, whether it raised or returned.**
    `GraphSyncer.reconcile` and `remove` never raise: they log and swallow
    every failure (`graph_rag/sync.py`). So a lane request that ran on a dead
    connection RETURNS. An exception-only check would drop that request
    silently, and only the next request would reconnect. A job that returns on
    a connection that is now closed is treated as lost.
- **R2. The failed job is retried once, in place, on the fresh connection.
  Jobs are not re-enqueued.**
  - **Re-enqueueing would reorder jobs.** It puts the job behind later jobs for
    the same path, for example a delete behind a re-creation's upsert.
  - **Re-enqueueing would lose jobs at shutdown.** It puts the job behind the
    sentinel.
  - **Every job is idempotent when re-run:**
    - **upsert:** `sync_one_file` reconciles from disk by content hash.
    - **delete:** `_handle_delete` is a no-op once the row is gone.
    - **move:** the `UPDATE` matches nothing after a committed first try, and
      `sync_one_file` matches the destination by its frontmatter id.
    - **overflow full sync:** a full reconcile from disk.
    - **lane reconcile:** reads the latest DB state.
    - **lane remove:** `remove_document` is robust to an already-deleted
      document.
  - **A second failure is an ordinary job error.** It is counted and logged.
    If that failure also lost the connection, the next job reconnects again.
    So a job runs at most twice, and the lane is never wedged.
  - **Residual:** suppose a delete's `DELETE` commits and the connection dies
    before its `RETURNING` arrives. The retry then finds no row, so no graph
    remove is queued, and the AGE `Document` vertex stays. That is the same
    ghost, with the same repair, as the handoff's "lost graph removes" item.
- **R3. Backoff: try at once, then 1s, 2s, 4s, 8s, 16s, then 30s after each
  failed attempt.**
  - The schedule restarts at 1s after a successful connect.
  - While the watcher runs, attempts are unbounded. A database that is down for
    an hour is survived, and the queued jobs then run.
  - Once shutdown is requested, a failed attempt gives up. The backoff sleep
    wakes on shutdown:
    - **worker:** `state.stop_event.wait`;
    - **lane:** a condition wait that `close` notifies.
  - So at most one connect attempt runs after shutdown starts, bounded by
    `connect_raw`'s `connect_timeout=10`.
- **R4. Giving up at shutdown is reported, never silent.**
  - **Worker:** it does not process the rest of its queue. It reads it up to
    the sentinel and logs a WARNING with the number of jobs not applied and how
    many were deletes. The next startup sync redoes edits and renames; a delete
    is redone only by `brain vault sync --prune`.
  - **Lane:** it logs an ERROR naming the graph removes not applied, as PR #54
    did for a failed connect. Pending reconciles are redone by the next
    startup.
- **R5. The lane's first connect uses the same reconnect.** PR #54 turned the
  lane off for good when its first connect failed (L9). It now retries like
  any other lost connection, and requests queue until it connects. L9 is
  rewritten to say so; `close` still ends it (R3).
- **R6. One thread owns each connection.**
  - **Worker:** `run_watcher` opens the worker's first connection on the main
    thread, as before, so a database that is down at startup still fails fast.
    It wraps that connection in the worker's `ReconnectingConnection`. After a
    clean join, shutdown closes that object's CURRENT connection; the old code
    closed the original, which a reconnect may already have replaced. The
    leak-on-timeout branch is unchanged.
  - **Lane:** it owns its own object.
- **R7. Only transitions are logged, and with no content.**
  - A WARNING when the connection is lost or unavailable.
  - An INFO when it is restored, with the number of failed attempts.
  - A DEBUG per failed attempt.
  - The caller's WARNING or ERROR on giving up.
  - Exceptions are named by `describe_failure`, which gives the class only.
- **R8. Dependency injection: `sleep(delay)` and `stopping()`, no clock.** The
  schedule is indexed by attempt, so no time is read. Tests inject a `sleep`
  that records and returns. Production passes the interruptible waits from R3.
  Tests never sleep through a backoff.

## Design: `brain/vault/db_reconnect.py` (new module, under 800 lines)

- **`backoff_delays()`:** an iterator over the module-level constants
  `BACKOFF_INITIAL_S` and `BACKOFF_MAX_S`. They are read when it is called.
- **`ReconnectingConnection(conn_factory, *, label, sleep, stopping,
  conn=None)`:**
  - **`call(fn)`:**
    1. Makes sure there is a live connection. If none is open, or the current
       one is closed, it reconnects with backoff.
    2. Runs `fn(conn)`.
    3. If `fn` fails and the connection is now closed, it reconnects and runs
       `fn` once more (R2).
    4. It raises `ReconnectAbandoned` (a `BrainError`) when shutdown stops the
       reconnect (R3).
  - Every connection it opens gets `autocommit = True`.
  - `close()` closes the current connection, and calling it twice is safe.
- **`_worker_loop(link, ...)`:** the per-job body moves, unchanged, into
  `_run_job(conn, job, ...)`. It returns False for the stale-upsert skip, which
  used to be a `continue`. The loop runs each job through `link.call`. On
  `ReconnectAbandoned` it reports the job and the rest of the queue (R4) and
  returns.
- **`GraphLane(syncer, conn_factory, *, sleep=None)`:** `_run` sends every
  request through its own `ReconnectingConnection`. The default `sleep` is the
  condition wait from R3. On `ReconnectAbandoned`, it puts back an in-flight
  remove, then logs and clears the removes (R4).

## Tests (TDD, RED first at `5cb4e4d`)

| ID | Test | Pins |
|---|---|---|
| C1 | `backoff_delays` yields 1, 2, 4, 8, 16, 30, 30 | R3 schedule |
| C2 | a job that loses its connection is re-run once on a fresh, autocommit connection, its value is returned, and the dead connection is closed | R1, R2 |
| C3 | a job failing on a HEALTHY connection (`closed` False) is re-raised and never reconnects | R1 |
| C4 | failed connects sleep 1, 2, 4, then the connection is restored: one WARNING (loss), one INFO (restored, 3 failed attempts), and no exception message in the logs | R3, R7 |
| C5 | with `stopping()` True, one failed connect raises `ReconnectAbandoned` without sleeping | R3 |
| C6 | a retry that loses its connection again propagates; the job ran exactly twice | R2 |
| C7 | `close()` closes the current connection, and calling it twice is safe | R6 |
| C8 | a stop requested during a backoff sleep gets exactly one more attempt, then gives up | R3 |
| C9 | a job that RETURNS on a connection that is now closed (a never-raising callee swallowed the loss) is re-run once on a fresh connection | R1 (state check after a return) |
| C10 | the 2,000th backoff delay is still the 30s cap | R3 over a long outage (found by mutation D7, round 1) |
| WR1 | real DB: the worker syncs `a`, its backend is terminated, then `b` and `c` sync on a fresh connection (processed 3, errors 0) | the bug, worker |
| WR2 | shutdown with the DB unreachable: the worker reports the jobs it did not apply (count and deletes) up to the sentinel, and returns without sleeping | R4 |
| WR3 | real DB, `run_watcher`: the worker's backend is terminated, a created file is still synced, and at shutdown the worker's CURRENT connection is closed | R6, end to end |
| WR4 | `run_watcher` shutdown while the worker is in a 600s backoff returns within half its 10s worker join and logs the jobs not applied | R3 (stop_event wake) |
| L9 | rewritten: a lane that cannot connect retries with backoff (1, 2, 4, ...), queues requests, and `close` ends it; the ERROR names each remove not applied | R5, R4 |
| L12 | fake factory: the lane's first connection dies on its second job; that job re-runs on connection 2, later jobs run there, and connection 1 is closed | the bug, lane |
| L13 | real DB: the lane's backend is terminated, and the next reconcile still writes its watermark | the bug, lane, real |
| L14 | `close` wakes a lane in a 600s reconnect backoff (default `sleep`) and itself returns within the backstop | R3 (condition wake) |
| T1 | `tests.thread_wait.blocked_in_wait(thread, caller)` is True only while `thread` is blocked in a `threading` wait reached through `caller` | the barrier WR4 and L14 use (round 1) |

## Ceiling

`watch.py` is over 800 lines and has a CLAUDE.md row. This change grows it by
the `_run_job` extraction, the link wiring, and the shutdown report. The growth
is recorded in three places:

- inline at each site;
- in the module docstring;
- in the row's trail, where the previous descriptive hop is promoted.

`graph_lane.py` and the new `db_reconnect.py` stay under 800.

## Proof

### TDD: RED at `5cb4e4d`

These tests were written and run before any production change:

- **Behavioral RED, reproducing the bug:**
  - **WR3** (worker): "a file created after the worker's backend was terminated was not synced".
  - **L13** (lane, real DB): "the lane's reconcile after its backend was terminated never completed".
  - **L12** (lane, fake): "the lane did not re-run on a fresh connection: [('reconcile', 'a', 'c1')]".
- **API RED:** the rest failed on the missing `brain.vault.db_reconnect` module, or on the `sleep=`
  keyword `GraphLane` did not yet take. That is C1-C9, WR1, WR2, WR4, L9 and L14.

### Round 1 of the mutation matrix (five clauses, in an archive copy only)

There were 25 mutants. 20 were OK, and the negative controls were rejected. Five were not OK, and
each one found something:

- **D7 (stopping ignored), G4 and W2 (stopping not wired) found a production bug.** The hot loop
  they cause should spin forever, but it ended within milliseconds.
  - **Cause:** `backoff_delays` computed `initial * 2**n`, which overflows a float at about n =
    1,024. So `_connect` raised `OverflowError` instead of waiting.
  - **In production:** after about 8.5 hours of outage at the 30s cap, every job would have failed
    with `OverflowError`.
  - **Fix:** `backoff_delays` now doubles the previous delay. Regression test: C10 (RED with exactly
    `OverflowError: int too large to convert to float`).
- **G3 (default sleep not woken by `close`) survived because of a race in L14.** L14 closed the lane
  as soon as the factory had been called once. If `close` landed before the lane's `stopping()`
  check, the lane gave up without sleeping, so the sleep was never exercised.
  - WR4 had the same race: its barrier was a factory call count.
  - **Fix:** both now wait on `tests.thread_wait.blocked_in_wait`, which is true only while the
    thread is really blocked in its backoff wait (T1).
- **D9 (no autocommit) was a misprediction.**
  - **What it killed:** C2 (targeted), L6 and WR1.
  - **What survived:** WR3, which syncs one file on the reconnected connection; that sync was
    visible even without autocommit. WR1 syncs two, and its second is not visible.
  - The mechanism was not chased, because autocommit is pinned at its own site by C2 and L6. The
    prediction was corrected to {C2, L6, WR1}.

### Round 2 (after the overflow fix and the barriers)

D7, D9, G4 and W2 were now OK. Three were still not OK:

- **D1 and D5 had stale predictions, not surprises.**
  - **D1:** WR4's barrier message had been renamed to "the worker never entered its reconnect
    backoff", and the expected text still carried the old wording.
  - **D5:** a backoff that never grows also fails C10, which was written after D5's prediction.
    The prediction now includes it.
- **G3 survived again, through a second test defect.** The mutant's non-interruptible sleep runs
  inside `with self._cond:`.
  - So `close()` blocks on that lock for the whole backoff, then joins a lane that exits at once,
    and returns True about 30s later.
  - L14 asserted only that `close` eventually returned True.
  - **Fix:** L14 now runs `close` on a helper thread and requires it to finish within the shared
    backstop.
  - **Backoff patch:** L14 and WR4 now patch both `BACKOFF_INITIAL_S` and `BACKOFF_MAX_S` to 600s.
    The earlier 60s initial value had been capped to 30s by `BACKOFF_MAX_S`.

### Round 3 (final tree): 25 of 25 OK

The control passed, the negative controls (a wrong text, a wrong set) were both rejected, and the
archive was restored byte-identical. The judge reads only each JUnit failure's `message`, and the
failing set must EQUAL the prediction.

**The mutants that make a thread spin forever (D7, G4, W2) run only the tests named for them.** So
no hot loop can grow a list without bound; pytest-timeout is a backstop.

| Mutant | Edit | Killed by (at its assertion) |
|---|---|---|
| D1 | a job's exception never reconnects | C2, C6, WR1, WR3, WR4 |
| D2 | lost is decided by exception class | C3 "a healthy-connection error re-ran the job" |
| D3 | no state check after a RETURN | C9, L12, L13 |
| D4 | reconnect, but do not re-run | C2, C6, C9, WR1, WR3, L12, L13 |
| D5 | backoff never grows | C1, C4, L9, C10 |
| D6 | backoff never capped | C1, C10 |
| D7 | `stopping()` ignored | C5, C8, WR4, L14 |
| D8 | dead connection not closed | C2 |
| D9 | fresh connection not autocommit | C2, L6, WR1 |
| D10 | restore not logged | C4 |
| D11 | loss WARNING logs the message | C2 |
| D12 | connect WARNING logs the message | C8 |
| D13 | per-attempt DEBUG logs the message | C4 |
| G1 | lane abandon hides the removes | L9 |
| G2 | in-flight remove not reported | L9 |
| G3 | lane's default sleep not woken by `close` | L14 |
| G4 | lane's `stopping` not wired | L14 |
| W1 | worker's sleep is not the stop event | WR4 |
| W2 | worker's `stopping` not wired | WR4 |
| W3 | rest of the queue not reported | WR2 |
| W4 | shutdown closes the ORIGINAL connection | WR3 |
| W5 | stale upsert counted | `test_stale_upsert_event_for_vanished_file` |
| W6 | `ReconnectAbandoned` not caught | WR2, WR4 |
| W7 | a raising close escapes shutdown | `test_run_watcher_handles_already_closed_connection` |
| W8 | repair hint loses `--force` | `test_undrained_removes_at_shutdown_are_logged` |

The harness (`mutate_rc.py`) and its JUnit output lived in the session scratchpad and are not
checked in. Each mutant's anchor text is in the table above, and each one was applied exactly once
(clause b).

### Suites and checks (final tree)

- **Targeted suites, run serially on a sibling test DB:** 317 passed. These are `test_thread_wait`,
  `test_vault_db_reconnect`, `test_vault_watch_db_reconnect`, `test_vault_graph_lane`,
  `test_vault_watch_startup_blind`, `test_vault_watch_fire_race`, `test_vault_watch_move_identity`,
  `test_vault_watcher`, `test_cli_vault_watch`, `test_debounce_timer`, `test_graphrag_sync_wiring`
  and `test_doctor_watcher`.
- **Lint and types:** `ruff check` and `mypy src/` (224 files) are clean. The new and rewritten
  files are ruff-format clean; `watch.py`, `thread_wait.py` and `test_thread_wait.py` were already
  unformatted at base and are not reformatted.
- **Full suite:** CI.

### Ceiling

`watch.py` grew from 1,198 to 1,265 lines. Its CLAUDE.md row is updated:

- the previous descriptive hop is PROMOTED to `0c461f7` (1,198);
- a new descriptive hop and a growth record are added;
- the module docstring carries the pointer.

`graph_lane.py` is 195 lines and the new `db_reconnect.py` is under 200. Re-derive both with
`wc -l`.
