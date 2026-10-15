# `brain ui` watcher status line — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Spec:** `docs/specs/2026-10-15-ui-watcher-status-design.md`. **Branch:** `feat/ui-watcher-status`.
**Goal:** close A9's UI half — `brain ui` reports "watcher not running — external edits will not
appear in search", reusing `brain.doctor_watcher` detection.

Every task is TDD: the test lands first and is seen red for the stated reason, then the code.

## Tasks

### T1 — `watcher_state()` in `doctor_watcher.py`
* Test (`tests/test_doctor_watcher.py`): `running` for a snapshot containing a watcher;
  `not_running` for a non-empty snapshot without one; `unavailable` for `[]`; every near-miss
  command line is `not_running` — one module constant, `_NEAR_MISSES`, parametrizes both this test
  and the `is_vault_watcher` one (a hand-copied subset had covered 2 of 6; audit, 2026-10-15).
* Doctor's non-darwin branch calls `watcher_state` (unavailable → a distinct "unknown" WARN), so
  doctor and the UI agree off macOS; regression test for the unreadable-snapshot case.
* Code: `WatcherState = Literal[...]`, `WATCHER_STATES` tuple, `watcher_state(processes=None)`.

### T2 — `WatcherProbe` (`src/brain/ui/watcher_probe.py`, new)
* Tests (`tests/test_ui_watcher_probe.py`, `nodb`): first call detects; a second call inside the
  TTL does not re-detect; a call at/after the TTL does; the default detector is `watcher_state`.
* Code: class with `detect`, `clock`, `ttl_s` (default 15.0), a `threading.Lock`, `state()`.

### T3 — `GET /api/watcher`
* Tests (`tests/test_ui_routes_watcher.py`, `nodb`, real `create_app` via `TestClient`):
  each of the three states round-trips as `{"state": …}` with **exactly** that key set; a sentinel
  vault path inside the fake snapshot never appears in the body (route over a probe built on a real
  `watcher_state` over a fake process list); a token-protected context refuses it without the
  token (403 `invalid_token`) and serves it with it; `cache-control: no-store`; the route needs no
  database (a connection factory that raises `psycopg.OperationalError` on ENTRY, so even
  opening a connection fails — `_FakeConn` raising only on `execute` let an open-and-ignore
  route pass; auditor finding); POST is 405.
* Code: `UiContext.watcher_probe`, `routes_meta.watcher` via `run_in_threadpool`, the `Route`.

### T4 — `watcherLine()` (`static/js/watcher_status.js`, new, pure)
* Tests (`tests/test_ui_watcher_status.py`, `nodb`, node harness like
  `tests/test_ui_ledger_status.py`): the three states' text/short/tone; `running` is empty;
  `null`/garbage payloads fall back to `unavailable`; the JS constants equal
  `brain.doctor_watcher.WATCHER_STATES`; the `not_running` sentence contains the spec's exact
  phrase "external edits will not appear in search".
* Add `watcher_status.js` and `watcher.js` to `JS_ORDER` in `tests/test_ui_static_behaviour.py`.

