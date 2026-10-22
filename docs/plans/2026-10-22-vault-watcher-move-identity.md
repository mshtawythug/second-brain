# Vault watcher: a move keeps the right document's identity: plan and proof

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `fix/vault-watcher-move-identity`, base `second-brain/master` @ `7acf133`
(PR #51 merged). This closes handoff follow-up 4 in
`docs/handoff/2026-10-13-merge-all-handoff.md`: "a coalesced move can carry the
wrong document's row". That residue was recorded honestly in
`docs/plans/2026-10-22-vault-watcher-shutdown-mixed-actions.md` (rows
upsert → move and delete → move), and its audit, F1, found it.

## Problem

A pending `upsert` or `delete` of P coalesces into a later `move` P → Q, which
is correct in the common case (an atomic save that keeps the frontmatter id).
But suppose P had been re-populated by a DIFFERENT document B (another
frontmatter id) before the rename. Then `_handle_move` ran a path-only

```sql
UPDATE documents SET vault_path = Q WHERE kind = 'vault' AND vault_path = P
```

which renamed the previous document A's row to Q. It then synced Q:

- **Q carries id B.** `sync_one_file` inserts B at Q, which collides with A's
  row on `documents_vault_path_idx`, so the worker logs an error and B is not
  indexed. A's row now points at a file that is not A.
- **Q is id-less.** This is the residue's other half, and it is resolved below
  by a ruling.

## Fix

In `_handle_move`, before the path-only `UPDATE`, compare the destination's
frontmatter id with the id of the vault row at src:

- **The ids differ.** Q is a different document. Do NOT rename A's row:
  `_handle_delete(src)` removes it, exactly as the coalesced delete would have.
  Then the existing `sync_one_file(Q)` upserts B by its own id. The identities
  are correct, and there is no index collision.
- **The ids are equal**, or there is no row at src: it is a plain rename. Keep
  the in-place `UPDATE`, which keeps the document id and every incoming
  backlink. This is the common case, and it is unchanged.
- **Q has no frontmatter id.** Keep the rename (the ruling below).

**The comparison is UUID-normalised.** `documents.id` is a `uuid` column, and
`sync` passes the frontmatter id straight to Postgres. A frontmatter id that
differs only in case (or format) from the row's id is therefore the SAME
document. Comparing raw strings would turn every rename of such a file into a
delete plus insert, and lose its backlinks. Test (d) pins this.

**One parser.** The id is read by a new `read_frontmatter_id(path)` in
`brain/vault/frontmatter.py`. It wraps the existing `parse_frontmatter` and
applies the same validity test `_sync_one` uses (`isinstance(raw_id, str) and
raw_id`). It returns `None` for an absent or invalid id, an unreadable file, or
malformed frontmatter. In that last case `sync_one_file` reports the
malformed-frontmatter error itself, as before.

### Ruling: an id-less destination keeps the in-place rename

The evidence is in `_sync_one` and `_write_frontmatter_back`
(`src/brain/vault/sync.py`):

1. A synced file normally carries its id on disk, because the first sync
   writes it back. But the write-back is skipped deliberately on a concurrent
   edit with malformed frontmatter, and it fails on an I/O error. Both paths
   log that the DB row is committed and "next sync will detect the missing
   id". So "committed row, id-less file" is a known, supported state.
2. For that state, `_sync_one`'s recovery branch identifies an id-less file BY
   PATH: it reuses the id of the vault row at the file's path. Treating an
   id-less file at a row's path as that row is therefore the codebase's
   existing identity rule.
3. An id-less file cannot prove it is a different document. Renaming it in
   place keeps the backlinks of the case the recovery branch exists for. The
   following sync then writes A's id back to Q.

Two alternatives were rejected:

- **Comparing content hashes:** an edit followed by a rename (exactly the
  coalesced upsert → move) changes the hash, so it would break the identity
  of the common case.
- **Treating id-less as a different document:** it contradicts the recovery
  branch, and it loses the backlinks of a failed write-back.

The residue that remains is a brand-new, never-synced, id-less B that
replaces P and is renamed within one debounce window. It adopts A's id, as it
did before. It is recorded in the handoff.

## Tests (TDD, RED first at `7acf133`)

The tests are DB-backed, against the test DB on 5434, in a new
`tests/test_vault_watch_move_identity.py`. They drive `_worker_loop`
synchronously with a single `move` job, which is what the debouncer delivers
after coalescing.

| Id | Scenario | Assertions |
|---|---|---|
| (a) | A synced at P. P deleted, recreated as B (a different id), renamed P → Q, then one coalesced move job | No error; A's row is not at Q (A is gone, as the delete intended); B is the row at Q, with its own id |
| (b) | A same-id rename b → b2, with `a → b` linking | b's row id unchanged, now at b2; the incoming backlink `a → b` survives |
| (c) | An id-less rename: b's id stripped from disk (a failed write-back), renamed b → b2 | Renamed in place: same row id at b2, the backlink survives, and the sync writes the id back |
| (d) | A same document whose frontmatter id differs only in case | Renamed in place, and the backlink survives |

(a) is the bug, so it is RED at `7acf133`. (b), (c) and (d) are characterisation
tests of behaviour that must not change, so they pass at `7acf133`. The
mutants below show each can fail.

## Ceiling

`watch.py`'s CLAUDE.md row ends in a descriptive hop ("the
shutdown-and-mixed-actions commit") that covers two commits, which now have
SHAs and counts: 1,158 (`87784fe`) and 1,159 (`e41e301`, the last to touch
the file). This change:

- promotes the hop to those two SHA-bound hops;
- adds its own descriptive hop;
- records the growth inline at `_handle_move` and in the module docstring.

The comparison itself goes to the new leaf `brain/vault/move_identity.py`, so
`watch.py` grows only by the branch. `frontmatter.py` is well under 800.

## Risks

- **An extra file read and query per move.** It is one `SELECT` by
  `vault_path` (indexed), plus one read of the destination, which
  `sync_one_file` reads again anyway. Moves are rare relative to edits.
- **The delete branch runs `_handle_delete`.** That is vault-tier only and
  graph-sync aware, and it is the same call a non-coalesced delete makes. No
  `graph_rag` file is touched.
- **The id-less residue** is recorded above and in the handoff.

## Proof

### TDD: RED at `7acf133`, then GREEN

`read_frontmatter_id` landed first: a pure reader over the existing parser,
with no behaviour change. The four tests were then run against `7acf133`'s
`_handle_move`:

- **(a) RED** at its first, targeted assertion: "the previous document's row
  was renamed onto the destination".
- **(b), (c), (d) passed.** They are characterisation tests of the common case
  and of the id-less ruling, which must not change.

With the identity check, all four pass.

### Placement and ceiling

The comparison lives in a new leaf, `src/brain/vault/move_identity.py`
(`is_another_document`, 44 lines). `watch.py` therefore gains only the
branch, its reason, an import and one module-docstring line. A first draft
kept the helper in `watch.py` (+32 lines) and was moved out under the ceiling
guidance.

The CLAUDE.md `vault/watch.py` row now ends in SHA-bound hops:

- 1,158 (`87784fe`);
- 1,159 (`e41e301`, the last to touch the file before `7acf133`);

followed by a descriptive hop for this commit. The growth is recorded inline at
`_handle_move`, in the module docstring, and in the row.

### Mutation matrix (five clauses), in a `git archive` copy only

The harness was the PR #51 design, with the move-identity file added (it is
DB-backed, so the test-DB env is set). The snapshot was `git stash create`
`c58a540`, archived and extracted, so the live worktree was never mutated.

