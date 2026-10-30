# RM-18b — Agent tab front end and the model-output hardening pass

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-18b in `docs/specs/2026-10-23-roadmap-completion-design.md` (wave 4), under the parent
"RM-18 — The Agent tab" and its decisions A1–A10.
**Builds on:** RM-18a (PR #79, merged at `2f83ea3`): `GET /api/ask/config`, `POST /api/ask`, ask jobs
on the shared registry, the citation lens keyed on `serve_confidential_bodies` (RM-18a.D1), ask jobs
unlisted and kept 10 minutes (A10). RM-11 (PR #74): `js/jobs.js`, the Ingest tab's patterns.
**Branch:** `feat/rm-18b-agent-frontend`, from `second-brain/master` at `2f83ea3`.

## Goal

Ask a question in the browser, watch plan → retrieve → reflect → synthesize progress, cancel it, and
read a cited answer whose `[n]` markers open the cited note in the Notes tab. Model text reaches the
page as text only.

## Files

New (each well under 400 lines):

| File | Responsibility |
|---|---|
| `src/brain/ui/static/js/ask.js` | The tab: loads `/api/ask/config` on first show, shows the disabled reason or enables the form, submits through `jobs.js`, polls, cancels, resumes the asks this browser tab submitted, and asks `GET /api/jobs` whether an ingest job is ahead of a queued ask. |
| `src/brain/ui/static/js/ask_view.js` | Pure DOM builders for one turn: question, step log, phase line, `<progress>`, Cancel, and the answer. Holds the hardening rules: `answerNodes` (the citation-marker parser) and the UUID check. |
| `src/brain/ui/static/css/ask.css` | The tab's styles, on the existing tokens only; every selector scoped under `.agent-pane`. |
| `tests/ui_ask_harness.py` | The hermetic browser harness: a scripted `/api/ask/config`, `/api/ask`, `/api/jobs` and `/api/jobs/{id}` fake, and the malicious payloads. Shares `static_origin` / `browser` with `tests/ui_ingest_harness.py`. Exports the wire keys it emits. |
| `tests/test_ui_browser_agent.py` | The five acceptance criteria in a real browser (hermetic, `page.route`). |
| `tests/test_ui_ask_wire_contract.py` | The route test (5434): a fake `ChatJson` through `UiContext`, the real job payload, and the proof that the harness's stub has the real payload's keys and that a real document id passes the JS UUID pattern. |

Touched:

- `static/index.html`: `#tab-agent` stops being `class="shell deferred-tab"`; it holds the form (a
  `<fieldset>`, a `<label>` on every control), the disabled-reason notice, the turn list and a polite
  live region. `ask.css` joins the stylesheet roster.
- `static/js/jobs.js`: an optional `scope` on the remembered-id store (see D2). Ingest's keys are
  unchanged.
- `static/js/main.js`: imports and calls `wireAsk()` (order-free: it owns `#tab-agent`).
- `static/css/components.css`, `css/layout.css`, `css/tokens.css`: the `.deferred-tab` / `.deferred`
  rules and the prose about them go, because no element carries the class any more (dead CSS).
- `src/brain/ui/__init__.py`: its docstring still said Ingest and Agent are deferred.
- `tests/test_ui_static_behaviour.py`: `ask.css` in `CSS_ORDER`; `ask_view.js`, `ask.js` in `JS_ORDER`;
  the sink guard `check_ask_has_no_html_sinks` beside `check_single_innerhtml`, with mutation rows; the
  boot-wiring guard for `wireAsk()`; the `.deferred-tab vs .shell` overlap row goes with the CSS.
- `tests/test_ui_tabs_static.py`: the Agent pane is a real tab now.
- `tests/test_ui_security.py`: the CSP is pinned to a literal on the shell and on `/api/ask/config`.
- `.github/workflows/ci.yml`: `tests/test_ui_browser_agent.py` in the byte-ordered browser roster.
- Docs: `docs/cli-reference.md` (`brain ui`), `docs/guides/brain-ui-on-your-phone.md` (no Agent over
  `--lan`), `CHANGELOG.md` (Unreleased), the backlog row, Appendix B's RM-18b row (follow-up commit).
- Memory: `cli.md`, `project_brain_ui_static_tree.md`, the `MEMORY.md` index line ("PR #N, verify merged").

No Python module under `src/brain/` other than the `ui/__init__.py` docstring changes, so no file in the
CLAUDE.md ceiling table is touched.

## The hardening rules (ticket §Design), and where each is enforced

1. **Model text is text.** `ask_view.js` builds every node with `el()` / `createElement` and sets
   `textContent` or appends `createTextNode`. Static: `check_single_innerhtml` still counts exactly one
   `.innerHTML` front-end-wide; the new `check_ask_has_no_html_sinks` fails if `ask.js` **or**
   `ask_view.js` contains `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`,
   `new Function`, `setAttribute("on…")`, and also `createContextualFragment`, `DOMParser` or `srcdoc`
   (three more HTML-parsing entry points; cheap to forbid). Both files, not only `ask.js`: the view is
   where the text is inserted, and a guard over the wrong file would be green and blind.
2. **Citation markers** are parsed from the answer string with `/\[(\d{1,2})\]/g`. A match becomes an
   `<a>` only if its number is a `ref` in the server's `citations`, that citation is not withheld, and
   its `document_id` matches the canonical UUID pattern; the `href` is `/?id=` +
   `encodeURIComponent(id)` and the text is the marker. Anything else is a text node.
3. **No model string becomes an attribute.** The only attribute set from result data is that
   UUID-checked `href` (the click handler reads the id back from it; there is no `data-id`).
4. **CSP unchanged**: pinned to a literal string in `tests/test_ui_security.py`, on the shell and on
   `/api/ask/config`, so a change to the constant is a visible test edit.

## Design choices the ticket leaves open (resolved here)

- **D1 — A withheld answer says so.** With bodies not served, RM-18a's lens nulls `answer` and
  `sub_queries` when any citation is withheld, and a withheld citation has no `document_id`, `title`
  or `snippet`. The turn says "The answer is withheld on this server: it draws on a confidential note."
  and lists each withheld citation as "[n] A confidential note (withheld on this server)", with **no
  Open-in-Notes link** (RM-11's carry-forward). Nothing typed into the form is shown in its place.
- **D2 — Ask ids are remembered apart from ingest ids.** `jobs.js`'s `submitJob` remembers every id in
  one `sessionStorage` list, and the Ingest tab resumes that whole list, so an ask would have appeared
  as an ingest row after a reload. `jobs.js` gains an optional `scope` (`submitJob(path, body,
  {scope})`, `rememberedJobs(scope)`, `forget(id, scope)`, `markSettled(id, scope)`,
  `wasSettled(id, scope)`); the Agent tab uses `"ask"`, the Ingest tab passes nothing and keeps its
  keys. A browser test pins it from both sides.
- **D3 — Turns live in the page; `sessionStorage` holds ask ids only.** A5's capped turn log of
  questions and answers is for RM-18c's "Follow up", its only consumer, and is not built here. A
  stored answer would outlive A10's 10-minute server retention, which this ticket has no reason to
  undo. After a reload a resumed ask is labelled by its submit time, not by its question (the question
  is not on the wire), exactly as RM-11 labels resumed ingest rows; its answer is re-read from the
  server while retained, and an evicted one is dropped silently (`wasSettled`).
- **D4 — "Waiting for the ingest job to finish."** A snapshot carries no queue position. While an
  ask is `queued`, the tab reads `GET /api/jobs` (which lists ingest jobs and never asks, A10). The
  queue is FIFO (`JobRegistry.run_next` pops its left end), so an ingest is **ahead** of the ask only
  when it is `running`, or `queued` with a `created_at` strictly earlier than the ask's own (both are
  the registry clock's ISO stamps, compared with `Date.parse`; an unparseable stamp is never
  "earlier"). Ahead: "Waiting for the ingest job to finish"; otherwise "Queued: waiting for the job
  runner". Read once per poll of a queued ask, and never for a resumed placeholder before its first
  poll (it is "queued" only until that poll says what it is). **Known, not changed (sec-83 item 3):**
  this is one extra `GET /api/jobs` per poll while an ask is queued, bounded by the queue (four
  jobs) and the 1 s / 3 s poll interval, and an in-memory read on the server.
- **D5 — One ask at a time per tab.** The submit button is disabled while this tab's ask is unsettled;
  the server has one worker (A1), and a second click would only queue behind the first.
- **D6 — The steps are a list.** Each `plan` / `retrieve` / `reflect` / `synthesize` event appends one
  line ("Planning the search", "Retrieving notes (round 1 of 3)", …), so the progress the goal asks
  to watch stays
  visible; the phase line and an indeterminate `<progress>` show the current step. The list is not a
  live region (it changes every step); `#agent-announce` gets one sentence per state change, as in
  RM-11 decision 2.
- **D7 — Citations list** under the answer: "[n]", the title, the source kind and the snippet, each as
  text, with "Open in Notes" (same UUID-checked `href`) for a citation that is not withheld.
- **D8 — `fallback_note`** (RM-02's text-only retrieval) is shown verbatim as a notice. It is a
  server-built, class-only sentence, still inserted as text.

## Tests (RED first)

- `tests/test_ui_tabs_static.py`: the Agent pane is not deferred, has the form and its labels — red
  until `index.html` changes.
- `tests/test_ui_static_behaviour.py`: the sink guard and the boot guard, each with mutation rows
  (`test_every_guard_can_fail`); rosters — red until the files exist and are wired.
- `tests/test_ui_security.py`: literal CSP on `/` and `/api/ask/config`.
- `tests/test_ui_ask_wire_contract.py` (5434): fake chat answer with `<img src=x onerror=…>`,
  `<script>`, `javascript:`, `[1]` and `[99]`; asserts `[99]` is dropped server-side, the HTML is
  returned verbatim (so the client is the layer that must hold), the citation id matches the JS UUID
  pattern read out of `ask_view.js`, and the result / citation / snapshot keys equal the harness's.
- `tests/test_ui_browser_agent.py` (hermetic): AC1 malicious answer, sub-queries, titles, snippets and
  error text render as text, no `img`/`script`/extra `a`, no dialog, a non-UUID id gives no link; AC2
  `[1]` opens the note in Notes; AC3 Cancel ends `cancelled`, the waiting-for-ingest state renders;
  disabled reasons; withheld answer; an ask never shows in the Ingest tab after a reload (D2); steps
  render in order.

Mutation proofs (in a `git archive` copy only): `textContent` → `innerHTML` in `ask_view.js`; the
UUID check dropped; the citation-range check dropped; each must turn a named test red.

## Risks

- A later edit adds a client-side markdown renderer: the sink guard fails first (ticket risk).
- The browser suite polls at 1 s; scripts are short and every wait is on the DOM.

## Deviations from the ticket text

1. **A5's turn log is not built here** (D3). `sessionStorage` holds ask ids only; the capped log of
   questions and answers is RM-18c's, its only consumer. A stored answer would outlive A10's
   10-minute retention.
2. **The sink guard covers `ask_view.js` as well as `ask.js`**, and forbids three more HTML entry
   points (`createContextualFragment`, `DOMParser`, `srcdoc`). The ticket names `ask.js` only; the
   view is where text is inserted, so a guard over `ask.js` alone would be blind.
3. **`jobs.js` is touched** (an optional `scope`, D2), although the ticket's scope list says "reuse".
   Reused as it was, a reloaded ask appeared as an ingest row; ingest's keys are unchanged.
4. **The dead `.deferred-tab` / `.deferred` CSS is deleted** (`components.css`), with the prose that
   described it (`layout.css`, `tokens.css`) and the `.deferred-tab vs .shell` row of
   `CROSS_SHEET_OVERLAPS`. The scan's "different selectors, one element" half lost its only live
   instance, so a new GUARDS row (`cross-sheet-second-shape-different-selectors-one-element`)
   re-creates one by putting `.shell` on the Agent pane, keeping that half proven.
5. **Docs beyond the ticket's list:** `README.md` (the `brain ui` paragraph) and
   `src/brain/ui/__init__.py`'s docstring, which still said Ingest and Agent were deferred.
6. **The route test is a new file**, `tests/test_ui_ask_wire_contract.py`, rather than rows in
   `tests/test_ui_ask_routes.py`: its job is to tie the hermetic stub to the wire, so it imports the
   stub's key sets.
7. **The list of turns is a `<div>` with a heading**, not a nested `<section>`:
   `tests/test_ui_tabs_static.py` slices a pane up to its first `</section>`.

## Evidence

- RED before code: `tests/test_ui_tabs_static.py` (2 failed: the pane was the placeholder),
  `tests/test_ui_security.py::test_the_agent_tab_ships_under_the_unchanged_csp` (`ask.js` 404).
- RED baseline for the browser suite: `master` `2f83ea3` plus the new test files, in a `git archive`
  copy: 12 failed (the pane did not exist).
- Mutation proofs, each in its own `git archive` copy (import realpath checked, anchor applied exactly
  once, copy deleted): answer through an HTML sink; UUID check dropped; range check dropped (any
  marker links to a citation); ask scope dropped; ingest-ahead check disabled; Cancel never offered;
  withheld flag ignored; CSP loosened; a stub key dropped; UUID pattern unanchored. Each turns its
  named test red. Two survived the first pass (withheld flag; unanchored pattern), and the tests were
  strengthened until both died (`80c9c01`).
- End-to-end: the real app under uvicorn with its worker thread, on the 5434 rm18b database, a fake
  `ChatJson` (no Ollama) and a real Chromium: four steps shown in order, the hostile answer shown as
  text with no `img` and no dialog, `[1]` opened the note in the Notes tab.

## Review round 1 (rev-83, sec-83 at `fc79ce6`)

1. D4 counted every queued ingest as ahead; the queue is FIFO, so only a running one or one queued
   earlier is. Fixed in `ask.js`; the stub's listed entries carry `created_at`, and
   `tests/test_ui_ask_wire_contract.py` proves the stub's listing keys and the `created_at` form
   against the real `GET /api/jobs`.
2. `setHTMLUnsafe` and `parseHTMLUnsafe` join the sink list.
3. The guard also refuses navigation (`location =`, `location.href =`, `.assign(`, `.replace(`),
   `.src =`, `.style` writes and `setProperty`, and `.href =` except the exact `noteLink` line
   (`ASK_HREF_SITE`). No existing line hit any of them: `ask.js` only READS `link.href` and
   `location.href` (in `new URL(...)`), which the `=(?!=)` patterns do not match. Each pattern has
   an example that trips it alone (an isolation test), and a GUARDS row built from that example.
   (Round 2 widened this again; see below.)
4. `jobs.js` keeps only ids shaped like `uuid.uuid4().hex` (`JOB_ID_RE`) in the `ask` scope.
5. D6's quoted step text corrected.
6. A resumed placeholder no longer reads `GET /api/jobs` before its first poll.
7. Step phases are tested with `Object.hasOwn`, so `toString` / `constructor` are never steps.

## Review round 2 (rev-83, sec-83 at `da3b245`): the last widening of the sink guard

Test-only; no JS changed. `ASK_FORBIDDEN_SINKS` gains, each with its own isolation example and
generated GUARDS row:

1. **An attribute allowlist.** Any `setAttribute(` whose name is not a quoted `aria-` literal, and
   any `setAttributeNS(` whose second argument is not one. This replaces the ticket's
   `setAttribute("on…")` pattern, which it subsumes; `ASK_ON_HANDLER_EXAMPLE` keeps a GUARDS row
   and an isolation case for `on…`. The tab's three real calls (`aria-labelledby`,
   `aria-describedby`, `aria-disabled`) are positive controls that must still pass.
2. **Bracket notation:** `["href" | "src" | "srcset" | "style" | "srcdoc" | "action" |
   "formaction" | "location"]`, and `.style[`.
3. **Navigation and URL setters:** `open(`, `.srcset =`, `.action =` / `.formAction =`. The last
   is scoped with `(?<!dataset)`: `ask_view.js` sets `link.dataset.action = "open"` and
   `button.dataset.action = "cancel"` (the tab's own `data-action` markers). `open(` hit nothing.

**The guard is a regression net, not a parser.** It reads source text, so an indirection can always
slip past it. The defences are the textContent-only rule and the CSP (`script-src 'self'`, no
`unsafe-inline`). Gaps found after this round go to the backlog, not into another widening here.
