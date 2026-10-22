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

### 5. The overflow flush dropped deletes and moves; found in the PR #49 review

The same bug class as item 4, on the overflow path. The flush cancelled and
cleared every entry, then enqueued only a full-vault `upsert`. The worker runs
that as `sync_vault(prune=False)`, which can neither remove a deleted file's row
nor move a row in place: it walks a move's destination as a new file.

- **A regression from the identity check.** A `delete` whose `_fire` lost the
  lock to the flush now returned. The reviewer's replay, with `_MAX_PENDING=1`:
  schedule a delete for `gone.md`, schedule an upsert for `other.md` (which
  overflows), then fire the delete's timer.
  - `a6a76fe` gives `[upsert(vault), delete(gone.md)]`.
  - `a2b8af7` gives `[upsert(vault)]`.
- **Pre-existing.** The flushed entries' delete/move jobs were always dropped.
  The overflowing call's own job was never armed and never enqueued either.

**Fix:** under the lock, the flush first enqueues the real job of every flushed
entry whose action is not `upsert`, plus the current call's job if it is not
`upsert`. Only then does it enqueue the full-vault upsert. The order matters: a
move updates `vault_path` before the full sync walks its destination as a new
file. Upserts stay covered by the full sync.

### 6. A scheduling exception lost a job and could kill the observer; found in the PR #49 review of `8341ea1`

- **A failing timer lost a job.** `_schedule_debounced` popped and cancelled
  the existing entry, and stored the new one, BEFORE it started the new timer.
  - Constructing a `threading.Timer` creates no OS thread. `start()` does, and
    raises `RuntimeError: can't start new thread` under thread exhaustion.
  - So a failing `start()` left a never-started timer as the entry, while the
    old entry's armed timer had already been cancelled. A raising factory
    likewise lost the old entry's job, and a racing `_fire` returned on the
    identity check.
- **The exception escaped into the observer thread.** `_Handler.on_any_event`
  wrapped only classification in its try/except, so the exception reached
  watchdog's observer thread. That can kill the thread and silently stop every
  later event.

**Fix:**
- Build AND `start()` the timer first, inside the lock, before the pop, the
  cancel and the store.
  - **A raising factory** leaves nothing popped or stored, so the old entry
    stays pending and ARMED. The exception propagates to the handler guard.
  - **A raising `start()`** (`RuntimeError`, which is what `threading` raises)
    falls back to delivery WITHOUT debounce: there is no timer, so the newest
    event wins at once, as debounce would have let it. Under the lock it pops
    and cancels the existing entry (if any), enqueues the NEW job directly, and
    logs once, with no path or content. Nothing is lost. A delete that
    supersedes a pending upsert is delivered as a delete, and an event for a
    path with nothing pending is delivered immediately. This replaced a
    recorded residual (the new event was dropped) at the coordinator's request,
    in 11 lines including comments.
  - Starting before the store is safe: a firing `_fire` blocks on `state.lock`,
    which is held until after the store, and its identity check then finds its
    own entry.
  - The overflow path builds and starts no timer at all. The overflow decision
    is computed first, discounting the path's own entry, which is popped either
    way.
  - My first attempt only moved the factory call above the pop (`fb20bde`,
    local, never pushed). The auditor showed it does not cover the realistic
    `start()` failure, and the `fb20bde` archive run below confirms it.
- Wrap each scheduling call in the same log-and-continue guard as
  classification.
  - It logs the action and path, never content.
  - It does not bump `state.errors`, because that counter is the worker's alone
    (its own comment: "Only the worker thread touches them").
  - A `_schedule_debounced` call from `_maybe_watch_new_directory` already runs
    inside the classification guard. A failure there aborts only the rest of
    that one directory event.

## Fix

All in `_schedule_debounced` (including its overflow branch), its `_fire`, and
`_drain_pending_timers`:

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

### No lost event: the closed list of `pending_timers` removers

