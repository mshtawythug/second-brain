# RM-22a — The engagement readout that decides the gate

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-22a in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(wave 5; backlog rank 14, "gate it on the data"). The ticket is the spec; this is the task list.
**Base:** `second-brain/master` `ef97b7c` (PR #86, RM-19, merged).

## Goal

A measured answer to "is there enough engagement data to learn from": `brain usage --engagement`
prints counts of document opens, clicks and ratings, the size of the `interactions` table (which
RM-58 needs), and one line saying whether RM-22b's gate is met. Nothing about ranking changes.

## Files

| File | Change |
|---|---|
| `src/brain/engagement.py` (new) | `engagement_stats(conn, *, days=90) -> EngagementStats`; `engagement_gate(stats) -> EngagementGate`; the gate constants `GATE_WINDOW_DAYS = 90`, `GATE_MIN_OPENS = 500`, `GATE_MIN_DOCUMENTS = 100`, `GATE_MIN_RATINGS = 30`, each with its rationale (RM-22a.D1). Parameterized SQL, counts only. Well under 800 lines. |
| `src/brain/cli_usage.py` | A `--engagement` flag on `brain usage` (RM-22a.D2: a flag, not a subcommand), declared last so the other options keep their order in `--help`. With the flag the command prints the readout (or `--json`) and returns before the existing report code, which is unchanged. |
| `tests/test_cli_usage_golden.py` (new) + `tests/fixtures/usage_golden/` | AC4. The goldens were captured from `cli_usage.py` at `ef97b7c`, **before the flag existed**, in a commit that changes no `src/` line. |
| `tests/test_engagement.py` (new) | AC1–AC3, DB tests on a synthetic `interactions` fixture: every count, the window, document rows only, the search-session join, the threshold flips, the 90-day refusal. |
| `tests/test_cli_usage_engagement.py` (new) | AC5 and the CLI forms: human and `--json`, `--days 7` against `--days 365`, `--raw-queries` refused, no titles in the output. |
| `docs/cli-reference.md` | `brain usage --engagement` under `## brain usage`. |
| `CHANGELOG.md` | Unreleased entry. |
| `docs/plans/2026-10-23-backlog-reconciliation.md` | Rank 14: the readout is SHIPPED (#PR, RM-22a); the stage (RM-22b) waits for the gate. |
| Spec Appendix B | Follow-up commit once the PR number is known: RM-22a → `DONE (#PR)`. |

`usage.py` is not touched (the ticket says so). No over-ceiling file is touched, so no
ceiling-table row moves. No migration, no knob, no MCP tool, no skill text.

## Design (as the ticket states it, with the choices it leaves open)

Read from the real schema and writers, never guessed: `interactions` (migrations 010, 015, 024,
027) has `document_id` (nullable since 015), `action ∈ {clicked, opened, rated_useful,
rated_irrelevant, pinned}`, `source ∈ {cli, mcp, wiki, ui}`, `session_id`, `at`. The writers are
`brain_show` (MCP `opened`, with the search's `session_id`), `brain_ask` citations (MCP `opened`,
with **the ask's own** `session_id`), `brain_rate` / `brain rate` (ratings, document or graph
target), and `brain ui` (`opened`, with the UI search's `session_id`). Nothing writes `clicked`
today (the wiki that would have is deleted), but the action is allowed, so it is counted.

- **"Opens" for the gate are read events, `opened` + `clicked`.** That is what `brain usage`'s
  existing `opens` total counts (`usage._READ_ACTIONS`), so the two commands never show the owner
  two different "opens" numbers. The readout still reports opens and clicks separately, by
  surface, as the ticket asks. A test pins the action set against `usage._READ_ACTIONS`, so the
  two cannot drift.
- **Every count is over document rows (`document_id IS NOT NULL`)**, except `interactions_rows`
  and `interactions_bytes`, which describe the whole table for RM-58. RM-22b ranks documents, so a
  graph-target rating (entity, community, theme) is not data it can learn from. Today graph
  targets are only ever rated, never opened.
- **"Came from a search session"** means the row's `session_id` matches a `search_queries` row.
  `session_id IS NOT NULL` alone would be wrong: `brain_ask` logs its citations with the ask's own
  session id, and no search is logged under it. The share is over read events, and is `None`
  (printed as no percentage) when there are none.
- **Explicit ratings are `rated_useful` + `rated_irrelevant`.** `pinned` is not a rating.
- **The window** is `at >= NOW() - make_interval(days => %s)`, the same predicate as `brain usage`.
  `days < 1` raises `ValueError` (the CLI's `--days` already has `min=1`).
- **The gate:** at least 500 read events across at least 100 distinct documents, and at least 30
  explicit ratings, all over 90 days. `engagement_gate(stats)` raises `ValueError` for stats
  computed over any other window, so a caller cannot meet the gate by widening one. The CLI
  computes the 90-day stats separately when `--days` is not 90, and reuses them when it is.
- **Output.** With `--engagement`, `brain usage` prints only the readout. `--json` emits the
  stats plus a `gate` object. `--limit` has no effect on it. Counts only: no title, query, id or
  agent appears in either form.

## Deviations

1. **`--raw-queries` with `--engagement` exits 2** (Typer `BadParameter`). The ticket does not name
   this. The readout carries no query strings at all, so the flag could only mislead someone into
   thinking it had added them.

## Tasks (TDD, red first)

1. AC4 golden, captured before any `src/` change: `brain usage`, `--days 7 --limit 2`, `--json`,
   `--json --raw-queries`, on a synthetic fixture. Dates are normalized, the console width is pinned
   and ANSI is stripped. Commit it with this plan.
2. `tests/test_engagement.py` RED (the module does not exist), then `engagement.py` GREEN.
3. `tests/test_cli_usage_engagement.py` RED (no such option), then the flag GREEN. The AC4 golden
   stays green throughout.
4. Mutation proofs in a `git archive` copy: the gate on `--days` instead of 90; each threshold off
   by one (`>=` → `>`, and each constant ± 1); ratings counted wrong (`pinned` included, one action
   dropped); the flag changing the no-flag output; the search-session join replaced by
   `IS NOT NULL`.
5. Docs, CHANGELOG, backlog row; `ruff check`, `mypy src/`, `python scripts/check_ceiling_table.py`,
   `grep -c '^| .*no commit through' CLAUDE.md` = 1; targeted pytest only (disk policy; CI is the
   full-suite evidence).
6. PR; then the Appendix B row → DONE (#PR) as its own commit; memory files `cli.md`, `types.md`
   and the `MEMORY.md` index line.

## Risks

- **The thresholds are judgment calls** (the ticket's risk). They are named constants with the
  rationale beside them, and the readout prints each count against its threshold. A miss then
  shows how far off it is, not just "not met".
- **The AC4 golden depends on Rich's table layout.** The width is pinned and ANSI stripped. A Rich
  upgrade that re-pads a table would turn it red, and the fix then is to re-capture it in a commit
  that changes nothing else.
- **Prod is never read.** The owner runs `brain usage --engagement` themselves. The PR body states
  what the gate needs, using a synthetic run only.
