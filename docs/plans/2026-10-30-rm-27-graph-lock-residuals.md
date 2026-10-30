# RM-27 — Graph-lock residuals 2–4 (H-3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

- **Ticket:** RM-27 in `docs/specs/2026-10-23-roadmap-completion-design.md` (the ticket is the
  spec; this is the task list). Source: H-3, the "Residuals (recorded, not fixed)" section of
  `docs/plans/2026-10-22-graph-entities-deadlock.md` (PR #53).
- **Branch:** `fix/rm-27-graph-lock-residuals`, from `second-brain/master` at `8416703`.
- **Approval:** the team lead's RM-27 brief (2026-10-30) approves this plan's scope.

## What ships

1. **Residual 2: a document deleted mid-build is skipped, not raised.**
   - `plan_reconcile` raises the new `brain.graph_rag.reconcile_plan.DocumentNotFound` when the
     document is gone. It is a subclass of `GraphReconcileError`, so every existing
     `except GraphReconcileError` (the MCP build tool, the syncers) behaves exactly as before.
   - `build_graph` catches only `DocumentNotFound` and counts the document in a new
     `BuildResult.skipped_deleted` field. The invariant becomes
     `reconciled + skipped + skipped_deleted == processed`.
   - The document still counts against `limit`.
   - `progress` is still called once per processed document. For a deleted one it gets a
     `ReconcileResult(skipped=True)`.
   - A deleted document does not count as `reconciled`, so it alone does not trigger the
     post-loop refresh. The `documents` cascade already removed its relational graph rows.
     Removing its AGE vertex is W2's job, done by whoever deleted it (the syncers), as before.
   - Any other `GraphReconcileError` (bad config, missing extractor) still propagates.
2. **Residual 3: the write re-checks every input the plan read.**
   - `ReconcilePlan` gains `inputs_fingerprint`: a SHA-256 over a canonical JSON of:
     - `documents.content_hash`;
     - `documents.content_type`;
     - `sources.kind` of the document's source (`null` when it has none);
     - the sorted raw participant keys, from `brain.people._doc_participant_keys(source_kind,
       metadata)` over `documents.metadata`. Those are the Gmail `from`/`to` addresses and
       names, and Krisp `_participant_keys`: exactly what `default_person_resolver` feeds to
       resolution.
   - `is_current` re-reads the same columns under `FOR KEY SHARE OF d` (the PR #55 lock, now on a
     `LEFT JOIN sources`, which locks only the `documents` row). It recomputes the fingerprint
     and compares the two.
   - A metadata change that does not move the participant keys (a summary, a tag) does not
     invalidate the plan. The plan never read it, so it cannot make the plan stale.
   - **Read order matters.** The fingerprint is read in the plan's FIRST statement, BEFORE the
     person resolver runs. The plan transaction is READ COMMITTED, so each statement takes a fresh
     snapshot. If an edit commits between the two reads, the fingerprint holds the OLDER value and
     the write's re-check fails, which is safe. Read after the resolver, the fingerprint would
     hold the NEWER value. It would then match at write time while the persons came from the older
     read, which is exactly the overtaking this closes. A test pins the order.
   - One relational helper replaces two. `relational.fetch_doc_inputs(conn, id, *, key_share)`
     returns a `DocInputs` and replaces `fetch_doc_meta` and `key_share_doc_meta`. Nothing else
     called those two.
3. **Residual 4: accepted, no code change (RM-27.D1).** `graph_rag/cross_type.py`'s module
   docstring records the W6/W7 collapse window, why it is cosmetic, and why it is not fixed.

## Files

| File | Change |
|---|---|
| `src/brain/graph_rag/reconcile_plan.py` | `DocumentNotFound`, `inputs_fingerprint` field + `compute_inputs_fingerprint`, fingerprint read first, `is_current` compares fingerprints; module docstring's residual paragraph rewritten |
| `src/brain/graph_rag/relational.py` | `DocInputs` + `fetch_doc_inputs` replace `fetch_doc_meta` / `key_share_doc_meta` |
| `src/brain/graph_rag/build.py` | `BuildResult.skipped_deleted`; catch `DocumentNotFound` |
| `src/brain/graph_rag/cross_type.py` | module docstring only (residual 4) |
| `src/brain/graph_rag/write_lock.py` | docstring: the re-check covers every plan input, not only the body |
| `tests/test_graphrag_residuals.py` | new: barrier tests for the three ACs + the read-order test + fingerprint unit tests |
| `docs/plans/2026-10-22-graph-entities-deadlock.md` | Residuals section: each residual marked closed or accepted, pointing here |
| `docs/plans/2026-10-23-backlog-reconciliation.md` | H-3 row SHIPPED |
| `CHANGELOG.md` | Unreleased entry |

No file in the CLAUDE.md ceiling table is touched. `reconcile.py` (757, budget +15 reserved for
RM-30) is not touched either.

## Tests (RED first)

All run on the AGE test instance (port 5434). The barriers are the production DI seams:

- AC1 uses the build's `progress` callback. After document A, a worker on its own connection
  deletes document B and the callback waits for that commit.
- AC2 and AC3 use the concept extractor, which runs between plan and write with no transaction
  open. Its hook edits the document from a second connection.

1. **AC1** `test_a_document_deleted_mid_build_is_skipped_deleted`: three documents, ids
   enumerated up front, B deleted after A is processed. The build succeeds with
   `skipped_deleted == 1`, A and C are reconciled, and no graph row names B.
2. **AC2** `test_a_participants_only_edit_between_plan_and_write_invalidates_the_plan`: a Gmail
   document. The hook adds a recipient (same `content_hash`). The reconcile returns `skipped`,
   writes no mention and no watermark, and the next reconcile indexes the new person set.
3. **AC3** `test_a_no_op_recheck_still_passes`: the hook rewrites the same participants and
   changes a non-participant metadata key. The plan is applied (watermark written, mentions
   present).
4. **Read order** `test_the_fingerprint_is_read_before_the_resolver`: a resolver hook edits
   the participants mid-plan. The write must drop the plan.
5. Unit tests for `compute_inputs_fingerprint`: it is stable, it is insensitive to participant
   order and to non-participant metadata, and each of its four inputs moves it.
6. AC1's unit companion: any other `GraphReconcileError` still propagates from `build_graph`.

## Mutation proofs (each in a `git archive` copy, deleted after)

1. `build_graph` no longer catches `DocumentNotFound` (the deleted case raises again).
2. `skipped_deleted` is not incremented.
3. The fingerprint omits the participant keys.
4. `is_current` compares `content_hash` only (the fingerprint is not re-checked at write).
5. The fingerprint includes a value that always differs (a no-op re-check fails).
6. The fingerprint is read after the resolver.

## Risks

- **Over-invalidation.** A plan dropped by the re-check writes nothing, and the next reconcile or
  build indexes the document (the people watermark includes the resolved persons). The fingerprint
  covers only what the plan read, so unrelated metadata writes cannot drop plans.
- **The directory is not in the fingerprint.** `directory_entries` is corpus-wide, not a
  document column. A build reads it once for the whole run (perf Fix B). A directory change
  between plan and write is the same as before this ticket: the next build re-indexes it through
  the people watermark.
- **Surfacing.** The `brain graphrag build` footer (`cli.py`) and the MCP build tool
  (`mcp_server.py`) are not changed. Both files are over the ceiling and outside the ticket's
  scope. `BuildResult.skipped_deleted` is the report the AC names.

## Deviations

1. `src/brain/graph_rag/relational.py` is touched though the ticket's scope lists only
   `build.py`, `reconcile_plan.py` and `cross_type.py`. The fingerprint needs one more read
   (`sources.kind`, `documents.metadata`), so one helper replaces the two doc-meta readers there.
   Both became unused, and keeping them would leave dead code.
2. `src/brain/graph_rag/write_lock.py`'s docstring is touched: it described the re-check as
   covering only the body.
