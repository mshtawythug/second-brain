# RM-26 — Owed watcher work survives shutdown and reconnects (H-2, H-4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Ticket: RM-26 in `docs/specs/2026-10-23-roadmap-completion-design.md` (Wave 6).
Backlog rows H-2 and H-4 in `docs/plans/2026-10-23-backlog-reconciliation.md`.

## Problem

| Id | What is lost today | Where |
|---|---|---|
| H-2 | A delete (or a move's source) delivered after the shutdown drain closed the watcher is dropped. The next startup `sync_vault` reconciles upserts, but prunes only with `--prune`. | `_schedule_debounced`, the `state.closed` branch |
| H-4a | A delete whose `DELETE` committed just before the connection died is re-run on a fresh connection, finds no row, and never asks the graph lane to remove the AGE `Document` vertex. The vertex is a ghost until `brain graphrag remove`. | `_handle_delete` under `ReconnectingConnection.call` |
| H-4b | A delete the worker could not apply at shutdown (no DB) is redone only by `brain vault sync --prune`. | `_report_unapplied` |

The same shutdown also loses graph removes the lane could not apply (a timed-out
`close`, or a lane that gave up reconnecting). Today they are only logged, with
`brain graphrag remove` as the repair.

## Design

A new leaf, `src/brain/vault/owed_work.py` (RM-26.D1: a **file**, not a table,
because the watcher owes work precisely when the database is unreachable).

### The journal

`$BRAIN_HOME/state/watcher-owed.jsonl`, append-only JSON lines:

```
{"action": "delete", "path": "<vault-relative POSIX path>"}
{"action": "graph_remove", "id": "<document uuid>"}
```

* Each line goes out in ONE `os.write` on an `O_APPEND` descriptor, then `fsync`.
  Appends from the observer, worker and main threads therefore never interleave.
* The file is created with mode `0600` (re-asserted with `fchmod` on every open);
  the `state/` directory with `0700`.
* It holds vault-relative paths and document ids only: no titles, no content.
* An append that fails (`OSError`) logs a class-only WARNING and returns: the
  watcher never dies because it could not write down what it owes.

### Who appends

| Site | Appends |
|---|---|
| The closed-watcher drop in `_schedule_debounced` (H-2) | a `delete` of the job's path, for a dropped `delete` or `move` (a move's **source**; its destination is a new file, which the startup sync picks up) |
| `_report_unapplied` (H-4b) | the same for every unapplied `delete`/`move`, plus a `graph_remove` for each id a delete job captured and never handed to the graph |
| `run_watcher` after `lane.close` | a `graph_remove` for each of `lane.undrained_removes()` |

`GraphLane.undrained_removes()` now also returns the removes `_abandon` dropped
(the lane gave up reconnecting at shutdown), which it previously only logged.

### H-4a: capture the id before deleting

`_Job` carries a `DeleteCapture` (not part of job equality). `delete_vault_rows`
(moved out of `_handle_delete`, which keeps its docstring and delegates):

1. on a run that has captured nothing yet, `SELECT id` of the vault row at the path;
2. `DELETE … RETURNING id` (the schema-invariant ERROR log moves with it);
3. hands to the graph syncer every deleted id, plus every captured id that the
   DELETE did not return, was never handed, and has no `documents` row (the
   re-run case: the first run's DELETE committed and its reply was lost).

The extra live-row check is the same "deleted documents only" ruling as
`brain graphrag remove` (`graph_rag/repair.py`).

### Replay at startup

`replay_owed(conn, vault_root, graph_lane, *, journal)` runs in `run_watcher`
**after** the startup `sync_vault` and **before** the observer starts, on the
startup connection. After, not before, so a move whose destination still carries
the document's frontmatter id is renamed in place by the sync (keeping its
backlinks) and the owed delete of its source then finds no row.

* An owed path whose file exists again is **not** deleted (criterion 5); every
  path is re-checked against disk, which also covers a journal replayed after the
  owner restored an older backup.
* Otherwise it deletes exactly the vault row at that path (a **targeted** prune,
  never a full one) and enqueues a graph remove for each deleted id.
* An owed id is enqueued as a graph remove only if it has no `documents` row.
* A corrupt or invalid line (bad JSON, unknown action, an absolute or `..` path,
  a non-`.md` path, a non-UUID id) is skipped with a class-only WARNING naming
  its line number; the rest replays (criterion 6).
* On success the journal is rotated to `watcher-owed.jsonl.1`. On a `psycopg.Error`
  it is kept whole for the next start (every step is idempotent) and the watcher
  still starts.

### Opt-in

`run_watcher(..., owed_journal: Path | None = None)`. `None` disables the journal,
the same default-off shape as `enable_log_rotation`, so library callers and the
test suite (which share one session `BRAIN_HOME`) never replay each other's
journals. The CLI's `vault sync --watch` passes `owed_work.default_journal_path()`;
that edit to `cli.py` is net-zero.

## Files

