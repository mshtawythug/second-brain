# Slim `CLAUDE.md` below the instruction-file limit (2026-10-29)

## Why

Claude Code warned that the instruction files it loads for this repo add up to
154.9k characters, over its 150.0k limit. The repo `CLAUDE.md` was 116.0k of that.
About 90k of it was reference records, not instructions an agent needs every
session: the over-800 ceiling table with its per-file SHA trails, the
trail/promotion/by-omission commentary around it, the F6 residue audit, and the
eval-gate and `ci.json` baseline history.

## What moved where

The text was moved, not deleted. Each block was sliced from `CLAUDE.md` by line
range and written out unchanged. The build asserted that every block appears
byte-for-byte in its new file. History before this date: `git log -p -- CLAUDE.md`.

| Block (old `CLAUDE.md` heading) | New home |
|---|---|
| `### File-size ceiling — 800 lines`: the rule text, the trail and promotion rules, the over-800 table, the "deliberately absent" notes, the source-kinds notes, the ceiling-audit findings | `docs/ceiling-table.md` |
| The paragraphs that followed the F6 audit but were about the ceiling table: the by-omission residue, "Closed by a build change" (RM-04), and the discharged merge note | `docs/ceiling-table.md`, after the table section |
| `### The F6 draft/sensitivity residue audit` (the audit proper, through the `_corpus_common_lexemes` ruling) | `docs/audits/f6-residue-audit.md` |
| `### Eval gate (CI)` and `#### The committed ci.json baseline` | `docs/eval-gate.md` |
| `## Default Design Skill: open-design` (it duplicated the global `~/.claude/CLAUDE.md`, and contradicted it: the repo copy said the Anthropic `frontend-design` plugin was disabled, the global copy says it is enabled) | removed, with the user's sign-off: the removal was described to the user before they approved the commit and PR |

Each new file starts with a short provenance note. `CLAUDE.md` keeps the same three
`###` headings, each with a short summary of the rule an agent must follow and a
pointer to the new file. The headings stay because code and docs cite them by name,
for example "CLAUDE.md, 'File-size ceiling'".

Everything else in `CLAUDE.md` is unchanged: the numbered IMPORTANT rules, the Team
Mode Override, the architecture principles and coding conventions, linting, the
migration and security rules, the workflow, the project overview, the tech stack,
the build commands, the layout, the key patterns and the lessons learned.

## Sizes (characters)

