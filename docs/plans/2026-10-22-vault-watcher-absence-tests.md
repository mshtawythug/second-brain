# Vault-watcher absence tests: plan and proof

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `test/vault-watcher-absence-sync`, base `second-brain/master` @ `58e27c6`.

## Problem

Open follow-up in `docs/handoff/2026-10-13-merge-all-handoff.md`: *"Watcher
burst/stale-event tests that `sleep` and then assert ABSENCE can false-PASS
under load. The vault-watcher half of this class is still open."*

A test that sleeps and then asserts something did NOT happen passes
vacuously if the watcher has not processed the event yet. Under load (load
average 50-140 during a full suite on this machine) the debounce timer thread
or the worker thread can easily be descheduled for longer than the sleep. Then
a watcher that WOULD sync the event, enqueue the extra job, or delete the
row still passes, because the assertion ran before the bad thing happened.

## Inventory: every vault-watcher test that asserts absence

Re-derive with
`grep -n "sleep\|deadline" tests/test_vault_watcher.py tests/test_cli_vault_watch.py`
and by reading each test that asserts `== []`, `== 0`, `== processed_before` or
`len(...) == N`. Only `tests/test_vault_watcher.py` and
`tests/test_cli_vault_watch.py` exercise `brain.vault.watch` threads. A grep for
`run_watcher|_worker_loop|_schedule_debounced` over `tests/` returns only those two.

| Test | Absence asserted | Old wait | In class? |
|---|---|---|---|
| `test_burst_of_modifies_collapses_to_one_sync` | no 2nd..5th job | first event, then `sleep(0.1)` | **yes** |
| `test_multi_path_bursts_yield_one_sync_per_path` | no 3rd+ job | two events, then `sleep(0.1)` | **yes** |
| `test_watcher_ignores_template_events` | no job | `sleep(0.1)` | **yes** |
| `test_watcher_ignores_non_md_files` | no job | `sleep(0.1)` | **yes** |
| `test_stale_delete_event_preserves_existing_file` | row not deleted | enqueue hook, then 0.5s spin | **yes** |
| `test_stale_upsert_event_for_vanished_file` | no error, no row, `processed` unchanged | enqueue hook, then 0.5s spin | **yes** |
| `test_vanished_file_with_cached_body_is_noop` | no sync, no skip, no error, `processed` unchanged | enqueue hook, then 0.5s spin | **yes** |
| `test_fence_only_write_skips_resync` / `test_cache_refreshed_after_real_sync` | no sync | waits on `skipped_fence_only`, which the SAME job sets on the skip branch | no: positive sentinel already |
| `test_real_body_change_*`, `test_cold_start_*`, `test_delete_invalidates_*` | `skipped_fence_only == 0` | waits on the sync spy, and the same single job takes the sync branch or the skip branch, never both | no |
| `test_overflow_triggers_full_sync` | exactly one overflow job | overflow enqueues synchronously inside `inject()` | no |
| `test_single_create_event_triggers_one_sync`, `test_graceful_shutdown_drains_pending` | exact event list | after `thread.join()` (full barrier) | no |
| `test_cli_vault_watch.py::test_watch_prints_initial_sync_summary` | none (`sleep(0.05)` before stop) | n/a | no |

## Fix: no production change

`src/brain/vault/watch.py` is in CLAUDE.md's over-800 table. Every fix below
uses seams that already exist, so the file does not change and owes no trail hop:

1. **Debounce / filter tests (4): a debounce that cannot fire on its own.**
   `WatchConfig(debounce_ms=_UNFIRED_DEBOUNCE_MS)` (one hour). `inject()` runs
   classify and schedule synchronously on the test thread. So when it returns,
   an accepted event IS in `state.pending_timers`, and it cannot have fired and
   left. The absence check reads `pending_timers` under `state.lock`. It is
   exact, and it holds on proof rather than after a sleep.
   - **Filter tests.** The absence check is followed by a positive control: a
     valid note injected through the same handler must show up in
     `pending_timers`. That proves the probe can see an accepted event. Then
     shutdown runs. `run_watcher`'s drain turns every pending timer into
     exactly one enqueued job, and `thread.join()` is a full barrier.
     `events == [("upsert", control)]` is then exact.
   - **Burst tests.** The test captures the `threading.Timer` that each
     `inject()` arms. It is deterministic, because none can fire for an hour.
     Each SUPERSEDED timer is then `join`ed with the shared backstop. A
     terminated `Timer` either was cancelled or has already run `_fire`, and
     `_fire` calls `on_event` before it returns. So after the join, "it is
     dead" AND "no event yet" proves it never fired, and never will. A
     superseded timer that is still alive is a job that WILL fire. That is
     the targeted absence assertion.
