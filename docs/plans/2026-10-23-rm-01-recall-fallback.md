# RM-01 — `brain recall` falls back to text-only matching

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-01 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(§3, Wave 1). The ticket is the spec. This plan is the task list, as DoD item 1 asks.
**Backlog row:** BL-14 in [`2026-10-23-backlog-reconciliation.md`](./2026-10-23-backlog-reconciliation.md).
**Reuses:** `brain.search_fallback` (PR #61). No new search code.

## Files

| File | Change |
|---|---|
| `src/brain/recall.py` | Routes the one search through `search_with_fts_fallback(hybrid_search, …)`. Adds `RecallResult.fallback` (last field, default `None`). `to_dict()` merges `fallback_json`, and `context_block()` appends `, text-only`. The envelope reserve is measured (see the deviation below). |
| `src/brain/cli_recall.py` | Prints `fallback.note` on stderr (yellow) when it is set, and exits 0. |
| `src/brain/mcp_server.py` | `brain_recall`: the unreachable `except EmbedError` branch is deleted and a one-line comment takes its place. The tool docstring names the four keys. Net negative. |
| `tests/test_recall_fallback.py` (new) | Unit tests on `RecallResult`; DB tests on `recall` (transient, permanent, context overflow, non-embed error, healthy run, budget sweep with a long query). |
| `tests/test_cli_recall_fallback.py` (new) | `CliRunner`: stderr note, exit 0, `--json` keys, and no secret detail. |
| `tests/test_mcp_recall_fallback.py` (new) | `brain_recall` with the failing embedder in `_State`: keys, no `INTERNAL_ERROR`, `payload_tokens`, `psycopg.Error` mapping. |
| `tests/test_recall_render.py` | The documented key set gains the four keys. |
| `CLAUDE.md` | The `mcp_server.py` row gets a descriptive hop. Its last hop (`bc3b868`) is already SHA-bound, so there is nothing to promote. |
| `docs/cli-reference.md`, `docs/configuration.md` (MCP tool list), `CHANGELOG.md`, `skills/` (only if a recall recipe documents the payload) | Docs. |
| `docs/specs/2026-10-23-roadmap-completion-design.md` (Appendix B), backlog BL-14 | Tracker rows. |

## Deviation from the ticket (raised with team-lead before code)

The ticket says the header "is covered by the fixed `_ENVELOPE_TOKENS` reserve (48 tokens)", and it
asks that the reserve be re-measured and raised if needed. **That is false for a long query, on
`master`, with no fallback at all.** The header echoes the query, and nothing bounds the query
length (CLI or MCP). Measured with `tests/test_recall_fallback.py`'s budget sweep (FakeEmbedder,
a 3×12-word query, budgets 300–1496 in steps of 23): **13 of 53 budgets overflow** on `4cf5f1a`.
One example is 338 tokens against a budget of 300. No constant can cover an unbounded query.

**Fix:** keep 48 as the starting reserve. After packing, measure the real `context_block()`. If it
is over the budget, raise the reserve by the overshoot and pack again, until it fits or nothing
packs. A recall whose block already fits is packed exactly as before, so packing, the budgets and
ranking are unchanged for every recall that honoured its budget today (the ticket's scope-out).
**One residual case:** a query whose header alone exceeds the budget yields no passages, and it
cannot fit. This is recorded in the docstring.

## Risks

- An agent may miss that the context is lexical only. The header marker mitigates this (it is in
  the pasted artifact).
- Re-packing costs extra `count_tokens` calls, but only on a block that overflowed.
