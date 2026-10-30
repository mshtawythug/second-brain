# RM-28 — Close the log-privacy guard's blind spots (H-5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

- **Ticket:** RM-28 in `docs/specs/2026-10-23-roadmap-completion-design.md` (the ticket is the
  spec; this is the task list). Source: H-5 (PR #58, the "Not covered" list in
  `docs/plans/2026-10-22-watcher-repair-hint-and-yaml-log.md`), backlog rank 16.
- **Branch:** `fix/rm-28-log-privacy-guard`, from `second-brain/master` at `213d12b`.
- **Approval:** the team lead's RM-28 brief (2026-10-30) is the approval of this plan's scope.

## What ships

The G1 guard (`tests/test_log_privacy.py`) gains three AST rules. Its scanner moves to a new
helper module, `tests/log_privacy_rules.py`, because the self-tests for three rules would take
the test file past the 800-line ceiling (it was 541). The allowlists move with it: they are the
scanner's data, and each entry keeps its reason.

G1 and its self-tests (G2-G3, T1-T3) move to `tests/test_log_privacy_rules.py`, marked `nodb`.
They are pure AST work, but in a module that also holds database tests, running them alone still
took the suite lock and reset the test schema (conftest resets unless EVERY collected test is
`nodb`). `tests/test_log_privacy.py` keeps the leak reproductions (Y1-Y6, H1-H2, F1).

1. **T1, taint propagation.** A name assigned inside an `except … as e` handler from a value that
   uses `e` (other than through `describe_failure`, `type` or `isinstance`) is tainted for the
   WHOLE enclosing function, and so is any name later assigned from a tainted one. Logging it
   after the handler is flagged. The scope is flow-insensitive on purpose: inside a loop, code
   above the handler runs after it on the next iteration. The handler's own name is not carried
   out of the handler, because Python unbinds it there and a later reuse (`for e in …`) is
   unrelated.
2. **T2, attribute stores.** `obj.attr = <tainted>` (also annotated, augmented and
   tuple-unpacking stores) is flagged as a `store` hit, unless the value goes through a
   content-free call. A new `STORE_ALLOWLIST`, keyed `(module, function, target)`, takes an entry
   only with a written reason.
3. **T3, nested scopes.** A `def` or `lambda` sees every name tainted where it is defined, minus
   its own parameters (which shadow). Before this, a nested `def` restarted from its own
   exception-annotated parameters, so a closure over `e` was invisible.

## Real hits in `src/`

The prototype scan over `src/brain` found **one** hit, so RM-28.D1 (fix in this PR when ten or
fewer) applies, and no follow-up ticket is needed:

- `ui/jobs.py` `_fail`, `job.error = error` (T2). `error` is annotated `JobError`, and the
  guard's parameter heuristic treats any annotation naming `Error`/`Exception` as an exception.
  `JobError` is not one: it is a frozen `(code, message)` dataclass whose message is FIXED per
  code and never carries exception text (its docstring; `generic_failure` uses the class name
  only). This is a false positive, so the fix is a `STORE_ALLOWLIST` entry with that reason, not
  a source change. `_fail` exists precisely to keep the store out of the handler (its comment),
  which is the shape T2 enforces. That comment named `tests/test_log_privacy.py` as the scan's
  home; it is reworded in place (net-zero, `jobs.py` is not in the ceiling table) to name
  `tests/log_privacy_rules.py`.

## Tests (self-tests, the file's existing pattern)

For each rule, synthetic positive snippets that must be flagged and negative snippets that must
pass:

- **T1:** positive: an alias logged after the `try`; an alias chained after the handler then
  logged; an alias logged inside a loop above the handler. Negative: a `describe_failure` alias
  logged after the handler; `e` reused as a loop variable after the handler.
- **T2:** positive: `self.last_error = e`; `obj.detail = str(e)`; a tuple unpack storing `e`;
  an alias stored after the handler; an exception parameter stored. Negative:
  `self.last = describe_failure(e)`; `self.kind = type(e).__name__`; `self.failed = True` in a
  handler; the allowlisted store at its own site only.
- **T3:** positive: a nested `def` logging `e`; a nested `def` logging an alias; a `lambda`
  logging `e`. Negative: a nested `def` whose parameter shadows `e`; a `lambda e: …`; a nested
  `def` logging `describe_failure(e)`.

## Mutation proofs

In a `git archive` copy only (PYTHONPATH pinned to the copy), never in the live worktree. For
each rule, disable it and show that its positive self-tests fail and the rest pass:

- T1: `_scope_taint` stops seeding the handler aliases.
- T2: `_stored_attributes` returns `[]`.
- T3: a nested `def` and a `lambda` inherit nothing.

The proofs, with their test counts, go in the PR body.

## Docs

- The guard's docstring and the PR #58 plan's "Not covered" list say these three are covered
  now, and name what is still not (a container mutation, a return value).
- CHANGELOG `[Unreleased]`; Appendix B RM-28 → DONE; backlog H-5 → SHIPPED.

## Verification

`ruff check && mypy src/`; `python scripts/check_ceiling_table.py`; the self-tests and G1 in
`tests/test_log_privacy.py` (no local full suite; CI is the evidence).
