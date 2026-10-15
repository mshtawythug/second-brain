# `brain ui` watcher status line — design (A9, UI half)

**Status:** design, 2026-10-15. **Parent spec:** `docs/specs/2026-08-10-wiki-to-ui-consolidation-design.md`
§8.5, A9 / Q8. **Plan:** `docs/plans/2026-10-15-ui-watcher-status.md`.

## 1. What A9 asked for, and what shipped

> **A9.** … Minimum: `brain doctor` and the UI status line must report "watcher not running —
> external edits will not appear in search". Q8.

The `brain doctor` half shipped in phase 8 (PR #27, `e1fc28e`) as `src/brain/doctor_watcher.py`.
The UI half never shipped: nothing in `brain ui` tells the reader that the search results they
are looking at may be stale because nobody is syncing the vault. That is the case §8.5 calls a
correctness problem rather than a nuisance — "the user edits in Obsidian, searches in `brain ui`,
and gets stale results with no indication why."

This spec covers only the UI half. It does **not** revisit whether the watcher becomes a hard
dependency of `brain ui` (the other half of A9's question) — the minimum is what is built.

## 2. Detection: reuse, do not re-derive

`doctor_watcher.is_vault_watcher(cmd)` is the one definition of "this process is the vault-sync
watcher", over `maintenance.snapshot_processes()` (`ps -axo command=`). The UI reuses both. A
second tokeniser in `ui/` would be a second rule to keep true.

One addition, in `doctor_watcher.py`: `watcher_state(processes=None) -> WatcherState`, returning
one of three strings:

| state | meaning |
|---|---|
| `running` | some process is `brain vault sync … --watch` |
| `not_running` | the snapshot is non-empty and contains no watcher |
| `unavailable` | the snapshot is **empty** — detection did not work on this machine |

**Why an empty snapshot means "unavailable", not "not running".** `snapshot_processes` returns `[]`
when `ps` is missing, times out, or raises (`OSError`/`SubprocessError` are swallowed). A real
snapshot can never be empty: the process asking — `brain ui` itself — is always in it. So `[]` is
a reliable signal that detection failed, and reporting it as `not_running` would resolve the
ambiguity in the direction that is merely *probably* right. The ledger's "UNKNOWN" status
(`ledger_status.js`) made the same call for the same reason.

**Doctor reads the same rule off macOS.** `watcher_doctor_check`'s non-darwin branch used to
re-derive the scan (`snapshot` + `any(is_vault_watcher …)`), which made it report an unreadable
process list as "not running" while the UI said "unknown". It now calls `watcher_state` and maps
`running` → ok, `not_running` → the existing WARN, `unavailable` → a distinct WARN saying the
process list could not be read so the status is unknown (audit ruling, 2026-10-15). Its remedy is
"check with `ps`; if no `brain vault sync --watch` is running, start one" — not a bare start
command, because the watcher may be running and nothing prevents a second instance. Off macOS the
two surfaces therefore **apply the same rule**. They can still show different things for a moment:
the UI answers from a 15 s cache, and if its first check fails it shows an "unknown" placeholder
until a real answer lands.

**Process scan on every platform, including macOS.** The doctor check on macOS asks whether the
`com.brain.watcher` plist is *installed* and leaves liveness to its `daemons` line. The UI line has
no `daemons` line beside it, and its sentence is "watcher not **running**", so the UI asks the
direct question everywhere. The launchd wrapper `bin/_brain-watcher-fg` `exec`s
`brain vault sync --watch --vault …`, so a launchd-supervised watcher is visible to the scan.
Because doctor asks a different question on macOS, the two surfaces can **legitimately disagree
there, in two ways**:

* **installed but crashed** — the UI says "not running"; doctor's `vault watcher` line is ok,
  because the plist is installed (the crash is reported on its `daemons` line instead);
* **not installed but started by hand** — the UI is silent (a watcher process is running); doctor
  WARNs that `com.brain.watcher` is not installed.

**Known residue (not fixed here):** the scan does not compare the watcher's `--vault` with the
UI's vault. A watcher for a different vault reads as `running`. The doctor check has the same
property; fixing it means parsing `--vault` with its default (`$BRAIN_VAULT_PATH`, the config
default), which is a separate ruling.

## 3. The endpoint — `GET /api/watcher`

```json
{"state": "running" | "not_running" | "unavailable"}
```

* **Minimal shape, by requirement.** The snapshot holds every process's full command line, and the
  watcher's own carries `--vault <path>`. Nothing from the snapshot is echoed: not the matching
  line, not a pid, not a count. The response is one enum. A test asserts the exact key set and
  that a sentinel string planted in the fake snapshot never appears in the body.
* **Auth: inherited, not re-implemented.** The route is under `/api/`, so `RequestGuardMiddleware`
  demands the token on a non-loopback bind before routing, `TrustedHostMiddleware` validates Host,
  and `SecurityHeadersMiddleware` adds `cache-control: no-store`. It is a `GET`, so read-only mode
  does not affect it. A test proves a token-protected server refuses it without the header.
* **No database.** Like `/api/health`, it answers when Postgres is down — the watcher question is
  independent of the DB.
* **Never blocks the event loop.** `ps` is a subprocess with a 10 s timeout; the route runs the
  probe through `starlette.concurrency.run_in_threadpool`. Pinned by a route test whose detector
  records whether it ran on the event-loop thread (`asyncio.get_running_loop()` succeeds only
  there); a refactor to a plain `probe.state()` call passes ruff, mypy and every other test
  (review round 3).

### 3.1 Cost and cadence

`ps -axo command=` costs on the order of tens of milliseconds and forks a process. Two bounds:

1. **Server-side TTL cache** — `ui/watcher_probe.py`'s `WatcherProbe` caches the last state for
   **15 s** (monotonic clock, injected for tests). However many tabs poll, and however often a
   reader switches windows, the server forks `ps` at most once per TTL. A lock makes concurrent
   requests share one snapshot rather than racing to fork several.
2. **Client cadence** — the page asks once at boot, then every **60 s** *while the tab is
   visible*, and again when the tab becomes visible or the window regains focus (the realistic
   case — "I was in Obsidian, now I'm back"; the same reasoning §8.4 used for C3). A hidden tab
   does not poll.

The probe lives on `UiContext` (`watcher_probe`, default-constructed), which is the app's existing
injection seam; tests pass a probe over a fake detector instead of patching anything. Its lock is
injectable too, so the concurrency test observes "the second request reached the lock" as an
acquire-attempt count instead of sleeping and hoping.

**Ruling — a detector that raises caches nothing.** The exception propagates and the route answers
a logged 500. The real detector cannot raise for an environmental reason: `snapshot_processes`
already converts a missing, slow or failing `ps` into `[]`, i.e. `unavailable`, which *is* cached
like any answer. An exception is therefore a bug, and caching an invented state for it would hide
the bug behind a plausible status line. The retry rate is bounded by the page's own cadence, and
the lock is released either way (both pinned by a test).

## 4. The status line

**Where:** in the top bar, `.topbar-right`, before the read-only badge. Not a banner: `.shell`'s
height is `calc(100vh - 2.75rem)`, so a row inserted below the top bar would push the page into a
vertical scroll. The top bar is also the one piece of chrome visible in every tab and at every
breakpoint (the rail is a drawer below 1100 px).

**What it says** — the decision is a pure function, `js/watcher_status.js`'s `watcherLine(payload)`,
executed under node the way `ledger_status.js` is:

| state | visible text (> 1100 px) | visible text (≤ 1100 px) | tone |
|---|---|---|---|
| `running` | *(nothing)* | *(nothing)* | — |
| `not_running` | Watcher not running — external edits will not appear in search | Watcher off | `warn` |
| `unavailable` | Watcher status unknown — external edits may not appear in search | Watcher unknown | `muted` |
| unrecognised / malformed | treated as `unavailable` | | |

* **Silent when healthy.** A line that says "watcher running" forever is chrome the reader learns
  to ignore, which defeats the line on the day it matters.
* **`unavailable` is shown, quietly.** Hiding it would claim a health the server could not check.
  It is styled muted, not as a warning: on a platform without `ps` it is permanent, and a permanent
  warning cries wolf (the doctor's ruling R8-5, same reasoning).
* **A failed request keeps the last answer — once there is one.** If `/api/watcher` errors, the
  line keeps its last state; "the server is unreachable" is already toasted by boot's
  `/api/health` call and is a different problem. But if the **first** check fails there is no
  last answer, and keeping "nothing" would be silence, which is exactly what `running` looks like.
  So a failed first check renders `unavailable` (review finding, 2026-10-15) — as a
  **placeholder, not an answer**: it does not advance the sequence below, so an older check still
  in flight (boot's, when a focus check fails first) paints when it lands. Counting the placeholder
  as an answer made "unknown" stick for a minute over a real `not_running` (review round 2).
* **Only a change of state touches the DOM.** Rewriting identical text into a live region can
  re-announce it, so a watcher that stays down would be read out every minute. The painted state
  is kept in `data-state` and an unchanged answer returns early.
* **Overlapping checks are sequenced.** Focus and `visibilitychange` often fire together, and
  across the server's 15 s cache boundary two overlapping requests can get *different* answers in
  either arrival order. Each check takes a ticket and an answer older than the last one painted is
  dropped. (The first cut argued sequencing was unnecessary because both requests hit the same
  cache; that is false exactly at a TTL boundary.)

**Narrow viewports — measured, not assumed.** The element carries two spans: the full sentence and
a short form. Below **1100 px** (the existing breakpoint where the rail becomes a drawer) the short
form shows and the full sentence is *visually* hidden — not `display: none` — so it stays the live
region's accessible text at every width; the short span is `aria-hidden`.

The first cut used the 780 px breakpoint and let the line shrink with an ellipsis. A visual check
against the demo sandbox found both wrong: at 900 px the full sentence squeezed the READ-ONLY badge
onto two lines, and at 375 px the line rendered as "Wa…" — present, announced, unreadable, and
green in every test. So the line is `flex: none` (it never gives way), the short forms are as short
as the meaning allows ("Watcher off", "Watcher unknown"), and the tab strip — which already scrolls
inside itself — absorbs the squeeze. Measured at 375 px read-only: the line is 91 px wide and
uncut, the tab strip keeps 54 px, the page has no horizontal scroll. The badge wrapping onto two
lines at 375 px predates this change (measured with the line removed). The browser test asserts
"not truncated" and "no horizontal scroll" at 375 px (with and without the badge) and at 900 px.

### 4.1 Accessibility

* **Text, not colour.** Every state the line shows is a sentence; the tone only adds colour and a
  border. A colour-blind reader, or a high-contrast theme, loses nothing.
* **`role="status"` + `aria-live="polite"`.** A change of state is announced without interrupting.
  The live region is **in the markup from first paint and never `hidden`** — a live region that
  is inserted or un-hidden at the same moment its text changes is announced unreliably, so the
  element is always present and simply empty while the watcher runs.
* **Contrast.** The `warn` tone uses `--flag-draft`, already used at `--t-meta` for the read-only
  badge; the `muted` tone uses `--ink-muted`. No new colour tokens.

## 5. Files

| file | change |
|---|---|
| `src/brain/doctor_watcher.py` | `WatcherState`, `WATCHER_STATES`, `watcher_state()` |
| `src/brain/ui/watcher_probe.py` | **new** — `WatcherProbe` (TTL cache + lock, injected detector and clock) |
| `src/brain/ui/context.py` | `UiContext.watcher_probe` field |
| `src/brain/ui/routes_meta.py` | `watcher` route |
| `src/brain/ui/app.py` | `Route("/api/watcher", …, methods=["GET"])` |
| `src/brain/ui/static/js/watcher_status.js` | **new** — pure `watcherLine()` |
| `src/brain/ui/static/js/watcher.js` | **new** — DOM wiring + polling |
| `src/brain/ui/static/js/main.js` | import + `wireWatcherStatus()` in `boot()` |
| `src/brain/ui/static/index.html` | the `#watcher-status` live region |
| `src/brain/ui/static/css/components.css` | `.watcher-status` rules + the ≤ 1100 px swap |
| `.github/workflows/ci.yml` | `tests/test_ui_browser_watcher.py` in the explicit browser roster |

No file in CLAUDE.md's over-ceiling table is touched; every touched file stays under 800.

Every guard and test above is mutation-proved: the plan's catalog holds **31** mutations, each
passing all five clauses (passes unmutated, lands once, red, needle on a `>`/`E` line,
restored green).
