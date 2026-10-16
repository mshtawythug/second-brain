# Build-watcher `_wait_for` latency budgets → one shared backstop

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Date:** 2026-10-16
**Branch:** `test/build-watcher-wait-backstop` (from `second-brain/master` @ `8900725`)
**Scope:** tests only, plus this plan and the 2026-10-13 session handoff doc. **No production
change.** `src/brain/wiki/build_watcher.py` is over the ceiling (it has a CLAUDE.md row) and is not
touched, so no trail hop is owed.

## Problem

PR #31 (`f6a588d`, plan `docs/plans/2026-10-15-watcher-racing-edit-flake.md`) fixed a flake class in
`tests/test_vault_watcher.py`. In that class, a `_wait_for` call site passes a 1–3 s wall-clock
timeout while it waits for work on another thread. That timeout is a latency assertion, and a starved
machine misses it. PR #31's plan recorded the same pattern in `tests/wiki/test_build_watcher.py` as an
out-of-scope follow-up.

### Call-site census (AST walk at `8900725`, not inherited from the brief)

The census counts `_wait_for` calls (an `ast.Call` whose `func` is `Name("_wait_for")`). It treats a call
as passing a timeout when it has a positional second argument, a `timeout=` keyword, or a `**` splat.

| File | `_wait_for` calls | with explicit timeout | Helper |
|---|---|---|---|
| `tests/wiki/test_build_watcher.py` | **9** | **9** (all `2.0`) | its own `def _wait_for(predicate, timeout=2.0)` |
| `tests/test_cli_vault_watch.py` | **1** | **1** (`5.0`) | imported **from `tests.test_vault_watcher`** |
| `tests/test_vault_watcher.py` | 36 | 0 | PR #31's 10 s backstop |

**A finding the brief did not name.** `tests/test_cli_vault_watch.py:101` imports PR #31's `_wait_for`
from the test module and passes `timeout=5.0`. PR #31's guard scans only `Path(__file__)`, so this
cross-file importer was outside it from the day the guard landed. Its 5.0 is at the floor, not under
it, so it is not a live flake. It is the coverage gap that a per-file guard always leaves, and this
plan closes it.

### Per-site classification: `tests/wiki/test_build_watcher.py`

Every site is a **positive** wait for work on another thread. None waits for absence, and none uses its
timeout as a sleep.

| Line | Test / helper | Waits for | Other thread |
|---|---|---|---|
| 159 | `_start_watcher` | `_FakeObserver.instances` non-empty | `run_watcher` thread constructs the observer |
| 161 | `_start_watcher` | `observer.started` | `run_watcher` thread calls `start()` |
| 528 | `test_on_build_hook_invoked` | `len(received) >= 1` | debounce `threading.Timer` → build → `on_build` |
| 561 | `test_build_failure_does_not_kill_watcher` | `calls >= 1` | debounce timer → raising build |
| 564 | same | `len(received) >= 1` | second debounce → build → `on_build` |
| 605 | `test_refresh_related_skipped_inline_in_watcher` | `spy.call_count >= 1` | debounce timer → build |
| 703 | `test_refresh_singleflight_pending` | `len(call_log) >= 1` | `brain-wiki-refresh-related` daemon thread |
| 715 | same | `len(call_log) >= 2` | same thread drains `refresh_pending` |
| 790 | `test_scoped_polling_does_not_pick_up_new_top_level_dir` | observer constructed **and** started | `run_watcher` thread |

Line 715 is followed at once by `assert len(call_log) == 2`, an upper-bound check. That check does
not wait. A third run would need a third `_schedule_refresh_related()`, and the test never makes one, so
a longer deadline cannot hide a third call.

`test_cli_vault_watch.py:101` is positive too: it waits for the watcher's worker to sync a created note
into `documents`.

**Same class, different API (in scope).** `test_lock_serializes_concurrent_builds` at lines 331 and 337
runs `assert enter_count.acquire(timeout=2.0)` and then `(timeout=3.0)`. These are positive waits for
the debounce timer, then for a build that sleeps 0.3 s and is followed by a coalesced follow-up build.
Both are latency budgets on other threads. They take the shared backstop constant. The `_wait_for`
guard does not cover `.acquire`, and that residue is written down below.

**Not changed (noted, a different defect class).** These sites sleep a fixed window and then assert
that something did **not** happen:

- `test_lock_serializes_concurrent_builds`: `sleep(0.4)`, then `call_count == 2`.
- `test_ignores_quartz_events`, `test_ignores_git_events`, `test_ignores_editor_artifacts`:
  `sleep(0.3)`, then `assert_not_called`.
