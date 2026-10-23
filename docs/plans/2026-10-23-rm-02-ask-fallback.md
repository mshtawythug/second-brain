# RM-02 — `brain ask` retrieves text-only when the embedder is down, and every failure is clean

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-02 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(§3, Wave 1). The ticket is the spec. This plan is the task list, as DoD item 1 asks.
**Backlog row:** BL-15 in [`2026-10-23-backlog-reconciliation.md`](./2026-10-23-backlog-reconciliation.md).
**Reuses:** `brain.search_fallback` (PR #61), the same way RM-01 (PR #63) did for `recall`. No new
search code.

## The defect, reproduced before any code

`brain ask "meadow survey" --no-loop` against the 5434 test database, with `OLLAMA_HOST` pointed
at a dead port: exit 1, empty stdout, and a 32 KB Rich traceback on stderr ending
`OllamaEmbedError: Ollama request failed: [Errno 61] Connection refused`. `ask._retrieve_hybrid`
called `hybrid_search` directly, and the `ask` handler caught only chat and graph errors. Its
other branches interpolated `{exc}` into user-facing text, which breaks DoD item 6.

## Files

| File | Change |
|---|---|
| `src/brain/ask_errors.py` (new leaf) | `ASK_FAILURES` (`EnrichmentError`, `GraphTenantError`, `GraphBackendError`, `EmbedError`) and `describe_ask_failure(exc)`: one fixed, class-only sentence per family, with the specific `EnrichmentError` subclasses tested before the base. |
| `src/brain/ask.py` | `_retrieve_hybrid` goes through `search_with_fts_fallback(hybrid_search, …)` and returns `FallbackSearch`. `_retrieve` returns `(results, graph_summary, fallback)`. `ask` and `ask_no_loop` keep the first fallback seen. `AskResult.retrieval_fallback` (last field, default `None`); `to_dict()` merges `fallback_json`; the `fallback_used` docstring gains the line that tells the two apart. Budget: ≤ 30 lines (§2.2). |
| `src/brain/cli.py` | `ask`: the three `except` branches collapse into one `except ASK_FAILURES`, printing `ask: <describe_ask_failure(exc)>`, then exit 1. On success it prints `retrieval_fallback.note` on stderr (yellow). `_print_ask_result` adds `Retrieval: text-only (semantic ranking unavailable)` under the answer. `_run_answer_eval_cli` passes `fallback=False` (see the deviation below). Net −3, so no growth record is owed. |
| `src/brain/mcp_server.py` | `brain_ask`: the same tuple maps to `INTERNAL_ERROR` with `describe_ask_failure(exc)`. `ValueError` and `psycopg.Error` keep their mappings. The tool docstring names the four keys and tells the agent to relay the note. Net −6 (two imports went unused), so no growth record is owed. |
| `tests/test_ask_errors.py` (new) | One test per class, subclass ordering, the transient/permanent embed split, the sentinel-absent property. |
| `tests/test_ask_fallback.py` (new) | DB tests on `ask_no_loop` / `ask` with `tests/embed_down.py`'s failing embedder (both modes), the first-fallback rule, the confidential lens on the fallback leg, non-embed errors propagating, a healthy run with `None`. |
| `tests/test_cli_ask_fallback.py` (new) | `CliRunner`: criteria 1–6. |
| `tests/test_mcp_ask_fallback.py` (new) | Criterion 7, plus sentinel-absent for every family. |
| `tests/test_ask.py`, `tests/test_mcp_ask.py` | Test doubles of `_retrieve` / `_retrieve_hybrid` updated to the new return shapes. |
| `CLAUDE.md` | `cli.py` row: promote the descriptive hop (`95a926b`, 8,819), add a net-negative descriptive one (no growth record). `mcp_server.py` row: promote RM-01's descriptive hop (`f2f57f1`, 5,077), then add a net-negative descriptive hop. |
| `docs/cli-reference.md`, `docs/configuration.md` (MCP tool list), `CHANGELOG.md`, `skills/brain-ask/SKILL.md` | Docs. |
| Spec Appendix B, backlog BL-15 | Tracker rows. |

## Deviation from the ticket (ruled by team-lead during implementation)

`brain eval --answer` runs `ask_no_loop`. RM-02's "Eval" line said it "inherits the fallback".
§2.7 says `brain eval` never falls back, spec 2026-10-16 §3.4 ruled that the answer eval aborts
cleanly on an embed failure, and `tests/test_cli_ask.py::test_cli_eval_answer_embed_failure_exits_cleanly`
pins that. Routing `ask` through the fallback made that test fail and the eval's
`except EmbedError` unreachable.

**Ruling (team-lead): eval never falls back.** `ask()` and `ask_no_loop()` take
`fallback: bool = True`, threaded through `_retrieve` to `_retrieve_hybrid`, which calls
`hybrid_search` directly when it is `False`. `_run_answer_eval_cli` passes `fallback=False`. The
existing test stays unchanged and green; `tests/test_ask_fallback.py::test_fallback_false_re_raises_the_embed_error`
pins the opt-out. The spec's "Eval" line is corrected in this PR. RM-09's
`run_search(..., fallback=True)` has the same shape, so `ask` will forward the flag and RM-09 has
nothing to undo.

**Consequence for the ceiling:** the argument costs `cli.py` one line, and the file still
shrinks overall (net −3), so it owes no growth record (team-lead, second note). `mcp_server.py`
still shrinks. `ask.py` lands exactly at its budget (654 + 30 = 684).

A bug found on the way: the loop unpacked each leg's fallback into a variable named `fallback`,
shadowing the new flag, so a healthy first sub-query would have switched the fallback off for
the next one. mypy caught it; `test_a_healthy_sub_query_does_not_switch_the_fallback_off_for_the_next`
pins it.

## Risks

- A weaker answer that reads confidently. The stderr note, the `Retrieval:` line and the MCP keys
  mitigate it; the answer prose is never edited (it would disturb the `[N]` sanitizer).
- `fallback_used` and `fallback` sit side by side in the JSON and mean different things. The
  `AskResult` docstring says so, and so do the CLI reference and the MCP docstring.
