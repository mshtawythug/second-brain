# RM-29 — Class-only terminal error reports (H-6)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

- **Ticket:** RM-29 in `docs/specs/2026-10-23-roadmap-completion-design.md` (the ticket is the
  spec; this is the task list). Source: H-6 (PR #58), backlog rank 16.
- **Branch:** `fix/rm-29-class-only-reports`, from `second-brain/master` at `8416703`.
- **Approval:** the team lead's RM-29 brief (2026-10-30) is the approval of this plan's scope.

## What ships

1. **`brain vault export`'s error lines are class-only.** In `export_vault`,
   `summary.errors.append(f"{relative}: {e}")` becomes
   `f"{relative}: {describe_failure(e)}"`. `describe_failure` keeps the symbolic errno of an
   `OSError` and drops its strerror and path, so a full disk still reads
   `<relative>: OSError[ENOSPC]`. The `relative` vault path was in the line before this change
   and stays in it. The CLI prints `summary.errors` unchanged (`cli.py`, `vault_export`), so this
   one line covers the terminal report.
2. **`brain backfill scan-secrets --action redact` records class-only failures.**
   `_apply_redact` no longer records `str(exc)`:
   - the documented hash collision (the redacted body has another document's `content_hash`) is
     recorded as the fixed sentence `REDACT_COLLISION_MESSAGE`, "redacted body collides with
     another document's content hash". `update_document`'s own message named the other
     document's id;
   - any other `ValueError` is recorded as `describe_failure(exc)`.

   Both the text report and the `--json` `errors[].error` field read from the same record.

## How the collision is told apart

`update_document` raises a bare `ValueError` for the collision and for other failures, such as a
document deleted mid-sweep. Only the message tells them apart, and the report must not use the
message. So after a failure, `_hash_taken_by_another` checks the database: does another document
already hold the redacted body's content hash? It hashes with the same function `update_document`
uses (`brain.ingest._content_hash`). `vault/sync.py` and `backfill/source_rows.py` already import
private ingest helpers this way. The query runs only on the failure path.

## Files

| File | Change |
|---|---|
| `src/brain/vault/export.py` | one import line, and the error line rewritten in place (764 → 765; the §2.2 budget is +1) |
| `src/brain/cli_sensitivity.py` | `REDACT_COLLISION_MESSAGE`, `_hash_taken_by_another`, and `_apply_redact`'s `except` (277 → 310) |
| `tests/test_class_only_reports.py` | new: sentinel tests on both report paths |
| `CHANGELOG.md`, `docs/cli-reference.md` | Unreleased entry; `scan-secrets`'s error reasons |
| `docs/plans/2026-10-23-backlog-reconciliation.md` | H-6 closed |
| `docs/specs/2026-10-23-roadmap-completion-design.md` | Appendix B, RM-29 → DONE (follow-up commit, once the PR number exists) |

Neither source file is in the CLAUDE.md ceiling table, so the promotion rule does not apply.

## Acceptance criteria → tests

Every exception message in these tests is a sentinel string that no code path produces by itself.

| AC | Test |
|---|---|
| 1. A sentinel message never appears in the export report | `test_export_summary_never_quotes_the_exception_message` (the `ExportSummary`), `test_cli_vault_export_report_never_quotes_the_exception_message` (the terminal output of `brain vault export`) |
| 2. A sentinel message never appears in the sensitivity report | `test_redact_report_never_quotes_the_exception_message` (text and `--json`); `test_redact_hash_collision_gets_the_fixed_sentence` (a real collision: the fixed sentence, no id of the other document, no original message) |
| 3. The errno class still appears for an `OSError` | both export tests assert `OSError[ENOSPC]` |

`test_redact_collision_message_is_the_documented_sentence` pins the ticket's wording.

## Mutation proofs (git-archive copies only)

1. The export line goes back to `{e}`.
2. The redact record goes back to `str(exc)`.
3. The errno is dropped (`type(e).__name__` in place of `describe_failure(e)`).
4. The collision check always answers no.
5. The collision check always answers yes.

## Risks

- None for the data: both changes are to report text. A script that parsed the old messages
  would see different text, and no code in the repository does.

## Deviations

- **The collision is identified by a database check, not by a typed exception.** The ticket does
  not say how the documented case is recognised, and `update_document` raises a bare
  `ValueError` for it. A typed exception would have to be raised in `ingest/__init__.py`. That
  file is over the ceiling, and §2.2 does not budget RM-29 for it. Its `brain.errors` import line
  is already 100 characters long, so adding a name grows the file. `_apply_redact` instead
  checks, after a failure, whether another document already holds the redacted body's hash.
- **`cli_sensitivity.py`'s `--action mark-confidential` path is not changed.** It writes
  `PropagationFailure.message` from `vault/sensitivity_propagate.py` into the same report, and
  three of those messages still interpolate `{exc}`. The ticket names `_apply_redact` only. The
  residue is recorded in the backlog's H-6 row for a follow-up.