- `test_scoped_polling_does_not_pick_up_new_top_level_dir`: `sleep(0.2)`.

Under load these can **false-pass**, because a late event lands after the assertion. They cannot
false-fail. A backstop does not apply to them: a longer sleep only widens the window, and it adds wall
time to every run. These are PR #31's "watcher burst/stale-event tests" follow-up, and they stay one.
The `thread.join(timeout=...)` calls in `finally:` blocks are teardown, and none of them gates an
assertion.

## Decision: share the helper rather than mirror it

New module **`tests/thread_wait.py`**. Pytest does not collect it, because the name does not match
`test_*`. That is the same shape as `tests/wiki/manual_timer.py` and `tests/ui_cli_support.py`. It holds
`WAIT_FOR_BACKSTOP_S = 10.0` and `wait_for(predicate, timeout=WAIT_FOR_BACKSTOP_S)`, with PR #31's
docstring and failure message carried over verbatim in substance.

**Why sharing:**

- One definition and one floor. A mirror is a second `2.0` default waiting to happen.
- It *removes* cross-test coupling that already exists: `test_cli_vault_watch.py` imports `_wait_for`
  from another test module today. After this change it imports from a helper module that has no tests,
  no fixtures and no collection side-effects.
- One guard can then cover every importer (see below). A mirror would need a guard per file, and a new
  file would have no guard until someone remembered to write one. That gap is exactly what the
  `test_cli_vault_watch.py` finding shows.

**Coupling check.** The helper is stateless, with no module globals other than the constant, and it
imports only `time` and `collections.abc.Callable`. Nothing about one importer can affect another.

**Importers bind it as `_wait_for`** (`from tests.thread_wait import wait_for as _wait_for`). This keeps
the 36 call sites in the 2,630-line `tests/test_vault_watcher.py` byte-identical, which avoids merge
conflicts with parallel tracks that touch that file. The guard resolves aliases, so the local name
does not matter to it.

## Guard: `tests/test_thread_wait.py`

`test_wait_for_timeout_is_a_backstop_at_every_call_site` moves from `tests/test_vault_watcher.py` (the
same name, so a grep for it still finds it) and is widened:

1. **Floor.** `WAIT_FOR_BACKSTOP_S >= 5.0`, and `wait_for`'s signature default `>= 5.0`. The default is
   checked separately, so a hard-coded literal default can't slip past.
2. **Every importer, discovered rather than listed.** The guard parses every `tests/**/*.py`. In each
   file it finds the names bound to the shared `wait_for`: `from tests.thread_wait import wait_for [as
   X]`, `import tests.thread_wait [as M]` → `M.wait_for`, and `from tests import thread_wait [as M]`.
   It then flags any call through those names that has a positional timeout, a `timeout=` keyword, or
   a `**` splat.
3. **No mirror.** No file other than `tests/thread_wait.py` may `def wait_for` or `def _wait_for`. This
   catches a re-introduced local helper that shadows or replaces the import, which is the pre-PR state
   of `test_build_watcher.py`.
4. **No re-export path.** No file may import a name `wait_for` or `_wait_for` from any module other than
   `tests.thread_wait`. This catches the pre-PR `from tests.test_vault_watcher import _wait_for`, which
   is how the 5.0 got past PR #31's guard.
5. **Non-vacuity.** The discovered importers must include the three known files, and each of them must
   have at least one call. If the discovery logic breaks, the guard goes red instead of passing over
   nothing.

**Exempt:** `tests/test_thread_wait.py` itself, and only from clause 2. Its own unit test of the raise
path has to pass a short timeout, and that file starts no threads. The exemption names one path, so a
new file cannot inherit it.

**Residue, written down.** The guard covers `wait_for`. It does not cover `Semaphore.acquire`,
`Event.wait` or `Queue.get` timeouts, so a new `acquire(timeout=2.0)` would not be caught. This change
fixes the two existing `acquire` sites. Guarding every blocking primitive's `timeout=` across the suite
would also catch legitimate fixed short waits (for example, `fired.wait(timeout=5.0)` in
`test_build_watcher_debounce_clock.py` is already at the floor), and it needs its own design. Out of
scope.

## Tasks

1. Add `tests/thread_wait.py` and `tests/test_thread_wait.py`, with the guard and two behaviour tests:
   it returns as soon as the predicate holds, and it raises the "backstop, not a latency assertion"
   message on its deadline.
