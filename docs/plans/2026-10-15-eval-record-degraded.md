# `brain eval` refuses to record or gate a degraded run — plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Spec:** `docs/specs/2026-10-15-eval-record-degraded-design.md`
**Branch:** `fix/eval-record-refuses-degraded`

**Goal:** a run in which any query's embed call raised `OllamaEmbedError` can
no longer be saved as a baseline, and cannot pass or fail `--fail-below`; it is
reported (by count) everywhere it used to be silent.

**Test doubles:** a `FlakyEmbedder` in the test modules that delegates to the
conftest `FakeEmbedder` and raises `OllamaEmbedError` for `input_type="query"`
on a named set of texts. No real Ollama. Each test uses query strings unique to
it, because `brain.search` caches query embeddings per embedder class and a
string that succeeded in one test would be served from cache in the next.

## Task 1 — represent the degraded row (runner)

Files: `src/brain/eval/runner.py`, `tests/test_eval_runner.py`.

1. RED: `run_eval` with a flaky embedder marks exactly the failing query
   `degraded=True` (metrics still `0.0`) and leaves the healthy one `False`;
   `degraded_results(report)` returns exactly the failing row.
2. GREEN: `EvalResult.degraded: bool = False`; set it in the
   `OllamaEmbedError` branch; add `degraded_results`.
3. Extend the existing `test_run_eval_handles_ollama_failure_gracefully` to
   assert `degraded is True`.

## Task 2 — refuse to save, strip on save (baseline)

Files: `src/brain/eval/errors.py`, `src/brain/eval/baseline.py`,
`tests/test_eval_baseline.py`.

1. RED: `save_baseline` on a report with one degraded row raises
   `DegradedBaselineError`, the message contains `1 of 2`, does not contain
   the query text, and the path does not exist afterwards; a pre-existing file
   at the path is byte-identical afterwards.
2. RED: `save_baseline` on a clean report writes a file with no `degraded`
   key; `load_baseline` round-trips it to `degraded=False`.
3. GREEN: `DegradedBaselineError(EvalBaselineError)`;
   `degraded_run_message(count, total, *, action)`; the guard at the top of
   `save_baseline`; pop `degraded` from each serialized result.

## Task 3 — exclude from the diff (baseline)

Files: `src/brain/eval/baseline.py`, `tests/test_eval_baseline.py`.

1. RED: `diff_reports(clean_baseline, current_with_one_degraded)` omits the
   degraded query from `per_query`, computes the means over the rest, and lists
   it in `degraded_queries`. A degraded row on the *baseline* side is excluded
   the same way. A fully degraded current run yields empty `per_query` and a
   non-empty `degraded_queries` (the case the gate must not pass).
2. GREEN: `BaselineDiff.degraded_queries: list[str]`; skip a query when either
   side is degraded.

## Task 4 — CLI mapping (cli.py, over the ceiling)

Files: `src/brain/cli.py`, `tests/test_cli_eval.py`.

1. RED, rule-12 regression: `brain eval --record-baseline ci` with one flaky
   query exits `1`, writes no file, and stderr names the count. (On master this
   exits `0` and writes the query as `0.0`; prove that by running the test
   against the unmodified CLI before the fix.)
2. RED: a pre-existing baseline file survives a refused re-record unchanged.
3. RED: plain run with a flaky query exits `0` and warns on stderr with the
   count.
4. RED: `--diff` with a flaky current run exits `0`, warns, and the diff JSON
   lists the query under `degraded_queries` and not in `per_query`.
5. RED: `--diff --fail-below` with a flaky current run exits `1` (not `3`, not
   `0`), stdout is still parseable diff JSON, stderr says the gate could not be
   evaluated. Also with a FULLY flaky run (the empty-intersection case).
6. GREEN: the four sites in `eval_cmd`, each with an inline why-comment; the
   module-docstring pointer; the CLAUDE.md `cli.py` row's hop promotion.

## Task 5 — docs

`tests/eval/baselines/README.md` (record refusal, the `exit 1` from
`--fail-below`, how to spot a pre-fix poisoned row), CLAUDE.md eval section
(exit-code paragraph and the "Updating a committed baseline" step), CHANGELOG
`[Unreleased]` → `Fixed`.

## Task 6 — verify

`ruff check`, `mypy src/`, full suite of record via `bin/brain-ci` (or its
environment on a sibling test database if refused — say which), plus the
mutation catalog below with the five-clause harness.

## Mutation catalog

Each mutation must land its declared count (1), turn the named test(s) red with
the needle on a `>`/`E` line, and go green on restore.
`PYTHONDONTWRITEBYTECODE=1` throughout.

| # | File | Mutation | Must turn red |
|---|---|---|---|
| M1 | `eval/runner.py` | `OllamaEmbedError` branch stops setting `degraded = True` | runner degraded-flag test; CLI record-refusal regression test |
| M2 | `eval/baseline.py` | delete the `save_baseline` refusal guard | save-refusal unit test; CLI record-refusal regression test |
| M3 | `eval/baseline.py` | message built from query text instead of the count | save-refusal "no query text" assertion |
| M4 | `eval/baseline.py` | stop popping `degraded` on save | "no `degraded` key in the file" test |
| M5 | `eval/baseline.py` | `diff_reports` skips only current-side degraded rows | baseline-side exclusion test |
| M6 | `eval/baseline.py` | `diff_reports` stops excluding degraded rows | diff exclusion unit test; CLI `--diff` test |
| M7 | `cli.py` | delete the `--fail-below` degraded refusal | CLI fail-below exit-1 tests (partial and full) |
| M8 | `cli.py` | move the degraded refusal after the regression check | CLI partial fail-below test (expects `1`, gets `3`) |
| M9 | `cli.py` | delete the plain-run warning | CLI plain-run warning test |
| M10 | `cli.py` | `except EvalBaselineError` around `save_baseline` removed, so `DegradedBaselineError` escapes | BOTH CLI record-refusal tests, via `isinstance(result.exception, SystemExit)`. The exit code cannot catch it: CliRunner maps an uncaught exception to exit `1` as well (in production it is a traceback), so the tests assert the exit was a clean `typer.Exit` |
| M10b (audit F3) | `cli.py` | the `save_baseline` handler narrowed to `except KeyError`, so the refusal escapes as a traceback | both CLI record-refusal tests, via the same `SystemExit` assertion |
| M11 (review F1) | `cli.py` | gate keyed on any degraded current row (`degraded_count`) instead of `diff_result.degraded_queries` | CLI "only an added query is degraded" test (expects `0`, gets `1`) |
| M12 (review X2) | `eval/runner.py` | `degraded = False` hoisted out of the per-query loop, so the flag sticks once set | runner `[healthy, failing, healthy]` flag test |
| M13 (review X4) | `eval/baseline.py` | `path.parent.mkdir(...)` moved above the degraded guard | save-refusal "creates no directory" test |

M11–M13 were added after review of PR #33, and M10's reason was corrected after its audit: the original row claimed the exit code would change, which it does not. X2 and X4 survived the first
catalog because every fixture put the failing query last and every refusal test
wrote into an existing `tmp_path`. The new tests fix the order and the parent
directory, which is what kills them.
