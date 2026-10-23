# RM-01 — `brain recall` falls back to text-only matching

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-01 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(§3, Wave 1). The ticket is the spec. This plan is the task list, as DoD item 1 asks.
**Backlog row:** BL-14 in [`2026-10-23-backlog-reconciliation.md`](./2026-10-23-backlog-reconciliation.md).
**Reuses:** `brain.search_fallback` (PR #61). No new search code.

## Files

| File | Change |
|---|---|
| `src/brain/recall.py` | Routes the one search through `search_with_fts_fallback(hybrid_search, …)`. Adds `RecallResult.fallback` (last field, default `None`) and `header_query` (default `None`, not in the JSON). `to_dict()` merges `fallback_json`, and `context_block()` appends `, text-only`. The header's query echo is capped (see the deviation below). |
| `src/brain/cli_recall.py` | Prints `fallback.note` on stderr (yellow) when it is set, and exits 0. |
| `src/brain/mcp_server.py` | `brain_recall`: the unreachable `except EmbedError` branch is deleted and a one-line comment takes its place. The tool docstring names the four keys. Net negative. |
| `tests/test_recall_fallback.py` (new) | Unit tests on `RecallResult`; DB tests on `recall` (transient, permanent, context overflow, non-embed error, healthy run, budget sweep with a long query). |
| `tests/test_cli_recall_fallback.py` (new) | `CliRunner`: stderr note, exit 0, `--json` keys, and no secret detail. |
| `tests/test_mcp_recall_fallback.py` (new) | `brain_recall` with the failing embedder in `_State`: keys, no `INTERNAL_ERROR`, `payload_tokens`, `psycopg.Error` mapping. |
| `tests/test_recall_render.py` | The documented key set gains the four keys. |
| `CLAUDE.md` | The `mcp_server.py` row gets a descriptive hop. Its last hop (`bc3b868`) is already SHA-bound, so there is nothing to promote. |
| `docs/cli-reference.md`, `docs/configuration.md` (MCP tool list), `CHANGELOG.md`, `skills/` (only if a recall recipe documents the payload) | Docs. |
| `docs/specs/2026-10-23-roadmap-completion-design.md` (Appendix B), backlog BL-14 | Tracker rows. |

## Deviation from the ticket (ruled by team-lead before code)

The ticket's Design said the header "is covered by the fixed `_ENVELOPE_TOKENS` reserve (48
tokens)". **That was false for a long query, on `master`, with no fallback at all**: the header
echoes the query, and nothing bounds query length (CLI or MCP). The evidence is
`tests/test_recall_fallback.py`'s budget sweep: FakeEmbedder, a 36-word query, budgets 300–1496 in
steps of 23. On `4cf5f1a`, **13 of 53 budgets overflowed**; one example is 338 tokens against a
budget of 300.

**Ruling (team-lead): bound the header, don't loop.** The query the header echoes is cut to
`_HEADER_QUERY_MAX_TOKENS` (16), measured with the budget's own `count_tokens`, with one `…`
appended when cut. The worst-case header is the cap, four-digit counts, a seven-digit token
total, `, truncated` and `, text-only`. It is asserted to fit `_ENVELOPE_TOKENS` under both the
fake tokenizer and `cl100k_base`, so the reserve stays 48. The JSON `query` stays whole.

- Packing is untouched.
- Every recall whose query is at or under the cap is byte-identical to today.
- A long query still packs passages.

A first cut that re-packed in a loop until the block fit was replaced by this, before review.

## Risks

- An agent may miss that the context is lexical only. The header marker mitigates this (it is in
  the pasted artifact).
- A long query's header shows a cut query. The JSON `query` field and the stderr notice are not cut.