2. `tests/test_vault_watcher.py`: delete the local `_WAIT_FOR_BACKSTOP_S`, `_wait_for` and the moved
   guard. Import `wait_for as _wait_for`.
3. `tests/test_cli_vault_watch.py`: import `_wait_for` from `tests.thread_wait` instead of from the test
   module, and drop `timeout=5.0`.
4. `tests/wiki/test_build_watcher.py`: delete the local `_wait_for`, import the shared one, drop all 9
   `timeout=2.0` overrides, and replace the two `acquire(timeout=…)` budgets with
   `WAIT_FOR_BACKSTOP_S`. Reword the module docstring's "short bounded waits" sentence.
5. Mutation-prove the guard (catalog below). Collect load evidence (below).
6. Commit `docs/handoff/2026-10-13-merge-all-handoff.md` (Deliverable 2), with its build-watcher
   follow-up marked as fixed by this PR.

No CHANGELOG entry. Test-only PRs #23 and #31 added none.

## Mutation catalog

Each mutation runs with `PYTHONDONTWRITEBYTECODE=1` and must satisfy five clauses: green unmutated;
the mutation lands its declared count; red; the needle on an `E`/`>` line; restored → green.

| # | Mutation | Count | Needle |
|---|---|---|---|
| M1 | `test_build_watcher.py`: one site regains `timeout=2.0` | 1 | `explicit timeout at` … `test_build_watcher.py:` |
| M2 | same site gains `**{"timeout": 2.0}` | 1 | same |
| M3 | same site gains a positional `2.0` | 1 | same |
| M4 | `thread_wait.py`: `WAIT_FOR_BACKSTOP_S = 4.0` | 1 | `backstop was lowered to 4.0s` |
| M5 (own) | `test_build_watcher.py` re-defines a local `def _wait_for(predicate, timeout=2.0)` | 1 | `defines its own` |
| M6 (own) | `test_cli_vault_watch.py` reverts to `from tests.test_vault_watcher import _wait_for` | 1 | `imports _wait_for from tests.test_vault_watcher` |
| M7 (own) | `test_build_watcher.py` calls `thread_wait.wait_for(..., timeout=1.0)` via `from tests import thread_wait` | 2 | `explicit timeout at` |
| M8 (own) | guard's module-name constant typo'd → discovery finds nothing | 1 | `importers not discovered` |
| M9 (own) | `thread_wait.py`: default becomes literal `2.0` with the constant left at 10.0 | 1 | `default was lowered to 2.0s` |

Counter-proof for M6: at base (`8900725`), PR #31's guard **passes** with that exact import and its
`timeout=5.0` in place, so the hole was real.

## Evidence (2026-10-16)

The probe was not committed. It is a pytest plugin, run against a `git archive 8900725` copy of
`tests/`. It replaced the base file's `_wait_for` with a twin that had a 30 s cap, and it recorded the
elapsed time of every wait by call-site line. It ran on a private sibling database
(`second_brain_test_bwflake_probe`) on the 5434 instance. The burners were 40 tracked `while True: pass`
processes on a 10-core machine. Each one was killed by PID afterwards, and `ps -p` confirmed 0 survivors.

