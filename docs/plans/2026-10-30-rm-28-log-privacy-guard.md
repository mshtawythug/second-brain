# RM-28 — Close the log-privacy guard's blind spots (H-5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

- **Ticket:** RM-28 in `docs/specs/2026-10-23-roadmap-completion-design.md` (the ticket is the
  spec; this is the task list). Source: H-5 (PR #58, the "Not covered" list in
  `docs/plans/2026-10-22-watcher-repair-hint-and-yaml-log.md`), backlog rank 16.
- **Branch:** `fix/rm-28-log-privacy-guard`, from `second-brain/master` at `213d12b`.
- **Approval:** the team lead's RM-28 brief (2026-10-30) is the approval of this plan's scope.

## What ships

The G1 guard gains three AST rules and moves to `tests/test_log_privacy_rules.py` (below). Its
scanner moves to a new helper module, `tests/log_privacy_rules.py`, because the self-tests for
three rules would take `tests/test_log_privacy.py` past the 800-line ceiling (it was 541). The
allowlists moved with the scanner, and in the review round to their own module,
`tests/log_privacy_allowlists.py`; each entry keeps its reason.

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

`ruff check && mypy src/`; `python scripts/check_ceiling_table.py`; G1 and its self-tests in
`tests/test_log_privacy_rules.py` (no database), and the leak reproductions in
`tests/test_log_privacy.py` against a sibling test database (no local full suite; CI is the
evidence).

## Review round (PR #94)

The security review, the code review and the completion audit found gaps in the new rules. None
was a live leak in `src/`. Each fix has a positive self-test, a negative where a false positive
is plausible, and a mutation proof (PR body).

- **S1:** a parameter whose DEFAULT is tainted stays tainted (`lambda e=e: …`).
- **S2:** taint flows through every binding form: `for`/`async for`, comprehension targets,
  `with … as`, `match` captures (`MatchAs`, `MatchStar`, a mapping's `**rest`, class-pattern
  keywords), and `except*` with `for sub in eg.exceptions`.
- **S3:** `traceback.format_exc`/`format_exception`/`format_exception_only`/`format_tb`,
  `sys.exc_info`, `sys.exception` and an argument-less `.exception()` are tainted sources;
  `traceback.print_exc` and kin are `traceback` hits.
- **S4:** `setattr`/`object.__setattr__`, a subscript on an attribute (`self.errors[k] = …`), and
  an attribute container's `append`/`extend`/`add`/`insert`/`update` are stores (the Y4 shape).
- **S6:** the `warn` and `fatal` levels; a `getLogger(...)` receiver; an exception parameter is
  one whose annotation has an identifier ENDING with `Error`/`Exception`, a builtin exception, one
  of `EXCEPTION_CLASS_NAMES` (the 46 `src/brain` exception classes whose names do not end that
  way; a self-test re-derives the set), or `psycopg.errors.…`. "Ends with", not "contains":
  `ErrorMapper` is a callable alias, and the substring rule flagged `ui/jobs.py`'s `submit`.
- **S7:** the store allowlist is keyed on the whole statement, and G1 fails when any entry of any
  allowlist matches no site.
- **S8:** the module scope no longer descends into top-level `def` bodies.
- **R1:** a content-free call exempts only its first positional argument. `len` joins the set (a
  count is content-free).
- **R2:** a nested `def`'s decorators and annotations are scanned with the enclosing taint.
- **A5:** only NAME targets are tainted. A store's base (`self`, `job`) and key stay clean; T2
  reports the store itself.

**Hits in `src/` after the round: six, none a leak.** Two are fixed at the rule level (`len`, the
"ends with" rule). Four are allowlisted with written reasons: `chunk_split.on_truncated` extends
the tally with the salvaged entities (the extraction's output, not exception text);
`frontmatter._shifted_past_the_fence` re-stores the exception's own mark on itself; and
`sync_vault`/`sync_one_file` append `str(e)` of a `_SyncError`, whose message is authored at its
raise sites from fixed text, `describe_failure`, a constraint name or a count (Y5 and Y6 pin two).

**Round 3 (final).** V1: a subscript store into a LOCAL container taints the container
(`info["err"] = str(e)` taints `info`, never the key), restoring what A5 had dropped. V2: quoted
annotations (`'OSError'`, `Optional['FooError']`, `'OSError | None'`) are read like unquoted ones.
V3: only the bare `type`/`isinstance`/`len` builtins are content-free; `describe_failure` may also
be called as an attribute. V4: a container store's receiver has its subscripts peeled
(`self.d[k].append(…)`), and `setdefault`, `appendleft`, `put` and `put_nowait` are stores. G4:
every entry of all three allowlists must carry a reason. No new `src/` hit.

**Still not covered** is listed in `tests/log_privacy_rules.py`'s module docstring. The two big
classes, raise-message interpolation and stderr echo, are backlog **BL-28**.
