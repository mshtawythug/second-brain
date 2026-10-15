# Eval recency determinism — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Goal:** make clock-dependent `recency` eval queries reproducible, so
`brain eval --baseline ci --diff --fail-below` stops tripping exit 3 on
wall-clock drift. To do that, add optional absolute `after` / `before` date
bounds to the golden-corpus schema and pass them through to `hybrid_search`.

**Spec:** `docs/specs/2026-10-15-eval-recency-determinism-design.md`.

**Architecture:** the change is in the eval layer only: `brain.eval.corpus`
(schema and validation) and `brain.eval.runner` (pass-through). There is no
search-layer change, no baseline-format change, and no CLI change.

## Files

| File | Change |
|---|---|
| `src/brain/eval/corpus.py` | `EvalQuery.after` / `.before` (`date \| None`), `_parse_date_bound`, and the validation rules in spec §3.1 |
| `src/brain/eval/runner.py` | `_utc_midnight`; pass `after=` / `before=` to `hybrid_search` |
| `tests/test_eval_corpus.py` | Spec §4 tests 1–7 |
| `tests/test_eval_runner.py` | Spec §4 tests 8–11 |
| `tests/eval/baselines/README.md` | (2): pinned form, the user-applied corpus edit, the window-check SQL |
| `CLAUDE.md` | "The committed `ci.json` baseline", item 2 |

No touched source file is in CLAUDE.md's over-ceiling table. `corpus.py` is
152 lines and `runner.py` is 223.

## Task 1 — corpus schema (TDD)

1. RED: write spec §4 tests 1–7 in `tests/test_eval_corpus.py`. Run them and
   confirm they fail. `after` is an unknown field today, so the parse tests fail
   with "unknown field(s)".
2. GREEN: add `"after"` and `"before"` to `_ALLOWED_FIELDS`. Add the fields to
   `EvalQuery`. Add `_parse_date_bound(value, *, field, index)`: reject a
   `datetime` before checking `date`, since `datetime` subclasses `date`;
   accept a `date` or an ISO `YYYY-MM-DD` string; reject everything else. Add
   the three cross-field rules.
3. Refactor so each rule is one `if` with a message that names the query
   number and the rule.

## Task 2 — runner pass-through (TDD)

1. RED: write spec §4 tests 8–11 in `tests/test_eval_runner.py`. Test 8 (the
   drift reproduced) must PASS on the unmodified code, because it is the bug.
   Tests 9–11 must fail.
2. GREEN: add `_utc_midnight(d: date | None) -> datetime | None` and pass
   `after=_utc_midnight(q.after), before=_utc_midnight(q.before)` to the
   `hybrid_search` call.

## Task 3 — docs

- README (2): document the pinned form and how to choose the dates. Keep the
  `since_days` history. Add the read-only "judged docs inside the range" SQL.
  State that converting a query requires a re-record.
- CLAUDE.md item 2: point at the pinned form, and keep the dated history.

## Task 4 — verification

`ruff check`, `mypy src/`, and `bin/brain-ci` (the suite of record) must be
green, using a sibling test DB name unique to this worktree.

## Mutation proofs

Apply each mutation in a `git archive` copy, not in the worktree, so the suite
of record is never mutated. Run with `PYTHONDONTWRITEBYTECODE=1`. Check all
five clauses: the unmutated run passes; the mutation lands exactly its declared
count; the run goes red; the needle appears on a `>` or `E` line; and the
restored copy is green again.

| ID | File | Mutation (sed-level) | Count | Red test (needle) |
|---|---|---|---|---|
| M1 | corpus.py | delete the `after`-without-`before` raise | 1 | `test_load_corpus_rejects_after_without_before` |
| M2 | corpus.py | `after >= before` → `after > before` | 1 | `test_load_corpus_rejects_after_not_before_before[equal]` |
| M3 | corpus.py | delete the `since_days` exclusivity raise | 1 | `test_load_corpus_rejects_since_days_with_absolute_bound` |
| M4 | corpus.py | drop the `datetime` rejection branch | 1 | `test_load_corpus_rejects_non_date_bound[datetime]` |
| M5 | runner.py | drop the `after=` / `before=` kwargs | 1 | `test_run_eval_pinned_window_is_stable_under_new_ingest` |
| M6 | runner.py | swap `q.after` / `q.before` | 1 | `test_run_eval_pinned_bounds_are_half_open` |
| M7 | runner.py | `tzinfo=UTC` → removed | 1 | `test_utc_midnight_is_utc_aware` |

## What the coordinator / user applies after merge

1. In the local `tests/eval/golden_corpus.yaml`, replace `since_days: 40` on
   the two `recency` entries with `after:` / `before:` dates. Spec §3.3 explains
   how to choose them.
2. Re-record `ci.json` (`brain eval --record-baseline ci`, read-only on prod,
   with Ollama), and commit it with the trigger "an edit to
   `golden_corpus.yaml`".
