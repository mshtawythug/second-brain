# Vault-watcher `_fire` race: plan and proof

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `fix/vault-watcher-fire-race`, base `second-brain/master` @ `a6a76fe` (PR #48 merged).

## Problem

Items 1-3 were found while reviewing PR #48
(`docs/plans/2026-10-22-vault-watcher-absence-tests.md`, Risks). Item 4 was
found in this PR's own audit. All four are in `src/brain/vault/watch.py`.

### 1. `_fire` steals its successor's entry

`_schedule_debounced` arms one `threading.Timer` per path. At `a6a76fe` its `_fire`
callback was:

```python
def _fire() -> None:
    with state.lock:
        state.pending_timers.pop(path, None)   # pops BY PATH: whoever is there
    _enqueue(state, _Job(...), config=config)  # outside the lock
```

`threading.Timer.cancel()` cannot stop a callback that has already started. So
here is the interleaving for one path:

1. T1's interval elapses. `Timer.run` sees "not cancelled" and enters `_fire`.
2. A new event for the same path calls `_schedule_debounced`. It pops T1,
   calls `T1.cancel()` (too late), and arms T2 at `pending_timers[path]`.
3. T1's `_fire` takes the lock and pops `pending_timers[path]`. That is **T2's**
   entry. T1 then enqueues its job.

The consequences:

- **Two jobs for one burst.** T1's superseded job, then T2's when it fires.
- **T2 is orphaned.** It is no longer in `pending_timers`, so the shutdown drain
  (`run_watcher`'s `finally`) cannot see it. It neither cancels T2 nor enqueues
  T2's path before the `None` sentinel. If shutdown comes first, T2 later fires
  into a queue whose worker has already exited. **The edit that armed T2 is lost.**

### 2. The enqueue happens outside the lock (the same window, reached a second way)

Even with an identity check, `_fire` popped under the lock and enqueued after
releasing it. So a drain could run in between: it would see nothing pending, put
the sentinel, and leave `_fire`'s job behind the sentinel. That edit would also
be lost, and nothing would be stolen to give it away.

### 3. A known-false comment

`watch.py:800-803` (at `a6a76fe`) says that without the stale-upsert guard,
`sync_one_file` "would raise `FileNotFoundError`" and the worker "would bump
`state.errors`". It does neither. It records "file does not exist" in its report
and returns, so the unguarded worker counts the phantom as **processed**. This
was measured in PR #48 (mutation e6).

### 4. The drain enqueued every pending path as an `upsert`; found in the PR #49 audit

At `a6a76fe`, `pending_timers` kept only the timer, so `_drain_pending_timers`
(then inline in `run_watcher`) enqueued an `upsert` for every pending path:

- A pending `delete` became a stale upsert. The worker skips it, so the row survives.
- A pending `move` upserted its vanished source, and the destination was not synced
  by that run.

The identity check from item 1 made this a **regression** for a `delete` whose
`_fire` loses the lock to the drain. At `a6a76fe` that late fire still enqueued
its delete before the sentinel. With the identity check it returns, and the
drain's `upsert` was the only job left.

The auditor replayed it deterministically: schedule a `delete` for a missing
file, run the real drain, put the sentinel, then fire. The queue ended as
`[_Job(action='upsert', ...deleted.md), None]`.

## Fix

All in `_schedule_debounced`, its `_fire`, and `_drain_pending_timers`:

```python
job = _Job(action=action, abs_path=path, dest_path=dest)

def _fire() -> None:
    with state.lock:
        entry = state.pending_timers.get(path)
        if entry is None or entry[0] is not timer:
            return
        del state.pending_timers[path]
        _enqueue(state, job, config=config)

...
state.pending_timers[path] = (timer, job)
```

- **Identity, not path.** A `_fire` acts only if `pending_timers[path]` holds its
  OWN timer, captured by the closure. Otherwise it returns without enqueueing.
- **Enqueue under the lock.** The check, the pop and the put are one critical
  section. So the drain sees either "still pending" (and drains it) or "already
  enqueued", never anything in between.
  - The overflow branch already enqueued under this lock.
  - `state.jobs` is an unbounded `Queue`, so `put` never blocks.
  - The worker never takes `state.lock`.
- **Each entry keeps its job.** `pending_timers: dict[Path, tuple[DebounceTimer, _Job]]`,
  one dict, so the timer and the job cannot drift apart. The drain enqueues the
  REAL pending job: a `delete` stays a delete, and a `move` keeps its
  destination.

### No lost event: a `_fire` returns without enqueueing only when its entry is gone or replaced

A `pending_timers[path]` entry leaves the dict in exactly four ways. Each one
holds `state.lock`, and each one hands the path's work to something that will
run before the sentinel:

| Removed by | Who covers the event |
|---|---|
| its own `_fire` (identity holds) | that `_fire` enqueues its job, inside the same critical section |
| supersession (`_schedule_debounced` for the same path) | the successor, which is now the entry and is itself resolved by one of these four rows. This is the last-wins semantics a cancel that arrives in time always had. |
| overflow (`len >= _MAX_PENDING`) | the full-vault `upsert` job, enqueued under the same lock |
| shutdown drain | the entry's OWN job (same action, same `dest`), enqueued before the sentinel |

A superseded or drained `_fire` that lost the race therefore drops nothing that
another row does not cover. For a drained one, the drain enqueued the very job
the fire would have. Since a `_fire` enqueues under the lock, any job it puts
precedes the drain's critical section, and therefore the sentinel. The drain's
own enqueues and the sentinel run on one thread, in order.

**The one remaining way past the sentinel is outside this race.**
`observer.join(5.0)` can time out, and an event delivered after the drain arms
a timer nobody drains. That predates this change, the tests' fake observer
cannot reach it, and it is recorded in the handoff.

## Test seam: an injectable timer factory

`_WatcherState` gains `timer_factory: Callable[[float, Callable[[], None]], DebounceTimer]`.
It defaults to `daemon_timer`, a daemon `threading.Timer`, so production is
unchanged. `DebounceTimer` (a `start`/`cancel` Protocol) and `daemon_timer` live
in the new module `src/brain/vault/debounce_timer.py`. That keeps `watch.py`'s
growth to the wiring.

The test injects a `ManualTimer`:

- `start()` and `cancel()` only record.
- `fire()` runs the callback on the test thread unconditionally. This models a
  timer thread that has already passed its cancel check.

The race is then three synchronous steps, with no threads and no sleeps:

1. arm T1;
2. schedule T2, which "cancels" T1;
3. `T1.fire()`.

The drain moves out of `run_watcher`'s `finally` into
`_drain_pending_timers(state, config)`, so a test can run the real drain.

New tests, in `tests/test_vault_watch_fire_race.py` (A-G) and
`tests/test_debounce_timer.py` (T):

| Id | Test | Claim |
|---|---|---|
| A | `test_superseded_fire_leaves_successor_pending` | After the race, T2 is still `pending_timers[path]`, T1 enqueued nothing, and T2's fire enqueues exactly one job |
| B | `test_raced_successor_is_drained_before_sentinel` | After the race, the real drain plus the sentinel: T2's path is queued before the `None`, and a late T2 fire adds nothing after it |
| C | `test_fire_enqueues_before_a_racing_drain_closes_queue` | A drain cannot run between `_fire`'s pop and its put. The `on_event` hook (called just before the put) runs the drain only if `state.lock` is free, and the job must still precede the sentinel |
| D | `test_fire_that_lost_to_overflow_adds_nothing` | A `_fire` that lost to the overflow flush adds nothing: the queue holds only the full-vault upsert |
| E | `test_drain_enqueues_a_pending_delete_as_delete` | A `delete` pending at shutdown drains as `[_Job("delete", p), None]` |
| F | `test_drain_enqueues_a_pending_move_with_its_destination` | A `move` pending at shutdown drains as that move, with its `dest` |
| G | `test_delete_fire_that_loses_to_the_drain_is_not_lost` | The auditor's replay: schedule a delete, drain, sentinel, then the losing fire. The queue is `[_Job("delete", p), None]`, with nothing after the sentinel |
| T | `test_daemon_timer_builds_an_unstarted_daemon_threading_timer` | `daemon_timer(60, cb)` is a `threading.Timer` with `daemon is True`, and not alive |

## Ceiling

`watch.py` is in CLAUDE.md's over-800 table: 1,070 at `f8c76c0`, untouched
through `a6a76fe`. This change:

- extracts the timer seam to a new module;
- records the remaining growth inline at `_fire` and the drain, AND as a
  module-docstring bullet;
- extends the row's trail. The trail now has SHA-bound hops for 1,070 through
  `a6a76fe` and for 1,095 at `bd7c8d8`, the first fix commit. The descriptive
  hop it carried was promoted when this review-fix commit touched the file, and
  a new descriptive hop was added for the review-fix commit.

The row stops saying "no commit through", so the anchored-grep set shrinks by
one. The parenthetical that already records `setup.py` leaving that set gets
the same note, with no digit.

## Risks

- **Holding `state.lock` across `on_event`.** It is a test-only hook, and the
  overflow path already calls it under the lock. A hook that takes `state.lock`
  would deadlock. No hook does, and the `_enqueue` docstring says so.
- **`timer` and `job` are captured by the closure.** Both are assigned before
  `start()`, so no fire can observe either unbound. The overflow branch returns
  before arming, and its loop variable is `pending`, not `timer`, so it cannot
  rebind the cell the identity check reads.
- **`pending_timers` changed shape.** It is now `(timer, job)`. Readers:
  `_fire`, `_schedule_debounced`, the drain, and two test helpers
  (`tests/test_vault_watcher.py::_pending`, the rig's `pending()`). Both test
  helpers map entries back to timers.
  `test_graceful_shutdown_drains_pending` reads only `len(...)`.
- **Still open, out of scope, and recorded in the handoff.** Debouncing is keyed
  by path, and the last event wins across actions. So a `move` keyed on its src,
  followed within `debounce_ms` by a recreated src, becomes an upsert of src,
  and the destination is not synced. Also, an observer that outlives
  `observer.join(5.0)` can arm a timer after the drain.

## Proof

### TDD: RED, then GREEN

**Round 1 (`bd7c8d8`).** First the seam landed alone: the `timer_factory` field,
`daemon_timer`, and the extracted `_drain_pending_timers`, while `_fire` still
had its `a6a76fe` body. The existing `tests/test_vault_watcher.py` passed (54).
A-D went RED, each at its targeted assertion:

- A: "the superseded timer's _fire popped its successor's pending entry"
- B: "a job landed behind the shutdown sentinel" (`[job, None, job]`)
- C: "a drain ran between _fire's pop and its put" (`[None, job]`)
- D: "a fire that lost to the overflow flush still enqueued its own job"

With the fix, all four passed.

**Round 2 (review fixes).** E-G were written first, against `bd7c8d8`'s code,
and went RED at their targeted assertions:

- E: "the drain did not enqueue the pending delete as a delete"
- F: "...the pending move with its destination"
- G: "the delete that lost to the drain was not delivered as a delete"
  (`[_Job(action='upsert', ...deleted.md), None]`, the auditor's exact queue)

With `(timer, job)` entries, all pass. T passes against `daemon_timer` as written.
It goes red only under its mutant (ME, below).

### Mutations (five clauses)

The harness is a throwaway script, not committed, using the same design as
PR #48's:

- Each mutant is a `str.replace` on `watch.py` (or `debounce_timer.py` for
  ME), and its `str.count` is asserted equal to the declared count (1).
- Each (mutant, test) pair runs as its own pytest node: 8 + 6×8 + 1 = 57 runs.
- Both files' bytes are restored in a `finally`.
- Clause (d) matches the expected text only in the failing block: the `>`
  statement plus its `E` lines.

Every cell not marked red passed:

| Mutant | Edit | Red at (targeted statement) |
|---|---|---|
| none | n/a | none: all 8 pass |
| M0 | `a6a76fe`'s `_fire` (pop by path, enqueue after the lock) | A `rig.pending() == {note: t2}`; B `jobs[-1] is None`; C `jobs[-1] is None` (drain between pop and put); D overflow loser enqueued; G `jobs[-1] is None` |
| MA | identity check removed, enqueue still under the lock | A `rig.pending() == {note: t2}`; B `jobs[-1] is None`; D overflow loser enqueued; G `jobs[-1] is None` |
| MB | identity kept, enqueue moved outside the lock | C `jobs[-1] is None` (drain between pop and put) |
| MC | identity kept, but the loser still enqueues its job | A `rig.events == []`; B `jobs[-1] is None`; D overflow loser enqueued; G `jobs[-1] is None` |
| MD | the drain enqueues `_Job("upsert", path)` again | E `== [_Job("delete", ...), None]`; F `== [_Job("move", ..., dest_path=dst), None]`; G `jobs == [_Job("delete", ...), None]` |
| ME | `daemon_timer` without `timer.daemon = True` | T `timer.daemon is True` |
| neg | MA, with the wrong claimed assertion (`rig.events == []`) for A | **MISMATCH**: the checker can fail |

Each claim has its own killing mutant (clause e):

- "do not steal the successor": MA → A;
- "do not enqueue when superseded": MC → A;
- "nothing behind the sentinel after a race": MA / MC → B, G;
- "no drain between pop and put": MB → C (the only test that kills MB);
- "the overflow loser adds nothing": MA / MC → D;
- "the drain keeps the action": MD → E;
- "the drain keeps a move's destination": MD → F;
- "a delete that lost to the drain is delivered as a delete": MD → G;
- "the default timer is a daemon": ME → T.

### Stability

- `test_vault_watch_fire_race.py` plus `test_debounce_timer.py` passed 5 times in
  a row (8 passed each run). There are no threads and no sleeps, so nothing is
  timing-dependent.
- Run once, serially: the watcher and docs-guard files
  (`test_vault_watch_fire_race.py`, `test_debounce_timer.py`,
  `test_vault_watcher.py`, `test_cli_vault_watch.py`, `test_thread_wait.py`,
  `test_graphrag_sync_wiring.py`, `test_reading_surface_pointers.py`,
  `test_repo_hygiene_files.py`, `test_eval_workflow_contract.py`). 292 passed.
- The full suite was not run locally; CI is the full-suite evidence.

### Lint and ceiling

- `ruff check` and `mypy src/` are clean.
- The new files are `ruff format`-clean. In `watch.py` (unformatted at base,
  like most of the repo), no format hunk overlaps a line this change added.
- `watch.py`: 1,070 at `a6a76fe`, then 1,095 at `bd7c8d8`; the review fixes add
  a few more lines (take the present count from `wc -l`). What grew is the
  wiring, `_drain_pending_timers`, the `(timer, job)` entry and the `_fire`
  reasoning. It is recorded inline at `_fire` and at the drain, in a
  module-docstring bullet, and in the CLAUDE.md row.
- `debounce_timer.py` is a new leaf, well under 800.
