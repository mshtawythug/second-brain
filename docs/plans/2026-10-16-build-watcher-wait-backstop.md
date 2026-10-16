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

**Coupling check.** The helper is stateless. Its only module globals are two constants (the backstop and
the poll interval), and it imports only `time` and `collections.abc.Callable`. Nothing about one importer can affect another.

**Importers bind it as `_wait_for`** (`from tests.thread_wait import wait_for as _wait_for`). This keeps
the 36 call sites in `tests/test_vault_watcher.py` (2,630 lines at `8900725`) byte-identical, which
avoids merge conflicts with parallel tracks that touch that file. The guard resolves import aliases and
one level of rebinding, so the local name does not matter to it.

## Guard: `tests/test_thread_wait.py`

`test_wait_for_timeout_is_a_backstop_at_every_call_site` moves from `tests/test_vault_watcher.py` (the
same name, so a grep for it still finds it) and is widened:

1. **Floor.** `WAIT_FOR_BACKSTOP_S >= 5.0`, and `wait_for`'s signature default `>= 5.0`. The default is
   checked separately, so a hard-coded literal default can't slip past.
2. **Every call site, in every test module.** The guard parses every `tests/**/*.py`. A call counts as
   a guarded wait when its callee is any of these:
   - **the underscore names `_wait_for` and `_wait_for_observer`**, as a bare name or as an attribute
     on ANY base (`tvw._wait_for`, `self._wait_for`). They belong to this suite alone.
     `_wait_for_observer` is `test_vault_watcher.py`'s observer-start wait; its floor is pinned in that
     file, and its `timeout` is the FIRST parameter;
   - **the public name `wait_for`, only where it provably resolves to `tests.thread_wait`.** That means
     a bare name bound by `from tests.thread_wait import wait_for [as X]`, or `M.wait_for` where `M` is
     bound by `import tests.thread_wait [as M]` or `from tests import thread_wait [as M]`. Playwright's
     `Locator.wait_for`, `asyncio.wait_for` and `threading.Condition.wait_for` all take a real
     `timeout`, so matching the public name on any base was a false positive (PR #35 re-review);
   - **any of the above through ONE level of rebinding:** `w = _wait_for`,
     `obs = _wait_for_observer`, `w = M.wait_for`, and the module rebind `tw = thread_wait`.

   A guarded wait is flagged if it passes a timeout in any form: positionally, as `timeout=`, or
   through a `*`/`**` splat. A call that passes the helper itself as an argument together with a
   timeout (`functools.partial(wait_for, timeout=2.0)`, under any name) is flagged too.
3. **No mirror.** No file other than `tests/thread_wait.py` may bind `_wait_for` anywhere, or `wait_for`
   at module level, other than by import. No file other than `tests/test_vault_watcher.py` may bind
   `_wait_for_observer` (PR #35 re-review 3). A local copy would carry its own default, and the floor
   pin there inspects only that file's definition. That covers `def`, plain, annotated and walrus assignment,
   and every name inside a tuple, list or starred target. A `def wait_for` METHOD, such as a page
   object's, is not a mirror. This catches a re-introduced local helper that shadows or replaces
   the import, which is the pre-PR state of `test_build_watcher.py`.
4. **No re-export path.** No file may import `_wait_for` from any module other than
   `tests.thread_wait`, or import `wait_for` from another test module (`tests.*` or a relative import).
   `from asyncio import wait_for` is not the helper and is left alone. This catches the pre-PR `from tests.test_vault_watcher import _wait_for`, which
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
| M10 (own) | `test_build_watcher.py`: `_wait_for = functools.partial(wait_for, timeout=2.0)` in place of the import | 1 | `defines its own` |
| M11 (review 1a) | after the import, annotated: `_wait_for: Any = functools.partial(wait_for, timeout=2.0)` | 1 | `defines its own` |
| M12 (review 1a) | after the import, tuple target: `_wait_for, _spare = functools.partial(…), None` | 1 | `defines its own` |
| M13 (review 1b) | `w = _wait_for` after the import, and a site calls `w(…, timeout=1.0)` | 2 | `explicit timeout at` |
| M14 (review 1c) | `from tests import test_vault_watcher as tvw`, and a site calls `tvw._wait_for(…, timeout=1.0)` | 2 | `explicit timeout at` |
| M15 (review 1c) | a site calls `observer._wait_for(…, timeout=1.0)` (the `self` form: an attribute on an object) | 1 | `explicit timeout at` |
| M16 (own) | `quick = functools.partial(_wait_for, timeout=2.0)`, and a site calls `quick(…)` | 2 | `explicit timeout at` |
| M17 (review 3) | `test_cli_vault_watch.py`: `_wait_for_observer(timeout=5.0)` back at one site | 1 | `explicit timeout at ['test_cli_vault_watch.py:` |
| M18 (review 3) | same, positional: `_wait_for_observer(5.0)` | 1 | `explicit timeout at ['test_cli_vault_watch.py:` |

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

**Known residue in the guard.** *(Rewritten in the PR #35 review round. The earlier paragraph listed
the wrong gaps: it said a renamed wrapper that forwards a timeout is not caught. It is caught, because
the wrapper's inner `wait_for(p, t)` passes a timeout. It also said a renamed `partial` is not caught,
and that is now closed.)* **After the re-review narrowing, this is exactly what is and isn't matched.** Matched: the two
underscore names on any base; the public `wait_for` only through a `tests.thread_wait` import binding
or a module alias; one level of rebinding of either; and a `partial` that binds a timeout to any of
them. **Not matched, by design:** a `wait_for` on any other base or from any other import:
`page.locator(…).wait_for(timeout=5000)`, `asyncio.wait_for(c, timeout=1.0)`,
`threading.Condition().wait_for(pred, timeout=…)`, and `from asyncio import wait_for`. Their
`timeout` is a real API argument. The cost of that narrowing is one new gap, listed first below.

These are the real gaps, each needing data-flow analysis that is out of proportion for a test-suite
convention:

- **The real helper reached under its public name through an unrecognised base or a star import:**
  `holder.wait_for(p, timeout=1.0)`, where `holder` is some object carrying the helper, or
  `from tests.thread_wait import *`. The guard can't tell that base from Playwright's, so it doesn't
  guess.

- **Dynamic access:** `importlib.import_module("tests.thread_wait").wait_for`, `__import__`,
  `getattr(module, "wait_for")`, `globals()["_wait_for"]`.
- **A rebinding chain deeper than one level** (`a = _wait_for; b = a; b(p, timeout=1.0)`).
- **A rebinding through a parameter default** (`def go(w=wait_for): w(p, timeout=1.0)`). This is one
  level deep, but it binds through a function signature, not an assignment (PR #35 re-review 3).
- **The helper stored under a new name through a call or a container:** `w = staticmethod(wait_for)`
  in a class body, then `C.w(p, timeout=1.0)`; or `waits = {"w": wait_for}`, then
  `waits["w"](p, timeout=1.0)`.
- **A wrapper that hard-codes a short deadline without calling the helper,** i.e. a new hand-rolled
  poll loop. That is the original disease, and no call-site guard can see it.
- **A timeout reaching the helper through `threading.Thread(target=wait_for, kwargs={...})`**, where
  the timeout is a dict key rather than a keyword.
- **Other blocking primitives** (`acquire`, `Event.wait`, `Queue.get`), as recorded above.

## Review round (PR #35, reviewer @ `8e9e921`)

There were seven findings. All are fixed in the commit that adds this section.

1. **Guard evasions** (MEDIUM):
   - an annotated or tuple-target `partial` mirror;
   - a plain rebind `w = _wait_for`;
   - an attribute call through a test module (`tvw._wait_for`) or an object (`self._wait_for`);
   - dynamic access.

   The first three are closed (clauses 2 and 3 above). Dynamic access is recorded as residue.
2. **Wrong residue paragraph** (LOW). It is rewritten under "Known residue in the guard".
3. **`_wait_for_observer(timeout=5.0)` ×2 in `tests/test_cli_vault_watch.py`** (LOW). Both are positive
   waits for the watcher thread to start its observer. The overrides are dropped, and the guard now
   covers `_wait_for_observer` by name. That costs one extra name and one positional index, and it uses
   the same rule.
4. **Nits:** the live "2,630-line" digit is now bound to `8900725`; the coupling sentence names both
   constants; the catalog table gains the M10 row; the handoff title date is corrected.

**Mutation results, round 2: 18/18 PASS on all five clauses** (M1–M18; the M1–M10 needles are unchanged
from round 1). The harness restores from in-memory backups and checks that the tree is byte-identical,
not that git is clean, because the fixes were uncommitted when it ran. These are the new red lines:

- M11, M12: `E  AssertionError: a test module defines its own wait helper at ['wiki/test_build_watcher.py:49']; …`
- M13, M14: `E  AssertionError: wait_for called with an explicit timeout at ['wiki/test_build_watcher.py:557']; …`
- M15: `… explicit timeout at ['wiki/test_build_watcher.py:556']`
- M16: `… explicit timeout at ['wiki/test_build_watcher.py:49']`. The `partial` line is flagged.
- M17: `… explicit timeout at ['test_cli_vault_watch.py:98']`
- M18: `… explicit timeout at ['test_cli_vault_watch.py:169']`

**Counter-proofs** ran on a `git archive 8e9e921` copy, with the two observer overrides pre-dropped so
that M17/M18 have a site to land on. **M11–M18 each stay GREEN under the old guard**, so every hole the
review named was real.

One correction from the run: the first M11/M12 form *replaced* the import line. That tripped the old
guard's non-vacuity clause and made it go red for an unrelated reason. Re-formed to keep the import
(the reviewer's shape), both stay green at `8e9e921` and go red at the fix.

## Re-review round (PR #35, reviewer and auditor @ `b53b74d`)

**Reviewer:**

1. **False positives on third-party `wait_for`** (LOW). Matching the public name on any base flagged
   `page.locator(…).wait_for(timeout=5000)` and `asyncio.wait_for(…, timeout=1.0)`, and the re-export
   check flagged `from asyncio import wait_for`. These were latent: no test file makes such a call
   today, and the browser files use `wait_for_function`/`wait_for_selector`. The fix narrows the match,
   as written in clauses 2–4.
2. **`_wait_for_observer` rebind and `partial` not covered** (LOW). Fixed: `_helper_index` returns a
   per-name positional index, so `partial(_wait_for_observer, 1.0)` and `obs = _wait_for_observer;
   obs(1.0)` are both flagged.

**Auditor:** the handoff's Claude Code MCP item and its "Done #1" version wording are corrected (see
"Handoff doc" below). The residue now states exactly what is and isn't matched, including that
`threading.Condition().wait_for(pred, timeout=…)` is not flagged, by design.

**Source-level probes** (`test_guard_leaves_third_party_wait_for_alone`,
`test_guard_still_flags_the_helper_under_narrowed_matching`) run synthetic snippets through
`_scan_source`, the per-module scan that the guard itself runs over every file.

- **Five must stay green:** Playwright's locator, `asyncio.wait_for`, `from asyncio import wait_for`,
  `threading.Condition().wait_for`, and a page-object `def wait_for` method.
- **Nine must stay red:** an imported keyword timeout, a module-alias positional timeout, a module
  rebind, an underscore name on any base, the observer `partial` (keyword and positional), the
  observer rebind, a top-level public mirror, and a public re-export from a test module.

**Mutations, round 3: 25/25 PASS on all five clauses.** M1–M18 are unchanged, plus these new rows:

| # | Mutation | Count | Red line |
|---|---|---|---|
| M19 | `quick = functools.partial(_wait_for_observer, timeout=1.0)`; `observer = quick()` | 1 | `explicit timeout at ['test_cli_vault_watch.py:98']` |
| M20 | `obs_wait = _wait_for_observer`; `observer = obs_wait(1.0)` | 1 | `explicit timeout at ['test_cli_vault_watch.py:99']` |
| M21 | guard: public `wait_for` matched on any attribute base | 1 | `false positive on a third-party wait_for (playwright_locator)`, and in a single-row re-run (threading_condition) |
| M22 | guard: bare `wait_for` matched without an import | 1 | `… (asyncio_bare_import): {'overrides': …}` |
| M23 | guard: re-export exemption for non-test modules removed | 1 | `… (asyncio_bare_import): {'reexports': …}` |
| M24 | guard: public-name mirror check not limited to top level | 1 | `… (page_object_method): {'mirrors': …}` |
| M25 | guard: rebinding skips index-0 (observer) waits | 1 | `true positive not flagged as overrides (observer_rebind)` |

M21–M25 mutate the guard itself. They show that each probe can fail, and that each narrowing rule is
load-bearing.

**False-positive rows** (FP1: a Playwright `locator.wait_for(timeout=5000)` helper added to
`test_build_watcher.py`; FP2: `from asyncio import wait_for` plus `await wait_for(c, 1.0)`) **stay green
at the fix.**

**Counter-proofs** on a `git archive b53b74d` copy:

- M19 and M20 stay GREEN, so the observer holes were real;
- FP1 and FP2 go RED, with `wait_for called with an explicit timeout at
  ['wiki/test_build_watcher.py:50']` and `…:51`, so the false positives were real.

**Browser files under the guard.** The guard's `_scan_source` was run over all 26 files that mention
Playwright, the `browser` mark, `wait_for_selector` or `wait_for_function`: 0 overrides, 0 mirrors,
0 re-exports. Across `tests/`, the only exact `.wait_for(` attribute call is the guard file's own exempt
raise-path test. This PR changes no browser file, and Playwright is not installed in this worktree's
venv, so the browser tests themselves were not executed. They are outside the default marker set, and
nothing here can change their behaviour.

## Re-review 3 (PR #35, reviewer @ `2671f53`)

1. **A local `_wait_for_observer` mirror was not flagged** (LOW). The mirror check now treats every
   underscore wait as a mirror name. A per-module `defines` set exempts each wait's one legitimate home:
   the helper file for `wait_for`, and `tests/test_vault_watcher.py` for `_wait_for_observer`. A
   must-stay-red probe (`observer_mirror`) is added.
2. **Residue:** a rebinding through a parameter default is now listed, here and in the guard's module
   docstring. No fix is needed.

**Mutations, round 4: 28/28 PASS on all five clauses** (the full catalog, M1–M28, re-run against the
edited tree), and FP1/FP2 stay green. These are the new rows:

| # | Mutation | Count | Expected red |
|---|---|---|---|
| M26 | `test_cli_vault_watch.py` defines `def _wait_for_observer(timeout: float = 1.0)` | 1 | `defines its own wait helper at ['test_cli_vault_watch.py:` |
| M27 | guard: the `test_vault_watcher.py` exemption is removed (shows it is load-bearing and precise) | 1 | `defines its own wait helper at ['test_vault_watcher.py:148']` |
| M28 | guard: the mirror check is narrowed back to `_wait_for` only (shows the new probe can fail) | 1 | `true positive not flagged as mirrors (observer_mirror)` |

Counter-proof: on a `git archive 2671f53` copy, M26 stays green, so the hole was real. These are the red
lines:

- M26: `E  AssertionError: a test module defines its own wait helper at ['test_cli_vault_watch.py:37']; …`
- M27: `… at ['test_vault_watcher.py:148']; …`
- M28: `E  AssertionError: true positive not flagged as mirrors (observer_mirror): …`

## Verification

- `ruff check`, `mypy src/`: zero.
- `tests/test_thread_wait.py`, `tests/wiki/test_build_watcher.py`, `tests/test_cli_vault_watch.py`,
  `tests/test_vault_watcher.py`, and `tests/test_repo_hygiene_files.py` (it reads `docs/`):
  **109 passed** in round 1, **123 passed** in round 3 (the 14 new probes) and **124 passed** in round 4
  (the `observer_mirror` probe), on a sibling database.
- No test reads `docs/handoff/2026-10-13-merge-all-handoff.md` or this plan.
- A full suite in `bin/brain-ci`'s environment against the sibling DB was started on the commit that
  adds this section. Its result goes in the team-lead report, not here, because a section cannot
  report a run of the commit that contains it. GitHub CI on the PR is the full-suite evidence of
  record.

## Handoff doc (Deliverable 2)

`docs/handoff/2026-10-13-merge-all-handoff.md` is committed from the main checkout's uncommitted copy.
In the PR #35 audit round, its "Needs the user" section was rewritten to match the machine state an ops
agent produced on 2026-10-16: S4 done, the daemon upgrade to 0.3.0, sibling DBs and merged branches
cleaned up, and migration 028 on prod. Each fact was re-checked read-only (`launchctl list`, the uv
receipt, `ps`, `ls -l`, `git ls-remote`, `gh pr view`, `SELECT` on `schema_migrations`). The two counts
that can't be re-checked after the fact (95 DBs, 24 branches) are attributed to the ops record. The
disk figure is the measured 93 %. The title date is now 2026-10-16. The round-1 edits:

- The build-watcher follow-up is marked as fixed by this PR.
- The absence-window follow-up names the build watcher's tests as the same class.
- The `#17–#21` date reads 2026-10-12/13 instead of 2026-10-13. `1a1d068` (#17) merged on 2026-10-12.

**Correction (audit @ `b53b74d`):** the claim above that every fact was re-checked read-only
was false for one item. "Restart open Claude Code sessions so brain-mcp respawns" assumed a Claude Code
MCP config that does not exist. The only brain MCP config is Claude Desktop's, and its `command`
pointed at the main checkout's editable `.venv` (at `6a82db2`), bypassing the shims. Team-lead
repointed it to `~/.local/bin/brain-mcp`, backing it up to `.bak-20261016T161548`. The doc now records
that under Done #8, with the revert, and lists "restart Claude Desktop" under the user's items. Both
were verified by loading the JSON and printing only the `command` field (current: the uv tool; backup:
the checkout's `.venv`). Done #1 is reworded too: `6a82db2`'s `pyproject.toml` already says 0.3.0, and
it was the pre-upgrade tool (v0.2.1) that was staler.

All 13 merge SHAs in its table were checked against `git log --merges second-brain/master`, and each
names its own PR. Every referenced repo path exists on master. It contains no PII and no golden-corpus
query text.