Items 4 and 5 were the same defect found twice: a remover that did not hand off
the entry's job. So this section is a **closed list**, not a sample. It was
derived from every reference in `src/`, via
`grep -rn "pending_timers" src/`, at the review-fix commit:

- **One writer:** `state.pending_timers[path] = (timer, job)` in `_schedule_debounced`.
- **Exactly five removers**, nothing else:
  - `del` in `_fire`;
  - `pop(path, None)` in `_schedule_debounced` (row 2);
  - `pop(path, None)` in its `start()`-failure fallback (row 2b);
  - `.clear()` in the overflow branch;
  - `.clear()` in `_drain_pending_timers`.

There are no other `pop`, `del` or `clear` calls, no reassignment of the dict,
and no reader outside `watch.py` except test code (two helpers and one inline
`len` check at `tests/test_vault_watcher.py:822`). Every remover
holds `state.lock`.

| # | Remover | Its proof that the entry's job is not lost | Pinned by |
|---|---|---|---|
| 1 | `_fire`: `del`, identity holds | It enqueues its own job in the same critical section, so the drain sees it either pending or already queued | A (T2's fire), C |
| 2 | supersession: `pop(path)` in `_schedule_debounced` | The current call's job replaces it, and that job is resolved by one of these rows. This is **last-wins**, the semantics a cancel that arrives in time always had. **Known gap, recorded and not fixed here:** last-wins across ACTIONS, e.g. a `move` keyed on src superseded by a recreated src (handoff item 3). **Exceptions:** the new timer is built AND started BEFORE the pop. A raising factory leaves the old entry pending and armed (nothing popped or stored), and the exception reaches `_Handler.on_any_event`, which logs it and keeps the observer thread alive | A, B, L, N |
| 2b | supersession on a failed `start()`: `pop(path)` in the `RuntimeError` fallback of `_schedule_debounced` | The NEW call's job is enqueued directly, under the lock, in the same critical section as the pop. The newest event wins exactly as row 2's last-wins, without the debounce delay. With nothing pending, the pop is a no-op and the event is still delivered | Ma, Mb |
| 3 | overflow `.clear()` | Every flushed entry's non-upsert job, plus the current call's non-upsert job, is enqueued before the full-vault upsert, under the lock. Upserts are covered by that full sync (`sync_vault` re-reads every file) | D, H, I, J, K |
| 4 | drain `.clear()` | It enqueues each entry's own job (same action, same `dest`) before the sentinel, on the thread that then puts the sentinel | B, E, F, G |

A `_fire` that finds its entry gone or replaced returns. Rows 2, 2b, 3 and 4
are the only ways that happens. Each one has already enqueued that fire's work,
or handed it to the newer job, which is itself enqueued (2b) or resolved by
these rows (2). For rows 3 and 4 the job enqueued is the very job
the fire would have queued, or an upsert covered by the full sync. Since a `_fire` enqueues under the lock, any job it puts
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

New tests, in `tests/test_vault_watch_fire_race.py` (A-N, with M split into Ma/Mb) and
`tests/test_debounce_timer.py` (T):

| Id | Test | Claim |
|---|---|---|
| A | `test_superseded_fire_leaves_successor_pending` | After the race, T2 is still `pending_timers[path]`, T1 enqueued nothing, and T2's fire enqueues exactly one job |
| B | `test_raced_successor_is_drained_before_sentinel` | After the race, the real drain plus the sentinel: T2's path is queued before the `None`, and a late T2 fire adds nothing after it |
| C | `test_fire_enqueues_before_a_racing_drain_closes_queue` | A drain cannot run between `_fire`'s pop and its put. The `on_event` hook (called just before the put) runs the drain only if `state.lock` is free, and the job must still precede the sentinel |
| D | `test_fire_that_lost_to_overflow_adds_nothing` | An UPSERT `_fire` that lost to the overflow flush adds nothing: the queue holds only the full-vault upsert, which covers it |
| E | `test_drain_enqueues_a_pending_delete_as_delete` | A `delete` pending at shutdown drains as `[_Job("delete", p), None]` |
| F | `test_drain_enqueues_a_pending_move_with_its_destination` | A `move` pending at shutdown drains as that move, with its `dest` |
| G | `test_delete_fire_that_loses_to_the_drain_is_not_lost` | The auditor's replay: schedule a delete, drain, sentinel, then the losing fire. The queue is `[_Job("delete", p), None]`, with nothing after the sentinel |
| H | `test_overflow_flush_enqueues_a_pending_delete_before_the_full_sync` | A pending `delete` plus an overflow gives `[delete job, full-vault upsert]` |
| I | `test_delete_fire_that_loses_to_the_overflow_flush_is_not_lost` | The reviewer's replay: the delete precedes the full sync, and the losing fire adds nothing |
| J | `test_overflow_flush_enqueues_a_pending_move_with_its_destination` | A pending `move` plus an overflow gives `[move job with dest, full-vault upsert]` |
| K | `test_overflowing_call_that_is_a_delete_is_enqueued` | The call that trips the overflow is a `delete`: it is enqueued before the full sync, the flushed upsert is covered by it, and the overflowing call builds NO timer (`len(rig.timers)` is unchanged) |
| K2 | `test_overflowing_call_whose_timer_cannot_start_still_flushes` | With `start()` failing, an overflowing call still flushes: `[flushed delete, full sync]`, and nothing is left pending |
| L | `test_raising_timer_factory_leaves_the_existing_entry_pending` | A factory raising on its 2nd call: P's original delete timer stays pending, and shutdown gives `[delete P, None]` |
| Ma | `test_start_failure_delivers_a_delete_that_supersedes_a_pending_upsert` | The fake `start()` raises "can't start new thread" for a delete superseding a pending upsert. Nothing raises; the queue is `[delete P]` exactly once; nothing is pending; the upsert's timer is cancelled |
| Mb | `test_start_failure_on_a_fresh_path_delivers_the_event_now` | The same failure with nothing pending: the queue is `[delete P]` exactly once, and nothing is pending |
| N | `test_handler_survives_a_scheduling_failure` | A schedule whose timer factory raises is logged ("schedule failed") and does not escape `on_any_event`; a later event for another path is the only entry scheduled |
| T | `test_daemon_timer_builds_an_unstarted_daemon_threading_timer` | `daemon_timer(60, cb)` is a `threading.Timer` with `daemon is True`, and not alive |

## Ceiling

`watch.py` is in CLAUDE.md's over-800 table: 1,070 at `f8c76c0`, untouched
through `a6a76fe`. This change:

- extracts the timer seam to a new module;
- records the remaining growth inline at `_fire` and the drain, AND as a
  module-docstring bullet;
- extends the row's trail with SHA-bound hops: 1,070 through `a6a76fe`, 1,095
  at `bd7c8d8` (the first fix) and 1,101 at `a2b8af7` (the drain fix). Each
  descriptive hop was promoted by the next commit to touch the file. A new
  descriptive hop covers the overflow-flush commit.

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

**Round 3 (`a2b8af7` review, the overflow flush).** H-K were written first,
against `a2b8af7`'s code, and went RED at their targeted assertions:

- H: "the overflow flush dropped a pending delete, or queued it after the full sync"
- I: "the delete that lost to the overflow flush was dropped or delivered twice"
- J: "...dropped a pending move, or queued it after the full sync"
- K: "the overflowing call's own delete was dropped"

With the flush enqueuing non-upsert jobs before the full sync, all 12 race and
timer tests pass.

**Round 4 (`8341ea1` review, exception safety).** Every RED check ran in a `git
archive` copy with the new test file dropped in.

- **Against `8341ea1`:** L ("a raising timer factory dropped the existing
  pending entry") and N ("a scheduling failure escaped into the observer
  thread") went RED. So did the first start-failure test, M: "a timer whose
  start() raised replaced the existing pending entry".
- **Against `fb20bde`** (the superseded factory-first attempt, local and never
  pushed): M and N were still RED. The realistic `start()` failure was not
  covered.
- **Residual closed.** M asserted the old entry stayed pending. That design
  dropped the NEW event, so a delete superseding an upsert was lost, and so was
  any event on a path with nothing pending. At the coordinator's request this
  was closed instead of recorded. M was replaced by Ma and Mb, which went RED
  against the staged build-and-start code ("a failing start() raised instead of
  delivering the event") before the fallback landed.

With the fallback, all 16 race and timer tests pass.

### Mutations (five clauses)

The harness is a throwaway script, not committed, using the same design as
PR #48's:

- Each mutant is a `str.replace` on `watch.py` (or `debounce_timer.py` for
  ME), and its `str.count` is asserted equal to the declared count (1).
- Each (mutant, test) pair runs as its own pytest node. The run of record is
  round 4's: 16 unmutated + 14 mutants × 16 tests + 1 negative control = 241
  runs. It ran in a `git archive` copy of the final tree (`git stash create`
  snapshot `da5ee13`), and each run's raw output was kept.
- Both files' bytes are restored in a `finally`. **Process note:** these runs
  mutated the live worktree. A concurrent auditor read `watch.py` mid-run and
  saw the M0 mutant. After every round, `git diff` against the previous commit
  was verified to show only the intended source. **From round 4 on, every
  harness ran only in `git archive` copies,** and the live worktree was never
  mutated. A first round-4 run kept no per-run logs. It showed every test red
  under MO and MP, even `test_debounce_timer.py`. Re-running those cells in the
  same archive gave exactly the predicted results, and the file was
  byte-identical afterwards. The run of record above keeps every log.
- Clause (d) matches the expected text only in the failing block: the `>`
  statement plus its `E` lines.

Every cell not marked red passed:

| Mutant | Edit | Red at (targeted statement) |
|---|---|---|
| none | n/a | none: all 16 pass |
| M0 | `a6a76fe`'s `_fire` (pop by path, enqueue after the lock) | A `rig.pending() == {note: t2}`; B `jobs[-1] is None`; C `jobs[-1] is None` (drain between pop and put); D overflow loser enqueued; G `jobs[-1] is None`; I delete delivered twice |
| MA | identity check removed, enqueue still under the lock | A `rig.pending() == {note: t2}`; B `jobs[-1] is None`; D overflow loser enqueued; G `jobs[-1] is None`; I delete delivered twice |
| MB | identity kept, enqueue moved outside the lock | C `jobs[-1] is None` (drain between pop and put) |
| MC | identity kept, but the loser still enqueues its job | A `rig.events == []`; B `jobs[-1] is None`; D overflow loser enqueued; G `jobs[-1] is None`; I delete delivered twice |
| MD | the drain enqueues `_Job("upsert", path)` again | E `== [_Job("delete", ...), None]`; F `== [_Job("move", ..., dest_path=dst), None]`; G `jobs == [_Job("delete", ...), None]`; L `rig.queued() == [_Job("delete", note), None]` |
| MF | overflow flush reverted (the non-upsert jobs are not enqueued) | H, I, J, K: each `rig.queued() == [..., full sync]` |
| MG | the flush enqueues its jobs AFTER the full sync | H, I, J, K: same statements (order) |
| MH | the flush omits the overflowing call's own job | K `rig.queued() == [_Job("delete", gone), full sync]` |
| MI | a factory failure pops the existing entry (the `8341ea1` order) | L `rig.pending() == {note: original}` |
| MN | no `start()` fallback (the `except` never matches) | Ma, Mb `raised is None` ("a failing start() raised instead of delivering the event") |
| MO | the fallback leaves the existing entry pending | Ma `rig.pending() == {}` |
| MP | the fallback drops the new job | Ma, Mb `rig.queued() == [_Job("delete", note)]` |
| OVFSTART | the overflowing call builds and starts a timer (`if not overflowing:` changed to `if True:`) | K `len(rig.timers) == timers_before`; K2 `rig.queued() == [delete, full sync]` |
| MJ | no scheduling guard in `_Handler.on_any_event` | N `escaped is None` ("a scheduling failure escaped into the observer thread") |
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
- "the overflow flush keeps flushed deletes/moves": MF → H, J;
- "a delete that lost to the flush is delivered exactly once": MF → I
  (dropped); M0 / MA / MC → I (delivered twice);
- "the flushed jobs precede the full sync": MG → H, I, J, K;
- "the overflowing call's own non-upsert job is kept": MH → K;
- "a raising factory leaves the old entry pending": MI → L;
- "a failing `start()` delivers the new event instead of raising": MN → Ma, Mb;
- "nothing stays pending after a `start()` failure": MO → Ma;
- "the new event is delivered exactly once": MP → Ma, Mb;
- "a scheduling error never escapes into the observer thread": MJ → N;
- "the overflowing call builds and starts no timer": OVFSTART → K, K2;
- "the default timer is a daemon": ME → T.

**The OVFSTART gap (PR #49 review of `76c9c2c`).**

- **The hole.** The Fix section's claim that "the overflow path builds and
  starts no timer at all" was stated but pinned by no test. The reviewer's
  mutant OVFSTART survived 15/15. Under thread exhaustion it would make row
  2b's fallback return BEFORE the flush, so no full sync is queued, and the
  flushed deletes and moves stay pending.
- **The fix.** K now asserts that the overflowing call builds no timer. K2
  covers the start-failure variant.
- **Evidence** (a `git archive` copy of `76c9c2c` with the new test file):
  - unmutated, all 17 pass;
  - OVFSTART applies 1/1 and reds K at `len(rig.timers) == timers_before` and
    K2 at "the overflow flush was skipped when a timer could not start";
  - the other 15 pass;
  - the file was restored byte-identical.
- **No correction needed.** The matrix had no OVFSTART-equivalent recorded as
  killed. The 241-run matrix above predates K2 and the K assertion; this row
  was run separately.
- **No CLAUDE.md hop.** `watch.py` is unchanged by this commit, so the ceiling
  row's trail stays correct as is.

### Stability

- `test_vault_watch_fire_race.py` plus `test_debounce_timer.py` passed 5 times in
  a row (16 passed each run in round 4; 17 after K2). There are no threads and no sleeps, so nothing is
  timing-dependent.
- Run once, serially: the watcher and docs-guard files
  (`test_vault_watch_fire_race.py`, `test_debounce_timer.py`,
  `test_vault_watcher.py`, `test_cli_vault_watch.py`, `test_thread_wait.py`,
  `test_graphrag_sync_wiring.py`, `test_reading_surface_pointers.py`,
  `test_repo_hygiene_files.py`, `test_eval_workflow_contract.py`). 296 passed
  in round 3, and 300 in round 4.
- The full suite was not run locally; CI is the full-suite evidence.

### Lint and ceiling

- `ruff check` and `mypy src/` are clean.
- The new files are `ruff format`-clean. In `watch.py` (unformatted at base,
  like most of the repo), no format hunk overlaps a line this change added.
- `watch.py`: 1,070 at `a6a76fe`, 1,095 at `bd7c8d8`, 1,101 at `a2b8af7`. The
  overflow-flush commit adds a few more lines; take the present count from
  `wc -l`. What grew is:
  - the wiring;
  - `_drain_pending_timers`;
  - the `(timer, job)` entry;
  - the overflow flush;
  - the `_fire` reasoning.

  It is recorded inline at `_fire`, the drain and the flush, in a
  module-docstring bullet, and in the CLAUDE.md row.
- `debounce_timer.py` is a new leaf, well under 800.
