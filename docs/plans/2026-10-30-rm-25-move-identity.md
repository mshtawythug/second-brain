# RM-25 — Move identity for an id-less replacement file (H-1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-25 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md) (backlog H-1, rank 16).
**Base:** `second-brain/master` `f21198f`. **Branch:** `fix/rm-25-move-identity`.
**Builds on:** PR #52, [`2026-10-22-vault-watcher-move-identity.md`](./2026-10-22-vault-watcher-move-identity.md).
That plan's ruling that an id-less destination keeps the in-place rename is **superseded for id-less files** by this one; its ruling for files with an id (including a non-UUID id) stands.

## Goal

A brand-new file that replaces a note and is renamed within one debounce window never inherits the
old note's identity or backlinks.

## The residue

PR #52 made `_handle_move` delete src's row, instead of renaming it onto dst, when dst's
frontmatter id names a different document. Its ruling kept the in-place rename for an id-less dst,
because `_sync_one`'s recovery branch identifies an id-less file by its path. So: note B synced at
`b.md`; `b.md` deleted; an unrelated, never-synced, id-less file written at `b.md`; renamed
`b.md` → `c.md`. Inside one window the debouncer coalesces delete + upsert + move into one move job.
The rename kept B's row, so `c.md` got B's id and every backlink pointing at B.

## Files

| File | Change |
|---|---|
| `src/brain/vault/move_identity.py` | `is_another_document` sends an id-less dst to the new `_differs_by_content`; the constants `MIN_IDENTITY_SHINGLES` and `MIN_IDENTITY_JACCARD`, each with its rationale. A 46-line leaf before, 97 after; far under the ceiling. |
| `tests/test_vault_watch_move_identity.py` | Thirteen tests (below; two added in review round 1); `_linked_pair` gains an optional `b_body`. |
| `src/brain/vault/watch.py` | **Not touched.** `_handle_move` already passes `conn`, `src_rel` and `dst_abs`, and the row's stored content is one `SELECT` on `conn`. |
| `CLAUDE.md` | The `vault/watch.py` ceiling row's id-less clause reworded (prose only; no hop, count or trail changes). Deviation 5. |

## Design, as built

`is_another_document(conn, src_rel, dst_abs)`:

1. `read_frontmatter_id(dst_abs)` returns an id: **unchanged from PR #52.** The id is compared with
   the row's id as UUIDs; no row at src, or an id that is not a UUID, is not another document.
