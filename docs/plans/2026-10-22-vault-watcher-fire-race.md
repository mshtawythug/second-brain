# Vault-watcher `_fire` race: plan and proof

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `fix/vault-watcher-fire-race`, base `second-brain/master` @ `a6a76fe` (PR #48 merged).

## Problem

Found while reviewing PR #48 (`docs/plans/2026-10-22-vault-watcher-absence-tests.md`,
Risks). Two items, both in `src/brain/vault/watch.py`.

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

## Fix

All in `_schedule_debounced` / `_fire`:

```python
def _fire() -> None:
    with state.lock:
        if state.pending_timers.get(path) is not timer:
            return
        del state.pending_timers[path]
        _enqueue(state, _Job(...), config=config)
```

- **Identity, not path.** A `_fire` acts only if `pending_timers[path]` is its OWN
  timer, captured by the closure. Otherwise it returns without enqueueing.
- **Enqueue under the lock.** Check, pop and put form one critical section, so
  the drain sees either "still pending" (and drains it) or "already enqueued".
  It never sees anything in between.
  - The overflow branch already enqueued under this lock, so this follows an
    existing pattern.
  - `state.jobs` is an unbounded `Queue`, so `put` never blocks.
  - The worker never takes `state.lock`, so this cannot deadlock against it.

### No lost event: a `_fire` returns without enqueueing only when its entry is gone or replaced

A `pending_timers[path]` entry leaves the dict in exactly four ways. Each one
holds `state.lock`, and each one hands the path's work to something that will
run before the sentinel:

| Removed by | Who covers the event |
|---|---|
| its own `_fire` (identity holds) | that `_fire` enqueues the job, inside the same critical section |
| supersession (`_schedule_debounced` for the same path) | the successor, which is now the entry and is itself resolved by one of these four rows. This is the same last-wins semantics a cancel that arrives in time already had. |
| overflow (`len >= _MAX_PENDING`) | the full-vault `upsert` job, enqueued under the same lock |
| shutdown drain | the drain's `upsert` for the path, enqueued before the sentinel |

A superseded or drained `_fire` that lost the race therefore drops nothing that
another row does not cover. Since enqueueing happens under the lock, any job
put by a `_fire` precedes the drain's critical section, and therefore the
sentinel.

**The shutdown drain itself is unchanged.** After the fix, no timer can enqueue
after the drain's critical section. Its own enqueues and the sentinel run on one
thread, in order.

The one remaining way past the sentinel is outside this race. `observer.join(5.0)`
can time out, and an event delivered after the drain arms a new timer that
nobody drains. That predates this change and is not reachable from the tests'
fake observer. It is recorded below, not fixed here.

## Test seam: an injectable timer factory

`_WatcherState` gains `timer_factory: Callable[[float, Callable[[], None]], DebounceTimer]`.
It defaults to a daemon `threading.Timer`, so production is unchanged.
`DebounceTimer` (a `start`/`cancel` Protocol) and the default factory live in a
new module, `src/brain/vault/debounce_timer.py`, which keeps `watch.py`'s growth
to the wiring.

The test injects a `ManualTimer`:

- `start()` and `cancel()` only record.
- `fire()` runs the callback on the test thread unconditionally. This models a
  timer thread that has already passed its cancel check.

The race is then three synchronous steps, with no threads and no sleeps:

1. arm T1;
2. schedule T2, which "cancels" T1;
3. `T1.fire()`.

The drain moves out of `run_watcher`'s `finally` into `_drain_pending_timers(state, config)`
(behaviour-preserving), so a test can run the real drain against the raced state.

New tests, in `tests/test_vault_watch_fire_race.py`:

| Test | Claim |
|---|---|
| `test_superseded_fire_leaves_successor_pending` | After the race, T2 is still `pending_timers[path]`, T1 enqueued nothing, and T2's fire enqueues exactly one job |
| `test_raced_successor_is_drained_before_sentinel` | After the race, the real drain plus the sentinel: T2's path is queued before the `None`, and a late T2 fire adds nothing after it |
| `test_fire_enqueues_before_a_racing_drain_closes_queue` | A drain that tries to run between `_fire`'s pop and its put cannot. The `on_event` hook (called just before the put) runs the drain only if `state.lock` is free, and the job must still precede the sentinel |

TDD: the seam lands first as a behaviour-preserving refactor. The tests are run
RED against it while `_fire` still has the `a6a76fe` body. Then the fix lands.

## Ceiling

`watch.py` is in CLAUDE.md's over-800 table (1,070 at `f8c76c0`; untouched
through `a6a76fe`). This change:

- extracts the timer seam to a new module;
- records the remaining growth inline at the growth site AND as a
  module-docstring pointer;
- extends the row's trail. The SHA-bound hop is 1,070 untouched through
  `a6a76fe`, followed by a descriptive hop for this commit.

The row stops saying "no commit through", so the anchored-grep set shrinks by
one. The parenthetical that already records `setup.py` leaving that set gets
the same note, with no digit.

