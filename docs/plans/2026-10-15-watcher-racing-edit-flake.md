# Watcher racing-edit flake — root cause and fix

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Date:** 2026-10-15
**Branch:** `test/watcher-racing-edit-flake` (from `second-brain/master` @ `e1fc28e`)
**Scope:** `tests/test_vault_watcher.py` only. **No production change** — `src/brain/vault/watch.py`
(over the ceiling, CLAUDE.md row) is untouched, so no trail hop is owed.

## Symptom

`tests/test_vault_watcher.py::test_racing_edit_during_sync_is_not_masked_by_body_cache` failed in two
independent full-suite runs on 2026-10-15, on two unrelated branches, each with
`AssertionError: predicate never became true` from `_wait_for`'s 2.0 s wall-clock deadline, at
machine load average 50–140. It passed in isolation.

## Root cause — test-only timing, not a production race

The test ran the real `run_watcher` in a background thread and waited three times, each with a
2.0 s wall-clock budget, for work happening on two other threads: the debounce timer, then the
worker running `sync_one_file` (DB round trips plus an embed). That budget is a latency assertion.
On a starved machine it is simply not met.

### Evidence

All probes ran against a private sibling database (`second_brain_test_wflake`) on the 5434 test
instance. The probe (not committed) is the test's own body, instrumented to time each of the three
waits with a 30 s cap and to record `state.errors`, `state.skipped_fence_only`, and the load average.

| Run | Load avg | n | `fired` max | `cached` max | `final` max | Correct end state |
|---|---|---|---|---|---|---|
| ambient | 33–43 | 20 | 0.574 s | 1.043 s | **1.852 s** | 20/20 |
| +40 tracked busy-loop PIDs | 89–99 | 15 | 0.344 s | **1.945 s** | 0.724 s | 15/15 |

- A single phase reached **97 % of its 2.0 s budget** at load ~95, and the failing suite runs were at
  load up to 140. The tail is load-driven, so it crosses 2.0 s from there.
- **35/35** probe iterations ended in the correct state. The cache was primed with the *indexed*
  body, the second event was **not** skipped as fence-only (`skipped_fence_only == 0`), the DB caught
  up to the raced body, and `errors == 0`. Nothing indicates a production race.
- In a code read, the cache is written only by the single worker thread. The baseline it caches
  (`SyncReport.body_baseline`) comes from the text `sync_one_file` read *before* the embed, so no
  interleaving of a racing save can baseline the raced body. A racing save's own event is debounced
  and queued behind the in-flight job, and it is processed after the baseline is cached.
- The unchanged test passed 10/10 at load ~95. Each phase's tail is short of the budget there, so
  the flake needs the heavier load the failing suite runs had. Shrinking the deadline in a throwaway
  copy to reproduce the message deterministically was attempted but was blocked: the shared 5434
  instance crashed (a backend exited with code 2 at 20:10:26 UTC, then a fast shutdown). The
  near-miss distribution above is the evidence instead.

(One probe iteration falsely "hung". That was a probe bug: the probe module lacked this file's autouse
`_reset_fake_observers` fixture, so iteration 1 polled iteration 0's state. It is recorded here so
nobody re-chases it.)

## Fix

1. **Target test → deterministic.** It now drives the worker synchronously through the file's
   existing house helper `_run_events_through_worker`, the same pattern as
   `test_move_within_vault_preserves_incoming_backlinks`. The helper gained a `state=` keyword so a
   second drain reuses the body cache the first one primed, as the long-lived worker does. There are
   no timers, no threads, and no wall clock. The race itself is still staged for real inside
   `sync_one_file` by `_EditDuringEmbed`. The test also asserts directly on
   `skipped_fence_only == 0`, which names the defect instead of inferring it from a DB timeout.
   It also pins that the second drain really ran against the first drain's state: it keeps the
   return value, asserts `again is state`, and asserts `state.processed == 2`. Without that, a
   helper that ignored `state=` would cold-start the second drain, sync unconditionally, and pass
   vacuously even with the pre-fix bug in place. Review R3 found this hole at `513bdc2`.
2. **Siblings → backstop.** At base (`e1fc28e`) the file had **39** `_wait_for` calls, and **35**
   of them passed an explicit 1–3 s timeout. **3** of those 35 were in the target test and went
   with its threaded body. The other **32** are sibling sites with the same weakness. (Re-derive
   with an AST walk of `git show e1fc28e:tests/test_vault_watcher.py`.) `_wait_for` now defaults
   to `_WAIT_FOR_BACKSTOP_S = 10.0`, and every per-site override is removed. This is the
   `_wait_for_observer` precedent from 2026-08-21.
   **Why a longer deadline is the right fix for these:** every one is a *positive* wait for
   something that will happen on another thread, so a passing run returns as soon as the predicate
   holds and never pays the deadline. Those tests exist to exercise the threaded path (debounce
   coalescing, shutdown drain, overflow, signal handlers, new-directory scheduling). Making them
   synchronous would delete the subject under test. Waiting on a production-side event instead of
   polling would still need a deadline, so the only non-determinism left is the deadline itself,
   and a backstop removes it. The failure message now says "backstop, not a latency assertion".
3. **Guard.** `test_wait_for_timeout_is_a_backstop_at_every_call_site` pins the default (≥ 5 s) and
   AST-scans this file for any `_wait_for(…)` call that passes a timeout. Pinning the default alone
   would not hold: one `timeout=2.0` at a call site brings the flake back. A `**{"timeout": …}`
   splat (`keyword.arg is None`) counts as an override too. Review R1 found that hole at `513bdc2`.

