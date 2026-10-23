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
  - **A re-run is told it is one (PR #56 audit).** If the connection dies after a
    worker job's last statement, the job RETURNS and is re-run (R1). The re-run then
    finds the body cache that the completed sync primed, and takes the fence-only
    branch. That is right for the data: the sync is done, and the cache is primed only
    after `sync_one_file` returns, which a lost connection's `OperationalError` never
    lets it do. It was wrong for the stats, which counted one sync as processed AND
    skipped. `_job_runner` passes `rerun=True` on the second run, and a re-run's
    fence-only hit counts nothing (WR5).
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
  - So at most one connect attempt runs after shutdown starts.
    - **Its handshake fits inside both shutdown windows** (the lane's 10s close and the worker's
      10s join). Every watcher connection is opened with `connect_timeout=WATCHER_CONNECT_TIMEOUT_S`
      (2s), passed by the CLI's factory through `connect_raw(..., connect_timeout=)`. libpq
      applies it to each address a host resolves to, so `localhost` (`::1`, then `127.0.0.1`)
      costs at most 4s.
    - **Not bounded:** the two probe queries `connect_raw` runs after the handshake (the
      `pg_type` lookup, then `register_vector`). A server that accepts a connection and then
      stalls can still outlast the windows.
    - **When the windows ARE exceeded, the report is still right (R4a):**
      - a lane `close` that times out names the remove in flight as well as the queued ones;
      - the lane then does not log them a second time;
      - the warnings say "still finishing a request or reconnecting".
    - *(Corrected in the PR #56 review round. This said "bounded by `connect_raw`'s
      `connect_timeout=10`". A 10s handshake bound equals the 10s windows rather than fitting
      inside them, and the probe queries come on top of it.)*
- **R4. Giving up at shutdown is reported, never silent.**
  - **Worker:** it does not process the rest of its queue. It reads it up to
    the sentinel and logs a WARNING with the number of jobs not applied and how
    many were deletes. The next startup sync redoes edits and renames; a delete
    is redone only by `brain vault sync --prune`.
  - **Lane:** it logs an ERROR naming the graph removes not applied, as PR #54
    did for a failed connect. Pending reconciles are redone by the next
    startup.
- **R4a. A timed-out lane close names the remove in flight, once (PR #56 review).**
  - **The gap:** `undrained_removes()` named only QUEUED removes. A remove the lane had already
    popped, still waiting on a slow connect, was in neither list when `close` timed out.
  - **The fix:** the lane tracks `_inflight` under its condition, and `undrained_removes()` lists
    the in-flight remove first.
  - **Ownership:** a `close` that times out hands the report to its caller. `_abandon`, which runs
    if the lane later gives up, then logs nothing instead of naming the same ids again.
  - **The warnings:**
    - `run_watcher`'s lane warning now says "still finishing a request or reconnecting ... may
      not be applied".
    - The worker's leak warning now says "still running a job, or reconnecting to the DB".
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
| L15 | a remove in flight while the lane's connect blocks past `close(0.05)` is named by `undrained_removes()`, and the lane does not log it again when it gives up | R4a (review round) |
| B1 | `connect_raw(url, connect_timeout=2)` opens with `connect_timeout` 2; the default stays 10 | R3 bound (review round) |
| B2 | the CLI's watch factory calls `connect_raw` with `connect_timeout=WATCHER_CONNECT_TIMEOUT_S` | R3 bound, wiring (review round) |
| B3 | two addresses' worth of `WATCHER_CONNECT_TIMEOUT_S` fits inside both `_LANE_CLOSE_TIMEOUT_S` and `_WORKER_JOIN_TIMEOUT_S` | R3 nesting (review round) |
| WR5 | a worker job whose connection dies AFTER its sync completed is re-run, and counts processed 1, `skipped_fence_only` 0 | R2 re-run flag (audit round) |
| M1 | the worker's leak warning names the reconnecting case (`test_worker_join_timeout_skips_close_when_alive`), and so does `run_watcher`'s lane warning (`test_undrained_removes_at_shutdown_are_logged`) | R4a wording (review round) |

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
- **So the worker's behavioral RED is WR3 alone.** WR1 reproduces the same bug on the
  worker, but it was RED only on the missing module (API-RED), because it drives the new
  `_worker_loop(link, ...)` signature directly. *(Added in the PR #56 audit round.)*

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

### Round 3 (the tree pushed as `c3161a7`): 25 of 25 OK

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

### Suites and checks (at `c3161a7`)

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

`watch.py` was 1,198 lines at `0c461f7` and 1,265 at `c3161a7`. Both counts are frozen; check
them with `git show "${sha}:src/brain/vault/watch.py" | wc -l`. The review round grows it again,
so take the present count from `wc -l`. Its CLAUDE.md row is updated:

- the hops for `0c461f7` and `c3161a7` are PROMOTED;
- a new descriptive hop and a growth record are added;
- the module docstring carries the pointer.

`graph_lane.py` and the new `db_reconnect.py` stay well under 800; re-derive both with `wc -l`.
`db.py` (under 800) and `cli.py` (one line shorter) changed only for the connect bound.

### The PR #56 review and audit round

**The reviewer's MINOR, at `c3161a7`.** R3's bound was a 10s connect, which equals the 10s
shutdown windows rather than fitting inside them. On top of that, a remove in flight during a slow
reconnect was not named. Both are fixed, under R3 and R4a:

- **The lane:**
  - it tracks `_inflight`;
  - a `close` that times out hands the report to its caller;
  - `_abandon` then logs nothing.
- **`connect_raw(..., connect_timeout=)`:** the CLI's watch factory passes
  `WATCHER_CONNECT_TIMEOUT_S` (2s).
- **The warnings** now say "still finishing a request or reconnecting" and "or reconnecting to the
  DB".

**Why `src/brain/cli.py` and `src/brain/db.py` changed.**

- `connect_raw` had a hard-coded `connect_timeout=10`. It gains a keyword parameter, whose default
  of 10 every other caller keeps.
- The CLI's `vault sync --watch` connection factory is the one place the watcher's connections are
  opened, so it passes the 2s bound there. That is a net-negative change to `cli.py`.

**The auditor's LOW notes:**

- the re-run stat (R2, WR5), whose RED in a pre-fix archive copy was `processed/skipped_fence_only
  (1, 1), expected (1, 0)`;
- the TDD note above.

**Mutation rows added (round 4: 33 of 33 OK, on the tree before the audit fix):**

| Mutant | Edit | Killed by |
|---|---|---|
| G2 (re-anchored) | the in-flight remove is left out of `_unapplied_removes` | L9, L15 |
| R2 | `_abandon` ignores the hand-off | L15 "the lane logged removes its caller had already reported" |
| R3 | `close` never hands off | L15 (same assertion) |
| R4 | `_inflight` not cleared after a request | L16 "an applied remove was still reported as undrained" |
| R5 | `connect_raw` ignores `connect_timeout` | B1 |
| R6 | the CLI's watch factory is unbounded | B2 |
| R7 | `WATCHER_CONNECT_TIMEOUT_S` raised to 5 | B3 |
| R8 | the leak warning goes back to "mid-statement" | M1 (`test_worker_join_timeout_skips_close_when_alive`) |
| R9 | the lane warning goes back to "a reconcile" | M1 (`test_undrained_removes_at_shutdown_are_logged`) |
| R10 | `_job_runner` never flags a re-run | WR5 (added with the audit fix; first run in round 5) |

### Round 5 (final tree): 34 of 34 OK

The whole matrix (34 mutants: rounds 1-3's 25 with G2 re-anchored, R2-R10, and nothing
dropped) ran on the final tree. Every edit applied exactly once. The control passed, the
negative controls were rejected, and the archive was restored byte-identical.

- 32 were OK as predicted.
- D3 (no state check after a RETURN) and D4 (no re-run) also killed WR5, which was written
  after their predictions:
  - D3: "fixture: the job was not re-run on a fresh connection";
  - D4: "processed/skipped_fence_only (0, 0)".
- Both were OK once WR5 was added to their predictions.

**Suites (final tree, run serially on a sibling test DB):** 416 passed. That is the round-3 set
plus `test_db`, `test_repo_hygiene_files` and `test_reading_surface_pointers`. `ruff check` and
`mypy src/` (224 files) are clean.