### T5 — wiring: `watcher.js`, `index.html`, `main.js`, CSS
* Browser tests (`tests/test_ui_browser_watcher.py`, `browser` marker, booting the REAL
  `index.html` with `/api/**` stubbed — so the `main.js` wiring is proven by behaviour):
  `not_running` shows the full sentence on a wide viewport; `running` shows nothing; the live
  region exists with `role=status` + `aria-live=polite` and is not hidden while empty;
  `unavailable` shows the unknown sentence; at 375 px the short text shows, the full sentence is
  still the region's accessible text, and the body has no horizontal scroll; a `visibilitychange`
  back to visible re-asks the server and the line follows the new state; a failed `/api/watcher`
  leaves the previous text in place; a window `focus` re-asks; a hidden tab does not ask; a
  visible tab asks again at 60 s and not before (Playwright's fake clock).
* Add the module to `ci.yml`'s explicit browser roster (byte order: last).

### T5b — review round 1 (2026-10-15)
Findings on `e221235`, each closed test-first:
* the minute poll skips a hidden tab — fake-clock test (`setInterval(check, …)` survived before);
* concurrent requests at an expired TTL share one detection — deterministic two-thread test over
  an injected, acquire-counting lock (the lock was unfalsifiable before); a raising detector
  caches nothing and releases the lock (ruling recorded in the spec §3.1);
* a failed FIRST check says "unknown" (silence read as running);
* an unchanged answer leaves the live region untouched (MutationObserver sees 0 mutations, and
  a positive control sees a real change);
* overlapping checks are sequenced — a held older response released after a newer one is dropped.

Round 2 (on `bccac42`):
* the "unknown" placeholder no longer advances the sequence — a regression test holds boot's
  check, fails a newer focus check with 500, then lands boot's `not_running`, which must paint;
* the raising-detector test asserts directly that the injected lock is not held after the raise;
  its acquire also times out loudly, so a leaked lock is a red test, never a hung suite.

Round 3 (on `dba6c19`):
* detection runs off the event loop — a route test whose detector records whether
  `asyncio.get_running_loop()` succeeds (it does only on the loop thread).
* doctor's non-darwin branch delegates to `watcher_state` — an unreadable snapshot is a distinct
  "unknown" WARN, as in the UI; the near-miss list is one constant shared by both scan tests.

Round 4 (on `e6176b5`):
* the unknown WARN's remedy is check-then-start (asserted), since the watcher may be running and
  there is no single-instance guard; the spec and user guide name BOTH macOS divergences.

Round 5 (on `d661e2a`):
* the unknown WARN is also tested on the production path (`processes=None`, a stubbed failing
  snapshot), not only through the explicit `processes=[]` seam.

### T6 — verify
`ruff check`, `mypy src/`, `bin/brain-ci` green; `brain demo` sandbox visual check with a throwaway
`BRAIN_VAULT_PATH` (never the live vault or prod DB).

## Mutation catalog

Each was applied, seen red with its needle on a `>`/`E` line, and reverted to green
(`PYTHONDONTWRITEBYTECODE=1`); each anchor occurs exactly once in its file. All 32 passed all
five clauses on 2026-10-15, run in a copy of the working tree (M13/M18 re-run and M19 added after
the layout fix; M14 re-anchored, M20–M25 added in review round 1, M26 from the audit, and M14/M23 re-anchored with
M27–M28 added in review round 2, M29–M30 in round 3, M31 in round 4, M32 in round 5 — M1/M2, the only DB-backed entries, were re-run after the shared test Postgres crashed
mid-run (20:10 UTC); their crash-window results were connection timeouts, not verdicts — M21 first went red on the
test's harness precondition rather than on `calls == 1`, so the test was restructured until it
failed on its claim).

| # | file | mutation | red test (needle) |
|---|---|---|---|
| M1 | `doctor_watcher.py` | delete `if not procs: return "unavailable"` | `test_an_empty_snapshot_is_unavailable_not_not_running` (`unavailable`) |
| M2 | `doctor_watcher.py` | `return "running"` → `return "not_running"` | `test_watcher_state_is_running_when_a_watcher_is_in_the_snapshot` (`'running'`) |
| M3 | `ui/watcher_probe.py` | freshness check → `if False:` | `test_a_call_inside_the_ttl_reuses_the_snapshot` |
| M4 | `ui/watcher_probe.py` | `< self._ttl_s` → `< self._ttl_s * 1000` (never expires) | `test_a_call_at_the_ttl_detects_again` |
| M5 | `ui/routes_meta.py` | payload gains `"vault": str(ctx.cfg.vault_path)` | `test_no_command_line_leaks_into_the_body` (`vault`) |
| M6 | `ui/app.py` | delete the `/api/watcher` `Route` | `test_each_state_round_trips_as_one_enum` (`404`) |
| M7 | `js/watcher_status.js` | A9 phrase → "external edits are delayed" | `test_not_running_says_the_a9_sentence_as_a_warning` |
| M8 | `js/watcher_status.js` | unrecognised state falls back to `RUNNING` | `test_anything_unrecognised_is_unknown_never_healthy` |
| M9 | `js/watcher_status.js` | `NOT_RUNNING = "stopped"` | `test_the_javascript_states_are_the_python_states` (`stopped`) |
| M10 | `js/main.js` | delete `wireWatcherStatus();` from `boot()` | `test_a_stopped_watcher_is_reported_in_words` (`Timeout`) |
| M11 | `index.html` | drop `aria-live="polite"` from `#watcher-status` | `test_the_line_is_a_polite_live_region_that_is_never_hidden` |
| M12 | `js/watcher.js` | delete the `visibilitychange` listener | `test_coming_back_to_the_tab_asks_again` (`Timeout`) |
| M13 | `css/components.css` | delete the ≤1100 px visually-hidden `.watcher-full` rule | `test_a_narrow_top_bar_gets_the_short_line…[phone]` (`laid out`) |
| M14 | `js/watcher.js` | every failure repaints the placeholder, even after an answer | `test_a_failed_check_keeps_the_last_answer` |
| M15 | `js/watcher.js` | delete the window `focus` listener | `test_refocusing_the_window_asks_again` (`Timeout`) |
| M16 | `js/watcher.js` | `if (!document.hidden) check();` → `check();` | `test_a_hidden_tab_does_not_ask` |
| M17 | `js/watcher.js` | `POLL_MS = 30_000` | `test_a_visible_tab_asks_again_every_minute` (`before the minute`) |
| M18 | `css/components.css` | delete the ≤1100 px `.watcher-short { display: inline; }` | `test_a_narrow_top_bar_gets_the_short_line…[phone]` (`is_visible`) |
| M19 | `css/components.css` | `flex: none` → the first cut's `min-width: 0` + ellipsis | `test_a_narrow_top_bar_gets_the_short_line…[phone-read-only]` (`cut off`) |
| M20 | `js/watcher.js` | `setInterval(checkIfVisible, …)` → `setInterval(check, …)` | `test_a_hidden_tab_skips_the_minute_poll` (`polled`) |
| M21 | `ui/watcher_probe.py` | `with self._lock:` → `if True:` | `test_concurrent_requests_at_an_expired_ttl_share_one_detection` (`calls`) |
| M22 | `ui/watcher_probe.py` | a raising detector is caught and cached as `"unavailable"` | `test_a_raising_detector_caches_nothing_and_releases_the_lock` (`DID NOT RAISE`) |
| M23 | `js/watcher.js` | a failed FIRST check stays silent (placeholder render deleted) | `test_a_failed_first_check_says_unknown_not_nothing` (`Timeout`) |
| M24 | `js/watcher.js` | drop the unchanged-state early return in `render()` | `test_an_unchanged_answer_leaves_the_live_region_untouched` (`unchanged`) |
| M25 | `js/watcher.js` | drop the `ticket < painted` guard | `test_a_stale_answer_cannot_overwrite_a_newer_one` (`full`) |
| M26 | `ui/routes_meta.py` | open a connection in `watcher` (`with ctx.connect(): pass`) | `test_it_answers_when_the_database_cannot_even_be_opened` (`OperationalError`) |
| M27 | `ui/watcher_probe.py` | `with self._lock:` → `self._lock.__enter__()` + `if True:` (lock leaks on raise) | `test_a_raising_detector_caches_nothing_and_releases_the_lock` (`leaked`) |
| M28 | `js/watcher.js` | the placeholder advances `painted` (the round-1 shape) | `test_the_unknown_placeholder_does_not_shadow_an_older_real_answer` (`Timeout`) |
| M29 | `ui/routes_meta.py` | `await run_in_threadpool(ctx.watcher_probe.state)` → `ctx.watcher_probe.state()` (detection on the event loop) | `test_detection_never_runs_on_the_event_loop` (`event loop`) |
| M30 | `doctor_watcher.py` | the non-darwin `unavailable` branch deleted (falls through to "not running") | `test_off_macos_an_unreadable_process_list_warns_unknown_not_stopped` (`could not be read`) |
| M31 | `doctor_watcher.py` | the unknown branch's remedy dropped | `test_off_macos_an_unreadable_process_list_warns_unknown_not_stopped` (`remedy`) |
| M32 | `doctor_watcher.py` | `if state == "unavailable" and processes is not None:` (the unknown branch only on the test seam) | `test_off_macos_a_failing_ps_snapshot_warns_unknown_on_the_production_path` (`could not be read`) |

## Risks
* **Live-region timing in headless Chromium** — asserted structurally (attributes + not hidden),
  not by a screen reader; announcement itself is not machine-verified.
* **Wrong-vault watcher** reads as running — recorded residue (spec §2).