**Correction to the commit record.** Commit `513bdc2`'s message says "All 35 sibling _wait_for
call sites passed 1-3s". The true sibling figure is **32**: 35 is the total of explicit-timeout
calls, including the target test's 3. The earlier draft of this section made the same error ("All
35 `_wait_for` call sites in the file"). History is not rewritten (no amend, no force-push), so
the correction lives here.

### Not changed (noted for follow-up)

- `test_burst_of_modifies_collapses_to_one_sync`, `test_multi_path_bursts_yield_one_sync_per_path`,
  and the stale-delete/upsert tests use a fixed `sleep` and then assert that something did **not**
  happen. Under load they can **false-pass** (an extra event arrives late). They cannot false-fail,
  so they are a different defect class from this flake.
- The fence-only cache tests (`test_fence_only_write_skips_resync` … `test_overflow_full_sync_clears_body_cache`)
  test worker logic and could also move to `_run_events_through_worker`. With the backstop they no
  longer flake, so this is optional.
- `tests/wiki/test_build_watcher.py:98` has its **own** `_wait_for(predicate, timeout=2.0)`, with
  **9** call sites, all of which pass an explicit `timeout=2.0`. This is the same latency-budget
  class, on the build-watcher's threads, and it is out of scope for this branch. (Re-derive with an
  AST walk counting `_wait_for` calls with a positional timeout, a `timeout=` keyword, or a `**`
  splat.)

## Mutation catalog

Each row was run with `PYTHONDONTWRITEBYTECODE=1`, and records all five clauses: green unmutated;
the mutation lands its declared count; red; the needle appears on an `E`/`>` line; restored → green.

| # | Mutation (target) | Count | Expected red at |
|---|---|---|---|
| M1 | `watch._cache_after_sync`: always `_refresh_body_cache(...)` (the pre-Task-3.2 disk re-read) | 1 | `skipped_fence_only == 0` ("masked as a fence-only write") |
| M2 | `sync._sync_one`: `report.body_baseline = None` (forces the disk-read fallback) | 1 | same |
| M3 | `watch._worker_loop`: drop the `_cache_after_sync(...)` call on the upsert path | 1 | `note in state.body_cache` |
| M4 | test: restore one `timeout=2.0` at a `_wait_for` call site | 1 | guard: "explicit timeout at lines [...]" |
| M5 | test: `_WAIT_FOR_BACKSTOP_S = 2.0` | 1 | guard: "backstop was lowered to 2.0s" |
| R1 | test: one call site gains `**{"timeout": 2.0}` (a splat the pre-review guard missed) | 1 | guard: "explicit timeout at lines [...]" |
| R3 | test helper: `if state is None: state = _WatcherState()` → `state = _WatcherState()` (ignores `state=`) | 1 | `again is state` |
| R3+M1 | R3 **and** M1 together (the vacuous-pass combination the review found) | 1+1 | `again is state` |

**Results (2026-10-15): 5/5 PASS on all five clauses.** Each mutation landed exactly 1×, and the
tree was restored after each (`git status` shows no `src/` change). These are the red lines:

- M1, M2: `E  AssertionError: the racing edit's event was masked as a fence-only write`
- M3: `>  assert note in state.body_cache`
- M4: `E  AssertionError: _wait_for called with an explicit timeout at lines [1673]; …`
- M5: `E  AssertionError: _wait_for's timeout backstop was lowered to 2.0s; …`

**Review round (2026-10-15): R1, R3 and R3+M1 added, and all eight rows re-run against the edited
test. 8/8 PASS on all five clauses.** R3+M1 landed 1+1, across two files. New red lines:

- R1: `E  AssertionError: _wait_for called with an explicit timeout at lines [1673]; …`
- R3, R3+M1: `>  assert again is state, "second drain did not reuse the primed state"`

Counter-proof for R1: with the pre-review guard line restored (`k.arg == "timeout"`) and the R1
splat applied, the guard test **passes**. The hole was real, and the review fix is what closes it.
R3 and R3+M1 were green at `513bdc2` per the reviewer's own run, and are red now.

## Verification

- `ruff check`, `mypy src/`: zero.
- `tests/test_vault_watcher.py`: 55/55 passed.
- The fixed test ran **30/30 green**, in 30 separate pytest invocations under 40 tracked busy-loop PIDs.
  Load average was 165–267 throughout, above the 50–140 at which the old test failed. All burner
  PIDs were killed by PID afterwards and confirmed dead.
- The full suite ran in `bin/brain-ci`'s environment (`CI`/`GITHUB_ACTIONS` exported, colour/pytest
  variables scrubbed, bare `pytest` with no extra flags) against the sibling DB. `bin/brain-ci`
  hard-codes `second_brain_test`, which other sessions hold. **Result: 9256 passed, 3 skipped,
  320 deselected, rc 0, coverage 94.72%** (run started after the 5434 restart at 20:19 UTC).
  That run was at `513bdc2`.
- **Review round (the commit after `513bdc2`):** ruff 0, `mypy src/` 0, and `tests/test_vault_watcher.py`
  55/55 on the edited file. A fresh local full suite was started and then **stopped on purpose**
  (SIGTERM to its own PID, rc 143) at the coordinator's direction. The delta from `513bdc2` is this
  one test file plus this plan, and GitHub CI on the new head is the full-suite evidence. No local
  full-suite result is claimed for this commit.
