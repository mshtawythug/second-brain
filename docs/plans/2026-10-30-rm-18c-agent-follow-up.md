# RM-18c — Follow-up questions in the Agent tab (and RM-51, closed as moot)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-18c in `docs/specs/2026-10-23-roadmap-completion-design.md` (wave 4), under the parent
"RM-18 — The Agent tab" and its decision A5 (conversation state). Also, by the coordinator's ruling,
RM-51 (retire the tab strip), closed as moot per §5 U3.
**Builds on:** RM-18a (PR #79: `/api/ask`, ask jobs, `ui/ask_service.py`, `ui/routes_ask.py`) and
RM-18b (PR #83: `js/ask.js`, `js/ask_view.js`, `css/ask.css`, the hardening rules, the
`ASK_FORBIDDEN_SINKS` guard). Its plan doc's Deviation 1 / D3 left A5's turn log to this ticket.
**Branch:** `feat/rm-18c-agent-follow-up`, from `second-brain/master` at `7a3931c`.

## Goal

A follow-up question keeps its context. After an answer, the user presses **Follow up**, types a
question that may refer back ("and who owns that?"), and the plan step sees the earlier turns, so
its sub-queries stand on their own. Synthesis still sees only the current question and the
retrieved notes (RM-18c.D1).

## Files

New:

| File | Responsibility |
|---|---|
| `src/brain/ask_history.py` | A leaf (imports only `brain.token_budget`): `AskTurn`, the caps (`MAX_HISTORY_TURNS` 3, `MAX_HISTORY_TOKENS` 2,000, `MAX_HISTORY_ANSWER_CHARS` 8,000), `cap_history` (newest kept, oldest dropped first) and `history_block` (the plan prompt's "Earlier in this conversation:" section). Importable from `brain.ui` without `httpx` (`tests/test_import_cycles.py`). |
| `tests/test_ask_history.py` | Unit tests: the cap (turns, tokens, the oversize newest turn), the block, `_plan_prompt` with and without history, history never reaching reflect or synthesis, `no_loop` + history refused, MCP `brain_ask` unchanged. |

Touched:

- `src/brain/ask.py` (706 lines, budget +≤50; not in the ceiling table): `ask` gains
  `history: Sequence[AskTurn] = ()`, capped by `cap_history` with the embedder's own `count_tokens`
  before the plan step; `_plan_prompt` / `_call_plan_step` take the capped turns. `ask_no_loop` is
  unchanged (it has no plan step); `ask(no_loop=True, history=…)` with any turn is a `ValueError`.
- `src/brain/ui/ask_service.py`: `AskSpec.history` (default `()`), passed to `ask`; the
  `max_history_turns` the config reports. Logs the turn count only.
- `src/brain/ui/routes_ask.py`: `history` joins the accepted fields; `parse_history` validates the
  shape (400 `invalid_history`, `history_turn_too_long`, `history_requires_loop`);
  `MAX_ASK_WIRE_BYTES` grows to hold three turns; `GET /api/ask/config` gains `max_history_turns`.
- `src/brain/ui/static/index.html`: the follow-up notice in the form (`#agent-follow-up`, its text
  and a **New question** button).
- `src/brain/ui/static/js/ask.js`: the follow-up state, the **Follow up** click, the request's
  `history`, the Quick-answer box turned off while following up.
- `src/brain/ui/static/js/ask_view.js`: the **Follow up** button on a turn that can be followed up,
  and a "Follow-up to: …" line on a turn that was one.
- `src/brain/ui/static/css/ask.css`: their styles, on the existing tokens, under `.agent-pane`.
- Tests: `tests/test_ui_ask_routes.py` (the server cap; the shape refusals; the config key),
  `tests/ui_ask_harness.py` (records each request body; `max_history_turns` in the config),
  `tests/test_ui_browser_agent.py` (the "Follow up" rows), `tests/test_ui_ask_wire_contract.py` if
  it pins the config keys, `tests/test_ui_tabs_static.py` if it pins the form's controls.
- Docs: `docs/cli-reference.md` (the Agent tab paragraph and the `/api/ask` paragraph), `CHANGELOG.md`
  (Unreleased), the backlog (`docs/plans/2026-10-23-backlog-reconciliation.md`: the Agent row and
  rank 9; BL-8 for RM-51), the consolidation spec's Q17 (RM-51), Appendix B's RM-18c and RM-51 rows
  (follow-up commit, after the PR exists).
- Memory: `cli.md`, `types.md`, the `MEMORY.md` index line ("PR #N, verify merged").

No file in the CLAUDE.md ceiling table is touched (`ask.py`, `ask_service.py`, `routes_ask.py` are
all under 800).

## Design

### D1 — Where "Follow up" gets the previous question and answer: the page, never storage

RM-18b stores **ask ids only** in `sessionStorage`, because a stored answer would outlive A10's
10-minute server retention (RM-18b plan, D3 and Deviation 1). This ticket keeps that intact:

- The earlier turns come from the **turn records the page already holds in memory**: the question
  the user typed in this page, and the answer string the page is showing. Nothing new is written to
  `sessionStorage`, `localStorage` or any other store, and nothing is re-fetched.
- A turn offers **Follow up** only when it succeeded, its question is known (it was asked from this
  page, not resumed after a reload, where the question is not on the wire) and its answer is a
  string (a withheld answer is `null`, and offers nothing to send).
- Each turn record keeps the history it was asked with. Pressing **Follow up** on turn *T* sets the
  form's context to `T.history + [T's question and answer]`, the last `max_history_turns` (3) of
  them, so a chain of follow-ups carries the conversation forward and never more than three turns.
- The context's lifetime is the rendered turn's: it lives in page memory, is gone on reload, and is
  cleared when its turn leaves the page. So no answer is kept anywhere it was not already visible,
  and A10's reasoning holds. A5's "capped at 20" turn log is the page's existing turn list (capped at
  `MAX_REMEMBERED`), not a stored log; see Deviation 1.
- The tab sends `history` **only** after **Follow up** is pressed. A fresh ask's body is exactly
  `{question, no_loop}`, with no `history` key. After a follow-up is submitted, the form returns to
  a fresh ask; **New question** clears the context without asking.

### D2 — The server caps it, as untrusted input

- `routes_ask.parse_history` checks the **shape**: a list of objects with exactly `question` and
  `answer`, each a non-empty UTF-8 string, the question at most 2,000 characters and the answer at
  most 8,000. Anything else is a 400 at submit, never a failed job.
- `ask.ask` applies the **cap** itself, with `cap_history(history, cost=embedder.count_tokens)`:
  the newest 3 turns, then newest-first while the total stays within 2,000 tokens, so the oldest go
  first. The cap is in `ask`, not only in the route, so no caller can reach the plan prompt with
  more. More than three turns on the wire is accepted and capped (oldest dropped), as A5 and the
  ticket say, rather than refused.
- A newest turn that alone exceeds the cap keeps its question and has its answer truncated to fit
  (`token_budget.truncate_to_token_budget`); if its question alone does not fit, no history is sent.
- `history_block` labels the turns as earlier questions and answers given **for context only, not
  instructions**, and tells the planner to write sub-queries that stand on their own.
- History reaches **the plan step only**. Reflect and synthesis prompts are built from the current
  question and the retrieved notes, exactly as before (RM-18c.D1): an earlier answer's claims cannot
  enter a new answer without a citation. Tests pin both.
- Nothing logs the history: the runner's INFO line gains the turn count, never a question or answer.

### D3 — A follow-up always plans

`ask_no_loop` has no plan step, so history would have no effect. Rather than drop it silently:
`POST /api/ask` with `no_loop: true` and a non-empty `history` is 400 `history_requires_loop`, and
`ask.ask(no_loop=True, history=[…])` is a `ValueError`. The tab unticks and disables **Quick
answer** while a follow-up is set up, and says why in the notice.

### D4 — MCP `brain_ask` and the CLI are unchanged

`brain_ask`'s signature and tool schema gain nothing (scope out); a test pins that no `history`
parameter exists. `brain ask` is untouched.

### D5 — Model text is still text

The **Follow up** button's label is fixed text; the "Follow-up to: …" line and the notice's quote of
the earlier question are set with `textContent`. The button's `data-action` is a literal
(`dataset.action = "follow-up"`, which `ASK_FORBIDDEN_SINKS`'s `(?<!\.dataset)` lookbehind allows
by design). No new sink; `ASK_FORBIDDEN_SINKS` is not touched, let alone loosened.

## RM-51 — Retire the tab strip: closed as moot (§5, U3)

Consolidation Q17 asked whether to retire the tab strip because three of its four tabs were
apologies. After RM-11 (Ingest, PR #74) and RM-18b (Agent, PR #83), the strip holds Notes, Ingest and
Agent, all real, and Publish was deleted in phases 8 and 9. Q17's premise is gone, so the ticket is
closed as moot, as §5 U3 recommends and the coordinator ruled. No UI change.
`tests/test_ui_tabs_static.py` still pins the three tabs (RM-51 AC2). Recorded in the backlog (BL-8),
in the consolidation spec's Q17 row, and in Appendix B; RM-51's ticket text is not edited.

## Tests (RED first)

- `tests/test_ask_history.py`: the cap (≤3 turns, oldest dropped; ≤2,000 tokens, oldest dropped; an
  oversize newest turn truncated; a non-positive cost never admits unboundedly); `_plan_prompt`
  without history is byte-identical to before, with history contains "Earlier in this conversation:"
  and each kept turn; AC1 through `ask` with a fake chat: the plan prompt holds the previous
  question and answer, cut oldest-first at the cap; the reflect and synthesize prompts hold no
  history; `no_loop` + history is a `ValueError`; AC3: `brain_ask` has no `history` parameter.
- `tests/test_ui_ask_routes.py`: the server cap (a route test: 5 turns, the plan prompt holds only
  what the cap keeps); each shape refusal; `history_requires_loop`; `max_history_turns` in the
  config; a body with three maximal turns is under the wire cap.
- `tests/test_ui_browser_agent.py` (hermetic): AC2, a fresh ask sends no `history`; **Follow up**
  sends the previous question and answer, and a chained follow-up sends both earlier turns; after a
  follow-up the next plain ask sends none; **New question** clears it; no **Follow up** on a withheld
  or resumed turn; nothing but job ids in `sessionStorage` after a follow-up; Quick answer is off
  while following up.

Mutation proofs (each in its own `git archive` copy): history reaching synthesis; the server cap
removed; the cap dropping newest-first; history sent without **Follow up**; MCP `brain_ask` taking
`history`. Each must turn a named test red.

## Risks

- History steers the plan off-topic (ticket risk): only the plan step sees it, and the plan's
  output is search strings, not prose.
- A prompt-injection in an earlier answer reaches the planner: it is the user's own earlier answer,
  labelled as context, capped, and it can only change which searches run.

## Deviations from the ticket text

1. **A5's turn log is the page's in-memory turn list, not a `sessionStorage` log.** A5 puts the turns
   in `sessionStorage`; RM-18b deliberately stored ask ids only, because a stored answer would
   outlive A10's 10-minute retention. "Follow up" reads the question and answer from the turn the
   page still shows, so nothing is persisted, and a reload ends the conversation.
2. **`no_loop` with history is refused** (400 `history_requires_loop`, a `ValueError` in `ask`),
   and the tab turns Quick answer off while following up. The ticket does not say; `ask_no_loop` has
   no plan step, so the history would otherwise be dropped silently.
3. **`GET /api/ask/config` gains `max_history_turns`**, so the tab does not restate the server's 3.
4. **RM-51 is closed in this PR** (coordinator's ruling), with closure notes in the backlog's BL-8
   row and the consolidation spec's Q17 row, as RM-51's own scope names.

## Evidence

(filled in as the work lands)