| File | Before (`f311a94`) | After (PR #112) |
|---|---|---|
| `CLAUDE.md` | 129,893 | 26,851 |
| `docs/ceiling-table.md` | — | 91,321 |
| `docs/audits/f6-residue-audit.md` | — | 3,978 |
| `docs/eval-gate.md` | — | 13,865 |

Counted as characters (`len()` of the decoded text; `wc -c` gives bytes, which run
higher because of the em dashes and arrows). "Before" is `CLAUDE.md` at `f311a94`, the
master PR #112 is based on. It was 117,129 at `92438fe` (PR #81's base) and 116,030 at
`e1f613d`, where the work began, when the warning reported 154.9k in total.
`docs/ceiling-table.md` grew from 78,201 on PR #81 because the seven rows master
rewrote after `92438fe` came across with it (see "Reconciled onto `f311a94`" below).

## Edits made after the verbatim move

These are the only changes to the moved text, made deliberately and recorded here:

1. **Row-count grep.** The instruction `grep -c '^| .*no commit through' CLAUDE.md`
   now greps `docs/ceiling-table.md`, because that is where the rows are.
2. **Trail rules, applied.** This change repoints a docstring in four files that are
   in the table: `setup.py`, `graph_rag/extract.py`, `vault/graph.py` and
   `vault/watch.py`. Each edit replaces one line in place, so every file keeps its
   line count. Following the promotion rule, each row's trailing descriptive hop was
   promoted to a SHA and count before this change's own descriptive hop was added:
   - `vault/graph.py`: 955 (`f0abf64`, the 2026-09-02 merge).
   - `graph_rag/extract.py`: 928 (`ad67f43`, PR #58).
   - `vault/watch.py`: 1,300 (`e7a26ca`, the PR #56 second review round). That was
     PR #81's edit. Master had already promoted `e7a26ca` itself by `f311a94`, so on
     PR #112 the row runs SHA-bound through master's `8351382`, then this change's
     own hop (see "Reconciled onto `f311a94`" below).
   - `setup.py`: its trail was already SHA-bound through `6074b97`.

   On PR #112 the reconciliation commits bound every one of these descriptive hops
   to its SHA (`5cba1e9`), so none is left to promote.

## Code that followed the table

- `scripts/check_ceiling_table.py` now reads `docs/ceiling-table.md` (the new
  `TABLE_DOC` constant) instead of `CLAUDE.md`. Its messages name the new file. The
  header anchor, the exit codes and the parsing are unchanged.
- `tests/test_ceiling_table_check.py` reads the real table from the new file and
  writes the synthetic table there. It adds two regression tests:
  - A complete table left in `CLAUDE.md` does not satisfy the check.
  - The real `CLAUDE.md` carries no second copy of the table header.
- The comments in `ci.yml`, `eval.yml` and `bin/brain-ci`, the assertion messages
  in `tests/test_ci_workflow.py` and `tests/test_eval_fail_below.py`, two lines of
  `CONTRIBUTING.md`, and `tests/eval/baselines/README.md` were all repointed.
- These docstrings named the table, a row or the residue section, and were repointed
  to `docs/ceiling-table.md`: `setup.py`, `graph_rag/extract.py`, `vault/graph.py`
  (two lines), `vault/watch.py` (two lines: the race-fixes line, and on PR #112 the
  owed-work line master added after `92438fe`), `related.py` and `snippet_context.py`.
  On PR #112, `eval/baseline.py`'s "no per-metric overrides" comment was also
  repointed at `docs/eval-gate.md`, where that decision now lives.
  `snippet_context.py` also loses a stale claim that `search.py` is under the
  ceiling; it is in the table.

## Brought up to date with master

The work began on `master` at `e1f613d`. It was moved onto `cc62cf4` (RM-10 to
RM-13) before the first commit, and then rebased onto `92438fe` (RM-15, PR #78)
when PR #80 conflicted. Across both moves, master changed three lines in the moved
region: the `cli_ingest.py` row of the ceiling table (by `cc62cf4`), and the
`cli.py` and `mcp_server.py` rows (RM-15's growth records, by `92438fe`). Each
branch copy of those rows was still identical to the base, so each was replaced
with master's line exactly as master has it. A line-by-line check confirmed
that every line of master's moved sections is in the new files. The only
exceptions are the five lines edited on purpose (listed above). Up to `92438fe`,
master did not change the four over-ceiling files this change touches, and no
file newly crossed 800 lines. That stopped being true by `f311a94`; see the next
section.

## Reconciled onto `f311a94` (PR #112)

PR #81 could not take master `f311a94`: master was 357 files ahead, and the merge
commit's staged diff (+63,181/−1,214) was too large for the pre-commit PII gate's
semantic pass, which errors on input that size. So PR #112 replays PR #81's two
commits on `f311a94` as `5cba1e9` and `1c87b23`. `5cba1e9`'s one conflict,
`CLAUDE.md`, resolved to the slim file. Two further commits carry master's
changes into the moved text:

- **Six rows ported verbatim.** Since `92438fe`, master rewrote seven ceiling rows:
  `cli.py`, `mcp_server.py`, `ingest/__init__.py`, `vault/watch.py`, `people.py`,
  `search.py` and `cli_ingest.py`. All but `vault/watch.py` were untouched on this
  branch, so each was replaced with master's line exactly.
- **`vault/watch.py`, the one row both sides changed.** Master changed
  `src/brain/vault/watch.py` in `9099853`, `5a52835`, `ae467f5`, `305b9a4` and
  `8351382` (RM-26 and PR #95), promoted `e7a26ca` itself, and added an owed-work
  docstring line that named "CLAUDE.md's ceiling table". The row is master's,
  character for character, plus two hops after `8351382`: 1,366 (`5cba1e9`) and
  1,366 (`07c760e`, the commit that repoints that docstring line). Both are net-zero.
- **Descriptive hops bound.** The `setup.py`, `graph_rag/extract.py` and
  `vault/graph.py` rows ended in a descriptive "the docs/slim-claude-md commit". A
  later commit that does not touch those files bound each one to `5cba1e9` (1,224,
  928 and 955), by the `bc3b868` precedent.
- No other line of master's `CLAUDE.md` changed after `92438fe`, and no file newly
  crossed 800 lines (`check_ceiling_table.py` passes: 16 files, one row each).

A line-by-line check of master's `CLAUDE.md` at `f311a94` against the slim
`CLAUDE.md` plus the three new files and `CONTRIBUTING.md` found 604 of 613
non-empty lines verbatim. The other 9 are the deliberate edits: four rewritten
rows, the row-count grep, and the four open-design lines.

## Left alone on purpose

- **Docstrings that cite the ceiling *rule*,** for example "File-size ceiling
  (CLAUDE.md): already over, and it grew". The rule is still in `CLAUDE.md`, so they
  are still correct. Changing them would have touched every file in the table for
  no gain in accuracy.
- **Historical sentences.** For example, `mcp_server.py`'s "written ... under a
  CLAUDE.md header": that was true when it was written.
- **Dated plans, specs and handoffs** under `docs/plans`, `docs/specs` and
  `docs/handoff`. They are records of their own date.

## Review round (PR #80)

- The code review approved with two LOW notes, both applied. The eval-gate summary in
  `CLAUDE.md` now says `--fail-below` exits `1` only when a query the baseline also
  has is degraded. `test_claude_md_without_header_exits_2` was renamed to
  `test_table_doc_without_header_exits_2`.
- The completion audit flagged the open-design removal (now signed off, above), the
  stale numbers in this record (updated), and nested bold in the four new
  descriptive hops in `docs/ceiling-table.md` (fixed, matching the existing
  `**the ... commit**, **net-zero**:` form).
- The rebased history went to a new branch with a replacement PR, because
  force-pushing is blocked here.
- That replacement, PR #81, was itself superseded by PR #112 when it fell 357 files
  behind master (see "Reconciled onto `f311a94`" above). PR #81 was closed with a
  pointer, and its branch was left in place.