## Risks

- **Holding `state.lock` across `on_event`.** It is a test-only hook, and the
  overflow path already calls it under the lock. A hook that takes `state.lock`
  would deadlock. No hook does, and the `_enqueue` docstring says so.
- **`timer` captured by the closure before it is assigned.** It is assigned
  before `start()`, so no fire can observe it unbound. The overflow branch
  returns before arming, and its `_fire` is never registered.
- **Out of scope, pre-existing, recorded.** The shutdown drain converts every
  pending job to `upsert`.
  - A pending `delete` at shutdown becomes a stale upsert, which is skipped. The
    row survives until a pruning sync.
  - A pending `move` becomes an upsert of its vanished source, and the
    destination is never synced.
  - Both are separate from this race and go to the handoff as follow-ups.

## Proof

### TDD: RED, then GREEN

First the seam landed alone: the `timer_factory` field, `daemon_timer`, and the
extracted `_drain_pending_timers`. `_fire` still had its `a6a76fe` body. With
that seam in place, the existing `tests/test_vault_watcher.py` passed (54), and
all four new tests went RED, each at its targeted assertion:

- A: "the superseded timer's _fire popped its successor's pending entry"
- B: "a job landed behind the shutdown sentinel" (`[job, None, job]`)
- C: "a drain ran between _fire's pop and its put" (`[None, job]`)
- D: "a fire that lost to the overflow flush still enqueued its own job"

With the fix, all four pass.

### Mutations (five clauses)

The harness is a throwaway script, not committed, using the same design as
PR #48's. Each mutant is a `str.replace` on `watch.py`, and its `str.count` is
asserted equal to the declared count (1). Each (mutant, test) pair runs as its
own pytest node, and the file's bytes are restored in a `finally`. Clause (d)
matches the expected text only in the failing block: the `>` statement plus its
`E` lines. Every cell is a separate run.

| Mutant | Edit | A | B | C | D |
|---|---|---|---|---|---|
| none | n/a | pass | pass | pass | pass |
| M0 | the `a6a76fe` body (pop by path, enqueue after the lock) | red: `rig.pending() == {note: t2}` | red: `jobs[-1] is None` | red: `jobs[-1] is None` (drain between pop and put) | red: overflow loser enqueued |
| MA | identity check removed, enqueue still under lock | red: `rig.pending() == {note: t2}` | red: `jobs[-1] is None` | pass | red: overflow loser enqueued |
| MB | identity kept, enqueue moved outside the lock | pass | pass | red: `jobs[-1] is None` (drain between pop and put) | pass |
| MC | identity kept, but the loser still enqueues its job | red: `rig.events == []` | red: `jobs[-1] is None` | pass | red: overflow loser enqueued |
| neg | MA, with the wrong claimed assertion (`rig.events == []`) for A | **MISMATCH**: the checker can fail | n/a | n/a | n/a |

Each claim has its own killing mutant (clause e):

- "do not steal the successor": MA → A;
- "do not enqueue when superseded": MC → A;
- "nothing behind the sentinel after a race": MA / MC → B;
- "no drain between pop and put": MB → C;
- "the overflow loser adds nothing": MA / MC → D.

MB is killed by C alone. That is why C exists: the identity check does not
close that window by itself.

### Stability

- `tests/test_vault_watch_fire_race.py` passed 5 times in a row (4 passed, 0.8-1.0s
  each, load average 24-140 across the runs). It uses no threads and no
  sleeps, so it has no timing to be unstable about.
- Run once, serially, together: the watcher and docs-guard files
  (`test_vault_watch_fire_race.py`, `test_vault_watcher.py`,
  `test_cli_vault_watch.py`, `test_thread_wait.py`,
  `test_graphrag_sync_wiring.py`, `test_reading_surface_pointers.py`,
  `test_repo_hygiene_files.py`, `test_eval_workflow_contract.py`). 288 passed.
- Also run once: the module-enumerating guards (`test_mcp_compat.py`,
  `test_database_url_isolation.py`, `test_compose_no_raw_calls.py`,
  `test_update_document_mirror_ordering.py`). 255 passed, 1 skipped.
- The full suite was not run locally; CI is the full-suite evidence.

### Lint and ceiling

- `ruff check` and `mypy src/` are clean.
- The two new files are `ruff format`-clean. In `watch.py` (unformatted at base,
  like most of the repo), no format hunk overlaps a line this change added.
- `watch.py`: 1,070 at `a6a76fe`. It grew by the wiring, `_drain_pending_timers`
  and the `_fire` reasoning. That is recorded inline at `_fire` and at the
  drain, plus a module-docstring bullet, and in the CLAUDE.md row (trail
  extended; descriptive hop for this commit).
- `debounce_timer.py` is a new leaf, well under 800.
- `grep -c '^| .*no commit through' CLAUDE.md` now returns the `enrichment.py`
  row alone. The parenthetical that records `setup.py` leaving the set notes
  `vault/watch.py` leaving it too, with no digit.
