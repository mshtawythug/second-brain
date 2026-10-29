# RM-11 — Ingest tab, phase 2: the paste front end

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-11 in `docs/specs/2026-10-23-roadmap-completion-design.md` (wave 3).
**Design:** `docs/specs/2026-10-23-brain-ui-ingest-tab-design.md` (the "Ingest spec"): §8, §3.4 items 1, 2,
4, 5 and 6 for paste, §7.5, the §9.4 paste rows, and the paste half of the §10 "After" row.
**Builds on:** RM-10 (PR #73, merged at `a873074`): `GET /api/ingest/config`, `POST /api/ingest/paste`,
`GET /api/jobs`, `GET /api/jobs/{id}?after=`, `POST /api/jobs/{id}/cancel`, `health.ingest_enabled`.
**Branch:** `feat/rm-11-ingest-paste-ui`, from `second-brain/master` at `a873074`.

## Goal

Paste ingest is usable from the browser, with live progress, cancel and a link to the new document.

## Files

New (each well under 400 lines):

| File | Responsibility |
|---|---|
| `src/brain/ui/static/js/jobs.js` | Kind-agnostic job client, reused by the Agent tab (RM-18b): `submitJob`, `pollJob` (backoff and visibility), `cancelJob`, `pollDelay` (pure), and the `sessionStorage` list of the ids this tab submitted, read and written inside `try`. |
| `src/brain/ui/static/js/ingest.js` | The tab: loads `/api/ingest/config` the first time the tab is shown, renders the disabled reason or the paste form, validates the body size client-side, submits, and draws the job panel. |
| `src/brain/ui/static/js/ingest_view.js` | Pure DOM builders for one job row (state, phase, `<progress>`, outcome, notices, cancel). Split out of `ingest.js` so each module has one reason to change and stays small; RM-13's file mode reuses it. |
| `src/brain/ui/static/css/ingest.css` | The tab's styles, built only on the existing tokens; every selector scoped under `.ingest-pane`. |
| `tests/test_ui_browser_ingest.py` | The §9.4 paste rows, hermetic (`page.route`). |

Touched:

- `static/index.html`: `#tab-ingest` stops being `class="shell deferred-tab"`; it holds the semantic form,
  a `<fieldset>`, a `<label>` on every control, the help line that separates paste from `+ New` (§3.3),
  the disabled-reason notice, the job list and a polite live region. The `ingest.css` link joins the
  roster.
- `static/js/main.js`: imports and calls `wireIngest()` (order-free: it owns `#tab-ingest`, subscribes to
  nothing).
- `tests/test_ui_static_behaviour.py`: `ingest.css` joins `CSS_ORDER`; `jobs.js`, `ingest_view.js` and
  `ingest.js` join `JS_ORDER`; a boot-wiring check for `wireIngest()` with its mutation entries.
- `tests/test_ui_tabs_static.py`: the Ingest pane is no longer a deferred pane; Agent still is.
- `.github/workflows/ci.yml`: `tests/test_ui_browser_ingest.py` in the byte-ordered browser roster.
- Docs: `docs/cli-reference.md` (the `brain ui` section), `README.md`,
  `docs/guides/brain-ui-on-your-phone.md` (ingest is unavailable over `--lan`), `CHANGELOG.md`
  (Unreleased), the backlog doc row, and Appendix B's RM-11 row (follow-up commit, the only spec edit).
- Memory: `types.md` (`JobSnapshot`, `JobRegistry`, `IngestOutcome`, `ProgressEmbedder`; the ticket's
  scope line) and `cli.md` (the tab), marked "PR #N, verify merged".

No Python under `src/` changes, so no file in the CLAUDE.md ceiling table is touched.

## Design choices the ticket leaves open (resolved here)

1. **A withheld result shows no title and no link.** RM-10's lens nulls `title`, `vault_path` and
   `document_id` and sets `title_withheld: true` while `serve_confidential_titles` is off. The panel then
   says "Saved. Its title is withheld on this server (confidential)." with no "Open in Notes" link.
   §7.5 says the panel **may** show confidential titles; it does not oblige the client to put back what
   the server withheld. The panel never fills a title from the form's input: a dedup skip can land on a
   different, existing confidential document, so the typed title could also misname it. **Job rows are
   labelled by kind and submit time only, never by the typed title**, for the same reason and because
   `sessionStorage` holds ids only.
2. **One announcer, not a live job list.** Spec §8 asks for an `aria-live="polite"` job list *and* for
   each state change to be announced once, not on every poll. A live list re-announces every progress
   update. So the job list is a plain `<ol>`, and a separate `role="status"` region receives one sentence
   per state change (queued, running, the outcome, failure, cancel). That meets the stated intent.
3. **Config loads lazily, on the first show of the tab**, together with the resumption of remembered
   jobs. Boot is unchanged for the Notes tab, and the existing hermetic browser suites (which stub no
   ingest route) keep their request sets.
4. **Polling** (§4.3): 1 s while running, 3 s once the job is older than 30 s or when
   `document.visibilityState === "hidden"`; stops at a terminal state. A network failure keeps polling at
   3 s and shows "cannot reach the server; still trying". A 404 `job_unknown` ends the row with the §4.6
   sentence ("outcome unknown … re-submitting is safe") and forgets the id.
5. **"Open in Notes"** is a real link, `/?id=<uuid>` (the URL state `store.js` reads), so middle-click and
   copy work. A plain click switches to the Notes tab and calls `openNote(id)`.
6. **Client-side size check.** The body's UTF-8 length (`TextEncoder`) is checked against
   `config.limits.paste_body_bytes` before submitting; the server's 413 stays the authority.
7. **Browser tier is hermetic**, per the ticket ("stubbed with `page.route`"). The spec's §9.4 sentence
   about one cancel test on the real worker thread is covered by the manual end-to-end pass against the
   test DB (a gated paste cancelled from the real page), not by CI. Deviation recorded here and in the PR.
8. **Remembered jobs**: at most 20 ids are kept (spec R7: the client renders the 20 most recent); a
   terminal job stays remembered so a reload shows its outcome until the server evicts it (404).

## Review round 1 (rev-74, at `4d6ffc8`)

Seven findings, all fixed in new commits:

1. **The withheld-title test could not see the typed title come back.** It typed "withheld", a word
   inside the withheld sentence. The fake now picks its scenario from a `scenario:<name>` marker in the
   BODY, never the title. The test types "Zorblax Quillfeather" and asserts it is absent from
   `#ingest-jobs`, from every text the live region held, and from `sessionStorage`. A control checks
   that it was sent. rev-74's two-site mutation is M14 below.
2. **A stale answer could un-settle a finished row.** `apply` now ignores everything once a row is
   settled. `cancelRequested` is sticky: it never goes from true to false. The poller is stopped the
   moment the merged record settles. There are three tests, one per defence, each holding a request with
   `page.route`: a stale poll after a 200 cancel (the stop), a stale 202 after the job finished (the
   guard), and a stale poll after a 202 (the sticky flag, counted by a MutationObserver of Cancel buttons).
3. **An ordinary eviction read as a lost outcome.** `jobs.js` now remembers which ids this tab saw
   settle (`markSettled` / `wasSettled`, the `brain-ui-jobs-settled` key; `forget` clears both). A 404
   for a settled id is eviction: the id is forgotten and the row dropped silently. The §4.6 sentence is
   kept for ids never seen settled.
4. **Reduced motion is now tested.** Under `reduced_motion="reduce"`, the indeterminate bar's computed
   `appearance` is `none` (the CSS rule's value), and under `no-preference` it is not (the control).
5. **Announcements.** A resumed job's first answer is drawn but not announced (`quiet` on the pending
   record). `announce()` clears the region and sets the sentence on the next animation frame, so two
   identical sentences are both read. Both are tested through a recorder of every text the region holds.
6. **`maxlength="200"` dropped** from `#ingest-title`. The server's 400 `invalid_title` reaches
   `#ingest-error`, and a test types 201 characters and checks both.
7. **Docstring rot** in `check_hidden_toggles_survive_the_cascade`: the digit is gone (the count lives in
   the registry entry), and the three defensive elements are named.

**The browser module was split**, because these tests took it to 822 lines, past the 800-line
test-file precedent (PR #67). The fake, the fixtures and the recorders moved to
`tests/ui_ingest_harness.py`, following the shape of `tests/ui_graph_harness.py`. The flows stay in
`tests/test_ui_browser_ingest.py`, and the round-1 tests are in
`tests/test_ui_browser_ingest_lifecycle.py`, which joins the `ci.yml` roster.

### The audit's gaps (aud-74, at `4d6ffc8`), folded into the same round

Each gap below is a mutation aud-74 actually ran, and each SURVIVED.

- **G1, the unreachable path.** A poll that answers 503 must show "Cannot reach the server; still
  trying.", keep polling, and let the row settle. New test.
- **G2, the indeterminate bar.** A new `writing` scenario embeds first and then writes, so the bar has
  had a value and must drop it. The new test asserts `:indeterminate` and the "Writing" phase. The
  reduced-motion test (rev-74 #4, which also closes G3) reuses this scenario.
- **G4, focus.** Focus must return to the row when Cancel goes away. While a cancel is in flight, the
  button is now `aria-disabled` rather than `disabled`, because a `disabled` button drops keyboard focus
  to `<body>` before it is removed. A second click is ignored while `aria-disabled` is set, and
  `ingest.css` styles that state. The cancel test focuses Cancel, presses Enter, and asserts that the
  `li.job` holds focus once the row settles.
- **G5.** `jobs_unavailable` joins the disabled-tab parametrize. A config that answers 500 shows "Could
  not load the ingest settings", and the form stays disabled.
- **G6.** The cancel test asserts the "Ingesting: search may be slower" line while the job runs. A
  non-409 cancel failure raises an error toast and offers Cancel again.
- **G7.** `test_poll_delay_backs_off` reads `POLL_FAST_MS`, `POLL_SLOW_MS` and `SLOW_AFTER_MS` from
  `jobs.js` instead of restating them. `remember`, `kindLabel` and `headline` are no longer exported.
- **G8.** The manual pass deviation is recorded under "Deviations" below.

## Deviations

1. **The CI browser tier is hermetic** (`page.route`), as the ticket text asks. The Ingest spec's §9.4
   real-worker-thread cancel is covered by the manual end-to-end pass (step 4 below), not by CI.
2. **The manual pass ran on the test DB (port 5434), not the `brain demo` sandbox (55433)** that the
   Ingest spec's §9.5 and the roadmap's §2.1 item 3 name. The real-thread cancel needed a scratch
   `BRAIN_HOME`, so that `brain ui` would read a `.env` pointing at a throwaway database and vault, and
   a gated 400-chunk paste. The test DB is just as synthetic, starts empty, and is off 55432. The
   database (`second_brain_test_rm11_e2e`) and vault were created for the pass and hold only synthetic
   text. Ruling by the coordinator: record it, and do not re-run on the demo.

## Tests (TDD: red first)

- `tests/test_ui_tabs_static.py`: the Ingest pane is not `deferred-tab` and carries a form; the Agent pane
  still is deferred.
- `tests/test_ui_static_behaviour.py`: roster additions (the existing guards then cover the new files:
  single `innerHTML`, `--ink-faint`, `var()` definitions, the `[hidden]` cascade, cross-sheet overlaps);
  `check_the_ingest_tab_is_wired_into_boot`, mutation-proven by its registry entries.
- `tests/test_ui_browser_ingest.py`:
  - paste → progress (`<progress>` with `value`/`max`) → "Added" → the link opens the note in Notes;
  - paste again → "Already in your brain" with a link;
  - `mirror_repaired`, `empty` and the secret-findings / egress / summary / graph notices render as text;
  - a withheld result → no link, the withheld sentence, and the typed title nowhere in the panel;
  - cancel a running job → "Cancelled. Nothing was written."; a 409 → the server's message;
    `cancel_too_late` → its sentence;
  - a failed job → the server's fixed message;
  - a reload with a remembered id that is now 404 → the "outcome unknown" sentence;
  - read-only and non-loopback configs → the form is disabled and the reason is shown;
  - an oversize body is refused client-side with no request;
  - keyboard only: Tab reaches every control, Enter on the submit button submits;
  - both themes: the panel renders and the page raises nothing;
  - markup-looking server strings are drawn as text;
  - `pollDelay` executed in the page.
- `tests/test_ci_workflow.py` needs no edit: it discovers the browser roster by marker, so the new module
  is red until `ci.yml` lists it.

## Mutation proofs (in `git archive` copies only)

Each mutation ran in a fresh `git archive HEAD` copy, never the worktree. The harness applies one
exact-string replacement that must match exactly once, proves it applied (the re-read file equals the
mutated text and differs from the original), sets `PYTHONDONTWRITEBYTECODE=1`, purges `__pycache__`, pins
`PYTHONPATH` to the copy's `src`, and checks by realpath that `brain.ui.app.static_dir()` resolves inside
the copy. It refuses any pytest exit code other than 0 or 1, and an unmutated baseline must pass first.

**The first run proved nothing, and that is recorded rather than dropped.** `addopts` deselects the
`browser` marker, so every named browser test was deselected (exit code 5) and every mutation read as
"killed" while the baseline read as red. The harness now passes `-m browser` and refuses exit code 5.

Run at `bd7e552`: **13 of 13 killed**, baseline passed.

| # | Mutation | Killed by |
|---|---|---|
| M1 | `wireIngest()` not called from `boot()` | `test_a_paste_shows_progress_then_added_and_opens_the_note` (and the static `ingest-wired-in-boot` guard) |
| M2 | the withheld branch skipped | `test_a_withheld_result_has_no_link_and_never_the_typed_title` |
| M3 | a withheld result shows a title | `test_a_withheld_result_has_no_link_and_never_the_typed_title` |
| M4 | polling never stops at a terminal state | `test_polling_stops_at_a_terminal_state` |
| M5 | the Cancel click ignored | `test_cancel_a_running_job` |
| M6 | no client-side size check | `test_an_oversize_body_is_refused_before_any_request` |
| M7 | the browser roster entry dropped from `ci.yml` | `tests/test_ci_workflow.py` (2 tests) |
| M8 | a 404 is not treated as "gone" | `test_a_job_the_server_lost_says_the_outcome_is_unknown` |
| M9 | the progress bar's `max` never set | `test_a_paste_shows_progress_then_added_and_opens_the_note` |
| M10 | no live-region announcement | `test_the_live_region_announces_the_outcome` |
| M11 | the form enabled on a server that refuses ingest | `test_a_server_that_cannot_ingest_renders_the_tab_disabled` (both reasons) |
| M12 | a 409's reason dropped | `test_a_refused_cancel_shows_the_servers_reason` |
| M13 | "Open in Notes" follows the link instead of switching tabs | `test_a_paste_shows_progress_then_added_and_opens_the_note` |

The two static guards added to `tests/test_ui_static_behaviour.py` (`ingest-module-imported`,
`ingest-wired-in-boot`) are mutation-proven by that file's own registry on every run.

## End-to-end (real server, test DB)

`brain ui --port 8797 --no-open` with a scratch `BRAIN_HOME` whose `.env` points at
`second_brain_test_rm11_e2e` on 5434, a scratch vault, `BRAIN_EMBEDDER=arctic`, and the graph off. A
Playwright script drove the real page with synthetic text:

1. A three-chunk paste (summary skipped) → **Added**, with the title, an *Open in Notes* link and
   `_ingested/manual/synthetic-quarkle-digest.md`.
2. The same body again → **Already in your brain**, same link.
3. A confidential paste (`serve_confidential_titles` off) → **Added**, "Saved. Its title is withheld on
   this server", and no link.
4. A 400-chunk transcript, cancelled from the page while "Embedding 0 of 400 chunks" on the **real
   worker thread** (the spec's §9.4 real-thread cancel) → **Cancelled. Nothing was written.**
5. *Open in Notes* on job 1 → the Notes tab, the note open, `?id=<uuid>` in the address bar.
6. A reload in the dark theme → all four rows come back with their outcomes. No page errors.

Afterwards the DB held exactly two documents (3 + 2 chunks, `metadata = {"ingested_via": "ui"}`) and the
vault exactly two mirrors: the cancelled transcript wrote nothing.

## Risks

- The hermetic stubs could drift from RM-10's wire shape. Mitigation: the stub payloads are built from the
  same keys `routes_jobs.snapshot_payload` and `ingest_service._result_payload` emit, and the manual pass
  runs the real server.
- `static/js/` module count grows by three; each is added to `JS_ORDER` in the same change.