2. **Stale-event tests (3): the synchronous worker drain.**
   `_run_events_through_worker` (already in the file, and the precedent set
   when the racing-edit test was de-flaked on 2026-10-15) classifies the
   event, enqueues the job, enqueues the `None` sentinel and runs
   `_worker_loop` on the test thread. When it returns, the job has been
   processed, so every counter and every DB row is final. These claims are
   `_worker_loop`'s existence guards. The event-to-job path through
   `run_watcher` is still covered by `test_delete_event_removes_row`,
   `test_single_create_event_triggers_one_sync` and the burst tests.

Why not a positive sentinel job behind the stale job on the live worker?
`_enqueue` calls `on_event` BEFORE `jobs.put`, on the timer thread. So
observing the hook does not prove the put happened, and a sentinel enqueued by
the test could overtake it. `processed` also cannot tell "barrier done" from
"barrier sync done, increment pending". Both gaps are real, and the
synchronous drain has neither.

Why not an injectable timer factory in `WatchConfig`? It would work, but it
grows an over-ceiling file to get a determinism that the one-hour debounce plus
`pending_timers` already gives.

## Risks

- **`Timer` as an observable.** The burst tests rely only on public
  `threading.Thread` API (`join`, `is_alive`) and on the watcher's own
  `pending_timers` dict, which the tests already read (`test_graceful_shutdown_drains_pending`).
- **The one-hour timers must not leak.** The shutdown drain cancels every
  pending timer. A test that fails mid-way leaves daemon timer threads that
  never fire within the run.
- **A mutated run costs the backstop.** Under the "no cancel" mutation each
  burst test waits `WAIT_FOR_BACKSTOP_S` (10s) on the first live superseded
  timer before going red. That is the backstop, not a latency budget, and a
  passing run never pays it.
- **Coverage of the threaded path.** The stale-event claims now run without
  `run_watcher`'s timer and worker threads. The event → debounce → job →
  worker path keeps its threaded coverage in `test_delete_event_removes_row`,
  `test_single_create_event_triggers_one_sync`, `test_worker_swallows_per_file_errors`
  and the burst tests.
- **Load.** Only the targeted watcher tests are run, serially. CI is the
  full-suite evidence.

## Mutation proofs (five clauses)

Clauses: (a) passes unmutated; (b) the mutation applies with its declared
count; (c) the test goes red; (d) it fails at the targeted absence assertion,
not at a timeout elsewhere; (e) one entry per independent claim.

**Harness.** This is a throwaway script, not committed. Each entry applies
`str.replace` edits to `src/brain/vault/watch.py`, and each edit's
`str.count` is asserted equal to its declared count (clause b). It then runs
one node with
`pytest <file>::<test> --no-cov -p no:cacheprovider -q -x` against a sibling test
database on the 5434 test container (`second_brain_test_vwabs`), and restores
the original bytes in a `finally`. `git status` showed `watch.py` clean
after every run. Clause (d) is checked against the FAILING block only, meaning
the `>` statement pytest marks plus its `E` lines. It also requires that no
backstop message (`predicate never became true`, `did not start observer`,
`watcher did not stop`) appears there.

**The (d) check was itself proved able to fail.** The first run matched the
expected text anywhere in pytest's output. That accepted `e6` as "fails at
the `errors` assertion" while it really failed at `processed`, because pytest
prints the sibling assertions as source context. The check was narrowed, and
the negative control `neg:d-checker` (right test, right mutation, wrong
claimed assertion) now reports MISMATCH.

**A finding from `e6`.** `test_stale_upsert_event_for_vanished_file`'s
docstring said the unguarded worker's `sync_one_file` raises
`FileNotFoundError`, which is counted in `state.errors`. It does not. It
records `"file does not exist"` in its report and returns, so with the guard
deleted, `processed` goes 0 → 1 and `errors` stays 0. The docstring is
corrected. The `errors` claim gets its own mutation (`e6b`).

### Mutations

| Id | Edit to `watch.py` (declared count 1 each) |
|---|---|
| M1 | `_schedule_debounced`: `existing.cancel()` → `pass` |
| M2 | `_filter_path`: `{"_templates", "_attachments"}` → `{"_attachments"}` |
| M3 | `_filter_path`: `if abs_path.suffix.lower() != ".md":` → `if False:` |
| M4 | `_worker_loop` delete arm: `if job.abs_path.exists():` → `if False:` |
| M5 | `_worker_loop` upsert arm: `if not job.abs_path.exists():` → `if False:` |
| M6 / M7 / M8 | stale-upsert guard: `state.processed` / `state.skipped_fence_only` / `state.errors` `+= 1` before its `continue` |
| D-fire | `_fire` sleeps 0.5s first: every fire (`all`), or only a superseded timer's (`superseded`: `threading.current_thread() is not pending_timers.get(path)`) |
| D-worker | `_worker_loop` sleeps 1.0s after each `jobs.get()` |