| Run | Load avg | pytest runs | waits | max wait | median (timer/build sites) | ≥ 1.0 s |
|---|---|---|---|---|---|---|
| ambient (other sessions' suites) | 72–84 | 10 | 230 | **0.147 s** (L561, 7 % of 2.0 s) | 0.05–0.07 s | 0 |
| ambient | 84–116 | 15 | 345 | **0.150 s** (L528, 8 % of 2.0 s) | 0.06 s | 0 |
| +40 burners | 119–155 | 15 | 345 | 0.089 s | 0.06 s | 0 |
| +40 burners | 162–194 | 20 | 460 | 0.095 s | 0.06 s | 0 |

**What this shows, and what it does not.**

- **The near-miss distribution is far from the budget on this machine.** The worst wait was 0.150 s, 8 % of
  2.0 s, across 1,380 waits. The waits on the debounce timer and the build (L528/561/564/605) cluster at the 0.05 s
  debounce plus a few ms, and the waits on observer start and the refresh thread are ~0. Unlike the
  vault watcher's racing-edit test (1.945 s of 2.0 s at load ~95, PR #31), nothing on these paths does
  DB round trips or an embed. `build_and_swap` is mocked. Pure CPU burners at load ~190 did not move
  the tail. **No `_wait_for` near-miss was observed, and no flake from this file was reproduced.**
  The change is made on the class argument, not on an observed failure. These are positive waits, a
  passing run never pays the deadline, and the shared backstop makes "starved enough to miss 2.0 s"
  impossible rather than merely unlikely. The flake was latent here, not demonstrated.
- **One unexplained red.** In the first ambient batch, run 10 of 10 (load ~76) ended `1 failed, 22
  passed`. The probe pinned every `_wait_for` at a 30 s cap, so that failure was **not** a `_wait_for`
  deadline. Candidates are the two `acquire(timeout=2.0/3.0)` budgets this PR replaces, or an
  absence-window assertion. The runner kept only pytest's summary line (a probe bug), so the test id
  was lost. Failure capture was added, and 50 further base runs (15 ambient, 35 with burners, load
  84–194) did not reproduce it. It is recorded here so the next person does not attribute it to
  `_wait_for`.
- **The fixed file, 20/20 green** in 20 separate pytest invocations under the 40 burners, at load
  162–194.

## Mutation results (2026-10-16)

The harness ran each row with `PYTHONDONTWRITEBYTECODE=1` against `tests/test_thread_wait.py`. It
checked the five clauses mechanically: unmutated → `7 passed`; the replacement count landed as
declared; rc ≠ 0; the needle on an `E`/`>` line; `git checkout` → green with `git status` clean.
**10/10 PASS.** These are the red lines:

- M1, M2, M3: `E  AssertionError: wait_for called with an explicit timeout at ['wiki/test_build_watcher.py:556']; …`
- M4: `E  AssertionError: wait_for's timeout backstop was lowered to 4.0s; …`
- M5: `E  AssertionError: a test module defines its own wait helper at ['wiki/test_build_watcher.py:48']; …`
- M6: `E  AssertionError: wait helper imported from a module other than tests.thread_wait: ['test_cli_vault_watch.py:30 (imports _wait_for from tests.test_vault_watcher)']`
- M7: `E  AssertionError: wait_for called with an explicit timeout at ['wiki/test_build_watcher.py:557']; …`
- M8: `E  AssertionError: importers not discovered: ['test_cli_vault_watch.py', 'test_vault_watcher.py', 'wiki/test_build_watcher.py']; …`
- M9: `E  AssertionError: wait_for's signature default was lowered to 2.0s; …`
- M10 (added during the run): `_wait_for = functools.partial(wait_for, timeout=2.0)` in place of the
  import → `E  AssertionError: a test module defines its own wait helper at ['wiki/test_build_watcher.py:49']; …`

**Counter-proof for M6:** in the `8900725` archive, PR #31's
`test_wait_for_timeout_is_a_backstop_at_every_call_site` → `1 passed`, with
`tests/test_cli_vault_watch.py:106` carrying `timeout=5.0` through the re-export import.

**Known residue in the guard.** A `functools.partial(wait_for, timeout=…)` bound to any name *other*
than `wait_for`/`_wait_for` is not caught. The same holds for a wrapper function with a new name that
forwards a timeout. The guard catches the shapes that have actually occurred (a local copy, a
re-export, a per-site keyword/positional/splat timeout). Catching a renamed wrapper would mean
data-flow analysis, which is out of proportion for a test-suite convention.

## Verification

- `ruff check`, `mypy src/`: zero.
- `tests/test_thread_wait.py`, `tests/wiki/test_build_watcher.py`, `tests/test_cli_vault_watch.py`,
  `tests/test_vault_watcher.py`, and `tests/test_repo_hygiene_files.py` (it reads `docs/`):
  **109 passed**, on a sibling database.
- No test reads `docs/handoff/2026-10-13-merge-all-handoff.md` or this plan.
- A full suite in `bin/brain-ci`'s environment against the sibling DB was started on the commit that
  adds this section. Its result goes in the team-lead report, not here, because a section cannot
  report a run of the commit that contains it. GitHub CI on the PR is the full-suite evidence of
  record.

## Handoff doc (Deliverable 2)

`docs/handoff/2026-10-13-merge-all-handoff.md` is committed from the main checkout's uncommitted copy,
with three edits:

- The build-watcher follow-up is marked as fixed by this PR.
- The absence-window follow-up names the build watcher's tests as the same class.
- The `#17–#21` date reads 2026-10-12/13 instead of 2026-10-13. `1a1d068` (#17) merged on 2026-10-12.

All 13 merge SHAs in its table were checked against `git log --merges second-brain/master`, and each
names its own PR. Every referenced repo path exists on master. It contains no PII and no golden-corpus
query text.
