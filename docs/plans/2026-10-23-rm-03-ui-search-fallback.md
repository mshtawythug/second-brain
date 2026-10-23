# RM-03 — `brain ui` search falls back automatically, with a visible notice

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-03 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(§3, Wave 1). The ticket is the spec. This plan is the task list, as DoD item 1 asks.
**Backlog row:** BL-13 in [`2026-10-23-backlog-reconciliation.md`](./2026-10-23-backlog-reconciliation.md).
**Reuses:** `brain.search_fallback` (PR #61). No new search code, and no new module.

## Files

| File | Change |
|---|---|
| `src/brain/ui/routes_search.py` | The one search goes through `search_with_fts_fallback(ctx.search_fn, conn, embedder=ctx.embedder, query=spec.query, limit=spec.fetch_limit, …)`. `query` and `limit` are taken out of `spec.filter_kwargs()` first, so neither is passed twice. The unreachable `except EmbedError` → 503 `embedding_unavailable` branch is deleted, with a comment naming the helper. The response gains `fallback_json(...)`'s four keys beside `meta`. |
| `src/brain/ui/static/index.html` | One element above the ledger: `<p id="search-fallback" class="search-fallback" role="status" hidden></p>`. |
| `src/brain/ui/static/js/results.js` | Sets the notice's `textContent` to `fallback_note` and un-hides it when a ready payload carries a non-null `fallback`; hides and clears it on every other render (loading, error, idle, a normal payload). No `innerHTML`. |
| `src/brain/ui/static/css/components.css` | A `.search-fallback` rule: an amber `--flag-draft` rule on the left, a `var(--paper)` inset (as `.search-box` uses) on the `.search-head` ground, and `color: var(--ink)` in the inherited body font at `--t-meta` size. Plus a defensive `.search-fallback[hidden] { display: none; }`: no author `display` competes today, and the rule keeps one added later from beating the UA `[hidden]` on origin. |
| `src/brain/search_fallback.py` | Docstring only: names `brain ui`'s `GET /api/search` among the surfaces that fall back. |
| `tests/test_ui_static_behaviour.py` | The `[hidden]`-cascade guard's anti-vacuity anchor count goes from 10 to 11 `.hidden =` sites, because the notice is one more. Its scan resolves the new site and passes on the new `.search-fallback[hidden]` rule. |
| `.github/workflows/ci.yml` | `tests/test_ui_browser_search_fallback.py` joins the explicit browser roster, in byte order. |
| `tests/test_ui_search_fallback.py` (new) | Route tests against the 5434 database with `tests/embed_down.py`'s failing embedder: transient, permanent, context overflow, non-embed error, the healthy run's `null` keys, telemetry `duration_ms` covering both attempts, `limit` page size with and without a fallback, the secret detail never on the wire, and the deleted 503. |
| `tests/test_ui_browser_search_fallback.py` (new) | Hermetic browser suite (`page.route` stubs `/api/search`): the note shows above the results with `fts-only` in the meta line; a following normal search hides it; the note is text, never markup. |
| `docs/cli-reference.md` (`brain ui`), `CHANGELOG.md` (Unreleased) | Docs. No `docs/guides` page mentions the 503 (checked with grep), so none changes. |
| `docs/specs/2026-10-23-roadmap-completion-design.md` (Appendix B), backlog BL-13 | Tracker rows, in a follow-up commit once the PR number exists. |

**Not touched:** every file in the CLAUDE.md ceiling table. `routes_search.py` is about 220 lines
and `results.js` about 260, so the ceiling does not apply. No skill documents the UI's search
payload, so `skills/` is unchanged.

## Details

- **The `embedding_unavailable` code goes.** `grep -rn embedding_unavailable src` finds only this
  branch, so no other route raises it.
- **`fts_only` requested by the caller is unchanged.** The helper attempts the embed only when the
  caller did not ask for `fts_only`, because `hybrid_search` never embeds then, so no fallback can
  happen and the four keys are `null`.
- **Telemetry.** `started` is taken before the first attempt and `duration_ms` is measured after
  the retry, so the recorded duration covers both. The `diagnostics` holder is reset by the helper,
  so `fts_count` describes the text-only run, not the failed one.
- **Confidential lens.** `sensitivity` is in the kwargs the helper passes through unchanged to the
  retry, so the text-only run excludes exactly what the normal run excludes.

## Risks

- The text-only ranking is weaker; the notice is the mitigation (the ticket's shared risk).
- RM-09 will later replace the helper call with `search_pipeline.run_search`. This PR adds nothing
  that change would have to undo: one call site, the same kwargs, the same four keys.