The two D edits are the artificial processing delay: a timer or worker thread
descheduled under load. Neither changes behaviour, only timing.

### Results: final run, 2026-10-22, load average 92-159

| Entry | Test | Mutation | Applied / declared | Result | Failing statement | Verdict |
|---|---|---|---|---|---|---|
| a ×7 | all seven | none | n/a | pass | n/a | OK |
| e1 | burst | M1 | 1/1 | red | `assert not timer.is_alive()` ("superseded debounce timer #0 is still armed") | OK |
| e2 | multi-path | M1 | 1/1 | red | same | OK |
| e3 | template | M2 | 1/1 | red | `assert _pending(state) == {}` ("daily.md was accepted for sync") | OK |
| e4 | non-md | M3 | 1/1 | red | same ("note.md.swp was accepted for sync") | OK |
| e5 | stale delete | M4 | 1/1 | red | `assert count == 1` ("stale delete event removed the row") | OK |
| e6 | stale upsert | M5 | 1/1 | red | `assert state.processed == 0` | OK |
| e6b | stale upsert | M8 | 1/1 | red | `assert state.errors == 0` | OK |
| e7 | stale upsert | M6 | 1/1 | red | `assert state.processed == 0` | OK |
| e8 | vanished + cache | M5 | 1/1 | red | `assert spy_calls == []` ("vanished file triggered sync") | OK |
| e9 | vanished + cache | M7 | 1/1 | red | `assert state.skipped_fence_only == 0` | OK |
| e10 | vanished + cache | M8 | 1/1 | red | `assert state.errors == 0` | OK |
| e11 | vanished + cache | M6 | 1/1 | red | `assert state.processed == 0` | OK |
| neg | stale upsert | M5, claimed `errors` | 1/1 | red at `processed` | n/a | MISMATCH (intended) |

Claim not independently mutated: `test_stale_upsert_event_for_vanished_file`'s
"no DB row" (`count == 0`). Nothing short of writing the file can make the
worker insert a row for a path that does not exist. It stays as defence in
depth and is recorded here rather than claimed as proved.

Under M1 each burst test pays the 10s backstop on the first live superseded
timer (e1 12.3s, e2 15.7s), and the failure is the targeted assertion, not a
backstop message. A passing run never pays it.

### The original false pass, demonstrated

Each pair runs the SAME mutation plus the SAME artificial delay against the
base commit's test (`git show 58e27c6:tests/test_vault_watcher.py`, placed
temporarily as a non-committed module) and against the new test.

| Pair | Mutation + delay | OLD test | NEW test (failing statement) |
|---|---|---|---|
| f1 burst | M1 + D-fire `superseded` | **pass** (false) | red: `assert not timer.is_alive()` |
| f1 burst, idle | M1, no delay | red (`expected 1 collapsed event`) | n/a: the old test catches it only when the machine is idle |
| f2 multi-path | M1 + D-fire `superseded` | **pass** (false) | red: `assert not timer.is_alive()` |
| f3 template | M2 + D-fire `all` | **pass** (false) | red: `assert _pending(state) == {}` |
| f4 non-md | M3 + D-fire `all` | **pass** (false) | red: `assert _pending(state) == {}` |
| f5 stale delete | M4 + D-worker | **pass** (false) | red: `assert count == 1` |
| f6 stale upsert | M5 + D-worker | **pass** (false) | red: `assert state.processed == 0` |
| f7 vanished + cache | M5 + D-worker | **pass** (false) | red: `assert spy_calls == []` |

### Stability

The seven tests were run 5 times serially at load average 44-94, and passed
7/7 each time (1.8-4.0s per run). `tests/test_vault_watcher.py`,
`tests/test_cli_vault_watch.py` and `tests/test_thread_wait.py` were run
once in full: 79 passed. The full suite was not run locally, by design (a
memory-constrained machine); CI is the full-suite evidence.

### Lint

`ruff check` and `mypy src/` are clean. `ruff format --check` fails on
`tests/test_vault_watcher.py` at the base commit too (714 files repo-wide,
and CI runs no format gate), so the file is not reformatted wholesale.
Instead, every line this change adds is format-clean: a script diffs
`ruff format`'s output against the file and reports no hunk overlapping an
added line. That check was shown to fail on a deliberately unwrapped line.