| File | Change |
|---|---|
| `src/brain/vault/owed_work.py` | NEW leaf: `OwedJournal`, `DeleteCapture`, `delete_vault_rows`, `vault_relative_posix`, `replay_owed`, `ReplayReport`, `default_journal_path` |
| `src/brain/vault/watch.py` | wiring only: import, `_Job.capture`, `_WatcherState.owed`, the `owed_journal` parameter, the replay call, the two append sites, the lane hand-off; `_handle_delete`'s SQL and `_vault_relative_posix` move to the leaf |
| `src/brain/vault/graph_lane.py` | `undrained_removes()` also names removes `_abandon` dropped |
| `src/brain/cli.py` | `vault sync --watch` passes the default journal (net-zero) |
| `tests/test_vault_owed_work.py` | NEW: journal, replay, capture unit tests |
| `tests/test_vault_watch_owed_work.py` | NEW: criteria 1–6 through the watcher |
| `CLAUDE.md` | `vault/watch.py` row (promote hop, add growth), `cli.py` row (net-zero hop) |
| `CHANGELOG.md`, roadmap spec Appendix B, backlog H-2/H-4 | status |

## Tests (RED first)

1. H-2: a late delete event after close is applied at the next start.
2. H-2 move: a late move after close leaves no row at its source after the next start.
3. H-4b: a delete unapplied at shutdown is applied at the next start, no `--prune`.
4. H-4a (AGE test instance): the reconnect re-run of a committed delete removes the AGE `Document` vertex.
5. An owed path whose file reappeared is not deleted.
6. A corrupt line is skipped with a class-only WARNING; the rest replays.

Plus: file mode 0600, one write per line, append failure is non-fatal, invalid
paths and ids refused, live ids not removed, lane undrained/abandoned removes
journaled, replay keeps the journal on a DB error, no journal means no-op.

## Risks

* **Restored backup / reappeared file:** every owed path is re-checked on disk
  before anything is deleted; every owed id is checked for a live row.
* **Vault path changed between runs:** paths are vault-relative, so a replay
  against a different vault deletes a row only where that vault has no file at
  the same relative path. Accepted: the watcher is one vault per `BRAIN_HOME`.
* **fsync under `state.lock`** on the closed-watcher drop: only after shutdown,
  when no timer is armed, so nothing contends for the lock.

## Round 2 (PR #95 review)

| Finding | Fix |
|---|---|
| M1: the replay rotates the journal after queuing graph removes on the not-yet-started lane; a setup failure before `lane.start()` lost them. | `run_watcher` wraps the setup from the replay to `lane.start()`; on any failure it owes `lane.undrained_removes()` again (an unstarted lane returns its pending removes), then re-raises. |
| M2: a move's two delete branches (destination gone; destination is another document) did not capture the source row's id. And capturing alone is not enough for the second branch: on the re-run the source row is gone, so `is_another_document` is False and the re-run takes the rename path, which never deletes. | `_handle_move` threads `job.capture` to both `_handle_delete` calls, and `_job_runner` calls `owed_work.settle_capture` after every job: every captured, never-handed id whose row is gone is handed to the graph once, whichever branch the re-run took. The ghost handling moved out of `delete_vault_rows` into `settle_capture`, so there is one place for it. No branch is left open. |
| L3: a torn or short line could swallow the next entry. | A short `os.write` is a failure (`TornJournalWrite`, logged by class). A file that does not end in a newline gets one before the next line. A new journal's directory is fsynced. |
| L4: a worker alive past its join never reached its queued jobs. | `_take_queued` drains the queue without blocking (the sentinel is put back), and its delete/move jobs are owed. A race only duplicates an entry, and the replay is idempotent. |
| L5, L6 | A blank line in a replay test's journal; the closed-watcher comment no longer says deletes need prune. |

## Round 3 (PR #95 review)

The job IN FLIGHT when the worker outlives its join was owed by nobody. A delete
stuck on a hung socket died with the daemon worker; one that finished after
`lane.close()` had its graph remove dropped by the closing lane while its capture
recorded it as handed.

Owing the in-flight path alone does not cover the second case: once the delete
commits, the replay finds no row at the path and so no id. So:

* The worker records `state.current_job` (set before the job, cleared in its
  `finally`).
* Past the join, shutdown sets `state.worker_abandoned` FIRST, then owes the
  in-flight job's path (delete/move only) and EVERY id it captured, handed or
  not (`_owe_in_flight`), then the queued jobs.
* A job the worker finishes after `worker_abandoned` owes its own captured ids
  (in its `finally`, after clearing `current_job`). That covers a job still
  inside its id capture when shutdown looked, which had no id to owe yet.

Ordering: shutdown writes the flag before reading the slot; the worker clears
the slot before reading the flag. A job that finished before the flag was set
handed its removes before `lane.close()` (which runs after this branch), so the
lane drains or reports them. Every other interleaving owes the id at least once,
and the replay de-duplicates and checks for a live row.

### Round 3 residual (aud-95)

If in-flight job A finished while shutdown was still inside `_owe_in_flight` (an
fsync), the worker could take queued job B before `_take_queued` did. B's
`finally` owed only B's captured ids, so a B that then hung past exit lost its
delete. Once `worker_abandoned` is set, the worker now owes each job it dequeues
(`_owe`: path and unhanded ids) and starts none (`305b9a4`).