2. It returns `None` (id-less): `_differs_by_content` decides.
   - No vault row at src: `False`.
   - dst unreadable, or its frontmatter does not parse: `False` (unproven; the sync that follows
     reports the error, as in PR #52's tests (f)).
   - The row's `documents.content` and dst's body after the frontmatter, fence-stripped
     (`strip_fence`, the same projection `sync` stores), are shingled with `near_dup.shingles`
     (lowercased whitespace words, 5-word runs, 64-bit hashes).
   - Fewer than `MIN_IDENTITY_SHINGLES = 8` on **either** side: `False`, the rename is kept.
   - Otherwise `set_similarity.jaccard(stored, incoming) < MIN_IDENTITY_JACCARD (0.2)`: another
     document. `_handle_move` then deletes src's row and `sync_one_file` syncs dst fresh, writing
     its own new id back.

**RM-25.D1, the thresholds.** 8 shingles: a short or empty note has too few for an overlap to mean
anything (one edited word moves up to five shingles, and `jaccard` of two empty sets is 0.0). 0.2:
an editor's atomic save of the same note keeps far more than a fifth of its shingles, so ordinary
renames keep their id and backlinks. **Accepted trade (the ticket's risk):** a long note rewritten
almost entirely *and* renamed within one debounce window is treated as new and loses its backlinks.

## Acceptance criteria → tests

The harness is PR #52's: one coalesced move job through `_worker_loop`, synchronously, on the test
DB. AC1 also drives the debouncer, with hand-fired timers as in
`tests/test_vault_watch_fire_race.py`. No threads, no sleeps.

| AC | Test | At base `f21198f` |
|---|---|---|
| 1. The H-1 sequence no longer carries the old id | `test_ac1_id_less_replacement_renamed_in_one_window_does_not_inherit_identity`: `delete`, `upsert`, `move` scheduled on `b.md`, ONE move job asserted, then the worker. `c.md`'s row is a new id, B's row is gone, `a → c` has no backlink, and `c.md` carries its own id. | **RED**: "the replacement inherited the old note's id" |
| 2. Atomic save of the same content keeps the id | `test_ac2_id_less_atomic_save_of_the_same_content_keeps_the_id` | passes (guard) |
| 3. A rewrite keeping ≥ 20% keeps the id | `test_ac3_rewrite_keeping_exactly_a_fifth_of_the_shingles_keeps_the_id` (40 / 200 = 0.2 exactly, via the worker, backlink kept) and `test_ac3_rewrite_just_below_a_fifth_is_another_document` (39 / 201) | the first passes; the second is **RED** |
| 4. A short or empty note keeps its id and backlinks | `test_ac4_short_id_less_destination_…` (7 shingles vs 36), `test_ac4_short_stored_row_…` (7 vs 36), `test_ac4_empty_id_less_note_…` (0 vs 0), each via the worker with the backlink asserted; `test_ac4_floor_is_eight_shingles_on_each_side` (8 vs 8 unrelated is another document, 7 vs 7 is not); and PR #52's (c) | the four worker tests pass (regression guard); the floor test is **RED** on its 8-shingle half |
| D1 | `test_rm25_constants_are_the_d1_thresholds` | **RED** (no constants) |
| PR #52 unchanged for files with an id | `test_id_bearing_same_id_rewrite_is_not_compared_by_content` (same id, entirely new body: kept) and `test_id_bearing_different_id_with_identical_content_is_another_document` | pass (guards) |

## Evidence

- **RED at base:** the new tests against the unchanged `move_identity.py`: 4 failed, 14 passed
  (D1 constants, AC1, AC3 just-below, the floor's 8-shingle half).
- **GREEN:** `tests/test_vault_watch_move_identity.py` 18 passed. The targeted set (every
  `tests/test_vault_watch*.py`, `test_vault_watcher.py`, `test_vault_sync*.py`,
  `test_vault_frontmatter.py`, `test_near_dup*.py`, `test_set_similarity.py`, 16 files): 370 passed.
  No local full suite (the disk policy); CI is the evidence.
- `ruff check`, `ruff format --check` on the touched files, `mypy src/` (271 files): clean.
  `python scripts/check_ceiling_table.py`: passes. `grep -c '^| .*no commit through' CLAUDE.md`: 1.
- **Mutation proofs**, each in a fresh `git archive` copy of the fix commit, the mutation checked
  to match exactly once and to be applied, `brain.vault.move_identity` checked to load from the copy
  (`PYTHONPATH` pinned), the copy deleted afterwards. Every mutant is killed:

| Mutant | Killed by |
|---|---|
| content check removed (id-less → `False`) | AC1, AC3 just-below, the floor's 8-shingle half |
| floor set to 0 | PR #52 (c), the three AC4 worker tests, the floor test, D1 |
| floor removed | PR #52 (c), the three AC4 worker tests, the floor test |
| floor `min` → `max` (only one side short) | the two one-side-short AC4 tests |
| cut 0.2 → 0.19 | AC3 just-below, D1 |
| cut 0.2 → 0.21 | AC3 exactly-0.2, D1 |
| cut `<` → `<=` | AC3 exactly-0.2 |
| comparison applied to id-bearing files (content decides every dst) | PR #52 (a), both id-bearing guards |
| content checked before the id (content OR id) | the same-id rewrite guard |
| the no-row guard on the id-less path removed (round 1) | the never-synced id-less rename |
| the fence strip removed, `shingles(body)` (round 1) | the derived-fence test |

## Review round 1 (rev-90 at `5341774`)

Two test gaps, tests only; `move_identity.py` is unchanged.

| Finding | Fix | Mutant, now killed |
|---|---|---|
| 1. The id-less path's `if row is None: return False` (a never-synced id-less note renamed) was untested. | `test_never_synced_id_less_rename_syncs_fresh_with_no_row_at_src`: `is_another_document` is False, and the worker syncs dst with its own id and no error. | the guard removed → `TypeError` |
| 2. Deviation 1's fence strip was unpinned: `shingles(strip_fence(body))` → `shingles(body)` survived. | `test_derived_fence_on_the_destination_does_not_count_against_the_note`: dst is the stored body plus a 200-word derived-links fence (about 36 / 236 unstripped); the rename keeps the id and the backlink. | `shingles(body)` |

After the round: 20 passed in the file; all 11 mutants killed, each in a fresh `git archive` copy, deleted afterwards.

## Deviations

1. **The destination's body is fence-stripped before it is shingled.** The ticket says "the destination's body". `documents.content` is stored without the derived-links fence (`sync._normalized_body`), so the body is compared in the same projection; a linker-written "Related" section would otherwise count against the note.
2. **An unreadable destination, or one whose frontmatter does not parse, keeps the rename.** `read_frontmatter_id` reads both as id-less, so they reach the content check. Neither can prove anything, as in PR #52's ruling, and the sync that follows reports the error itself.
3. **A non-string frontmatter id (`id: 123`) gets the content check.** `read_frontmatter_id` applies `sync`'s own validity test (a non-empty string), so such a file is id-less to both.
4. **The harness has no barriers.** PR #52's tests drive `_worker_loop` synchronously with one move job, and AC1 adds hand-fired debounce timers (the `test_vault_watch_fire_race.py` pattern). Nothing runs on a second thread, so nothing needs a barrier, and there are no sleeps.
5. **The stale `CLAUDE.md` clause was corrected; PR #52's plan was left as history.** The `vault/watch.py` row of `CLAUDE.md`'s ceiling table said "An id-less destination ... keeps the in-place rename". That clause now says an id-less destination is compared by content since RM-25 (#90). It is prose only: `watch.py` is not touched, so the row gains no hop and no count or trail changes. `docs/plans/2026-10-22-vault-watcher-move-identity.md` is a record of PR #52 and is deliberately not edited; this plan says it is superseded for id-less files.