- **(b)** Each edit's `str.count` is asserted to be its declared 1.
- **(c), (d)** One run per mutant over all three test files, with per-test
  failures read from JUnit XML. A mutant is OK only if the set of failing tests
  EQUALS the prediction, and each failure message contains its predicted text.
- **(a)** A control run with no mutation has 0 failures.
- **The checker can fail:** two negative controls are rejected.
- **Restoration:** the archive was restored byte-identical, and the harness
  asserts it.

**All 26 mutants OK:** N1-N4 new, plus all 22 from PRs #49 and #51, re-run
against this code.

| Mutant | Edit | Failing tests (exactly) |
|---|---|---|
| N1 | always rename (`if is_another_document(...)` becomes `if False:`) | (a) "the previous document's row was renamed onto the destination" |
| N2 | an id-less destination counts as another document | (c) "an id-less rename did not keep the row" |
| N3 | compare ids as raw strings, not UUIDs | (d) "a case-only id difference broke the rename" |
| N4 | the different-document branch skips `_handle_delete` | (a) "the deleted document's row survived" |

**Each claim has its own killing mutant (clause e):**

- "a different document is not renamed onto": N1 → (a);
- "its old row is deleted": N4 → (a);
- "id-less keeps the rename": N2 → (c);
- "ids compare as UUIDs": N3 → (d).

The same-id common case (b) is held by every mutant except those that break it.

**Equivalent mutant, recorded rather than tested:** changing "no row at src →
False" to True. With no row at src, the `UPDATE` and `_handle_delete(src)` are
both no-ops, so no test can tell them apart. The `return False` stays, because
it skips a pointless delete.

### Stability and suites

- The four move-identity tests, plus the race and timer files, passed 5 runs in
  a row (36 passed each).
- Run once, serially: the watcher, sync and docs-guard files gave 391 passed.
  They include `test_vault_watcher.py`, `test_vault_sync.py` (because
  `frontmatter.py` changed) and `test_reading_surface_pointers.py`.
- The full suite was not run locally; CI is the full-suite evidence.
