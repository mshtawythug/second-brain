# RM-04 — CI fails when the ceiling table and the tree disagree

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-04 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(§3, Wave 1). The ticket is the spec. This plan is the task list, as DoD item 1 asks.
**Backlog row:** BL-1 in [`2026-10-23-backlog-reconciliation.md`](./2026-10-23-backlog-reconciliation.md).
**Reuses:** the `importlib.util` loading pattern of `tests/test_embedding_smoke.py`, and
`bin/brain-ci`'s per-step status pattern (`RUFF_STATUS`, `MYPY_STATUS`). No `src/` change.

## Files

| File | Change |
|---|---|
| `scripts/check_ceiling_table.py` (new) | Stdlib only. `CEILING = 800`, `HEADER = "\| File \| Lines \| Why it is not being split \|"`. Pure functions `count_lines` (`data.count(b"\n")`, the `wc -l` count), `over_ceiling(src_root)`, `table_rows(claude_md)` (raises `TableNotFound`), `compare(over, rows) -> CeilingReport`, `format_report(report, src_root)`, and `main(argv) -> int`. Exit 0 when the table and the tree agree, 1 on any disagreement, 2 when `CLAUDE.md`, the header or `src/brain/` is not found. |
| `tests/test_ceiling_table_check.py` (new) | Loads the script with `importlib.util`. Drives the pure functions and `main` on `tmp_path` trees and synthetic markdown, runs the script on the real repo, and deletes each real row in turn from an in-memory copy of `CLAUDE.md` to show each deletion is caught. |
| `.github/workflows/ci.yml` | One step in the `lint` job, after `mypy src/`: `python scripts/check_ceiling_table.py`. The job's `name:` is unchanged (branch protection and the docs key on it). The header comment names the step. |
| `tests/test_ci_workflow.py` | The lint job runs the step, and runs it after `mypy src/`. |
| `bin/brain-ci` | A `CEILING_STATUS` block in the lint stage (a `step` line, an `ok` line), a summary row, and inclusion in the lint exit status. The header comment and the exit-code table name it. |
| `tests/test_bin_brain_ci.py` | `find_drift` gains a lint-step comparison: every `run:` of ci.yml's `lint` job (other than the install) must have a matching `step "ci.yml job 'lint' — <cmd>"` label and a `"$VENV/bin/<tool>" <args>` invocation in the script, and every such label must name a ci.yml lint command. Mutation cases in both directions, plus a test that every lint `*_STATUS` feeds the exit code. |
| `CLAUDE.md` | The "Closing it is a build change" paragraph is rewritten to say the check exists, where it runs, and what it still cannot catch (trail contents, the near-ceiling budgets). The "There is **no automated gate**" sentence under the table is corrected. **No table row is edited.** |
| `CONTRIBUTING.md` | The lint row of the CI table names the ceiling check. |
| `CHANGELOG.md` | An Unreleased entry. |
| Spec Appendix B, backlog BL-1 | Tracker rows, in a follow-up commit once the PR number exists. |

**Not touched:** every file under `src/`, so no ceiling row needs a promoted hop and no daemon
reinstall follows. No skill documents the CI gate, so `skills/` is unchanged.

## Details

### Parsing the row set

- The anchor is the one line whose `rstrip()` is exactly `HEADER`. Missing, or present on more
  than one line → `TableNotFound` → exit 2, so a reworded header disables the check loudly rather
  than silently, and two tables under one header are never a guess. (Prose that quotes the header
  mid-line, as `CLAUDE.md`'s own paragraph does, is not an anchor.)
- Rows are the lines after the header up to the first line that does not start with `|`. The
  separator row (`|---|---|---|`) is skipped.
- First cell: one backticked path relative to `src/brain/`. Second cell (Lines): must contain
  `` `wc -l src/brain/<path>` `` naming the same path. Only these two cells are read; a `|` inside the
  Why cell cannot shift them.
- A row whose first cell is not a single backticked path, whose Lines cell names no `wc -l` command
  or names another path, or that repeats an earlier row's path, is a **mismatched** row.

### Comparing

- `over_ceiling` walks `src/brain/**/*.py` (the only package under `src/`) and keeps files with
  more than 800 lines. 800 is not over.
- `missing_rows`: over, no row. Remedy: add a row in the trail form, or extract first.
- `stale_rows`: a row whose file is gone, or is at or under 800. Remedy: remove the row and add a
  "deliberately absent" note, as `backup/restore.py` and `related.py` have.
- `mismatched_rows`: see above. Remedy: fix the row.
- `compare` is pure over `(over, rows)`; `format_report` reads the tree only to say *why* a row is
  stale (gone, or its current count).

## Tests (RED first)

| AC | Test |
|---|---|
| 1. `master` exits 0 | `test_real_repo_passes` |
| 2. 801 lines, no row → 1, names the file | `test_801_line_file_without_row_fails_and_names_it` |
| 3. 800 is not over | `test_800_line_file_is_not_over`, `test_row_for_800_line_file_is_stale` |
| 4. Row for a deleted file / a 799-line file → 1 | `test_row_for_deleted_file_fails`, `test_row_for_799_line_file_fails` |
| 5. Lines cell names another path → 1 | `test_lines_cell_naming_another_path_fails` |
| 6. No header → 2 | `test_claude_md_without_header_exits_2` (+ reworded header, duplicated header line, missing `CLAUDE.md`, missing `src/brain/`) |
| 7. Deleting a real row turns it red | `test_deleting_any_real_row_is_caught` (every row, parametrized) + a `git archive` mutation run reported in the PR |
| 8. Step removed from `bin/brain-ci` → drift test fails | `test_lint_step_drift_is_detected` (both directions) |

Parser coverage of the real table: `test_parser_reads_every_real_row` counts the real rows by an
independent line scan and checks each Lines cell names its own path.

## Risks

- A future reformatting of the header disables the check: exit 2 makes that loud.
- The check cannot see trail contents or the §2.2 near-ceiling budgets; `CLAUDE.md` says so.
- RM-04 merges one at a time with every ticket that edits the ceiling table or `ci.yml`.
