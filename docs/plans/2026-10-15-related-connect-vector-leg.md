# Related-docs and `connect` vector legs: the design-D scan settings — plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Spec:** `docs/specs/2026-10-15-related-connect-vector-leg-design.md`.
**Branch:** `fix/related-connect-vector-leg` off `second-brain/master` @ `e1fc28e`.
**Constraints:** prod (55432) is read-only, and the probes below use a `read_only`
connection and never `brain search`. Tests use the sibling database `second_brain_t_vecl`
on 5434. Fixtures are synthetic only. `connect.py` is over the ceiling and must not grow.
`related.py` must end under 800.

## 1. Files

| File | Change |
|---|---|
| `src/brain/hnsw_scan.py` | **New.** `HNSW_EF_SEARCH`, `scan_settings()`, `RELAXED_SCAN_SETTINGS`, `STRICT_SCAN_SETTINGS`, `scoped_settings()`. The settings protocol moves here from `search_vector_leg`. |
| `src/brain/search_vector_leg.py` | Uses `scoped_settings(conn, _SCAN_SETTINGS)`. `_SCAN_SETTINGS` stays bound to `RELAXED_SCAN_SETTINGS`, and `HNSW_EF_SEARCH` is re-exported. `_restore_settings` moves to `hnsw_scan`. No behaviour change. |
| `src/brain/related_candidates.py` | **New.** `confidential_clause`, `fts_candidates` (moved verbatim), `vector_candidates_sql` + `vector_candidates` (design D). |
| `src/brain/related.py` | Imports the two legs. The module docstring's ceiling paragraph becomes "back under, by this extraction", with the trail promoted to `7536c4f`. |
| `src/brain/connect.py` | `_SET_ITERATIVE_SCAN` is replaced by `scoped_settings(conn, STRICT_SCAN_SETTINGS)`, and the exactness docstring is corrected. Net growth ≤ 0. Trail promoted to `f4d914d`. |
| `tests/hnsw_fixtures.py` | **New** test helper: the 1024-dim and HNSW production shape, planner pressure, `SettingsProbe`, `index_scan_nodes`. |
| `tests/ordered_ids.py` + six related-ranking test files | **New** helper; the fixtures' chunk inserts use insertion-ordered ids (spec §3.2). Two docstrings corrected. |
| `tests/test_hnsw_scan.py`, `tests/test_related_vector_leg.py`, `tests/test_connect_vector_leg.py` | **New** (§3). |
| `CLAUDE.md` | `related.py` row → "deliberately absent" note. `connect.py` row: promote the `f0abf64` hop and add a descriptive hop. Touch-ups to the two prose paragraphs that name `related.py` as over. |
| Auto-memory `search.md` | Text reported to the team lead; not written by the implementer. |

## 2. Ordered tasks

1. **RED: `tests/test_hnsw_scan.py`.** Fails on import.
2. **RED: `tests/test_related_vector_leg.py`.** The SQL-shape unit tests fail on import.
   The DB tests fail on the old `related._vector_candidates` once they point at it. For the
   red run, `related_candidates.py` first held the OLD query, moved verbatim. Result:
   **39 of 50** candidates, and **22 of 45** with half the corpus confidential.
3. **RED: `tests/test_connect_vector_leg.py`.** It fails on the shipped connect: **18 / 20**
   sources disagree with the Python oracle.
4. **GREEN:** `hnsw_scan.py`; `search_vector_leg` on top of it (the existing
   `tests/test_search_vector_leg.py` must stay green, unedited).
5. **GREEN:** `related_candidates.py`; `related.py` imports from it.
6. **GREEN:** `connect.py` settings + docstring.
7. Docs: the ceiling records (module docstrings + CLAUDE.md).
8. `ruff check`, `mypy src/`, `bin/brain-ci` (full suite of record). Commit.

## 3. Tests and mutation catalog

Each mutation is a literal text replacement in the named file, run with
`PYTHONDONTWRITEBYTECODE=1` against `second_brain_t_vecl`. All five clauses held for
every row on 2026-10-15:
1. the target is green unmutated;
2. the mutation lands its declared count of replacements;
3. the target goes red;
4. the needle appears on a `>`/`E` line;
5. after a byte-identical restore (sha256), the target is green again.

| # | Target test | Mutation (count) | Needle |
|---|---|---|---|
| M1 | `test_hnsw_scan::test_scan_settings_pin_the_design_d_values` | `hnsw_scan.py`: `HNSW_EF_SEARCH = 1000` → `40` (1) | `40 == 1000` |
| M1b | `test_connect_vector_leg::test_embedding_affinity_matches_the_oracle_on_an_hnsw_index` | same as M1 (1) | `Left contains` |
| M2 | `test_hnsw_scan::test_scoped_settings_restore_inside_the_callers_transaction` | `hnsw_scan.py`: the `_restore_settings(...)` call → `pass` (1) | `1000` |
| M3 | `test_hnsw_scan::test_scoped_settings_are_live_inside_the_block` | `hnsw_scan.py`: the `_set_local(...)` call in `scoped_settings` → `pass` (1) | `'40'` |
| M4 | `test_hnsw_scan::test_scoped_settings_restore_inside_the_callers_transaction` | `hnsw_scan.py`: `set_config(%s, %s, true)` → `false` (1) | `77` |
| M5 | `test_related_vector_leg::test_vector_leg_fills_the_pool_in_oracle_order` | `related_candidates.py`: `scoped_settings(conn, _SCAN_SETTINGS)` → `conn.transaction()` (1) | `Right contains` |
| M6 | `test_related_vector_leg::test_confidential_chunks_never_use_up_a_slot` | `related_candidates.py`: drop the F6 clause from the vector CTE (1) | `Left contains` |
| M7 | `test_related_vector_leg::test_iterative_scan_fills_a_gated_pool_beyond_ef_search` | `hnsw_scan.py`: drop the `hnsw.iterative_scan` pair (1) | `Right contains` |
| M8 | `test_related_vector_leg::test_off_corpus_source_reads_at_most_limit_index_rows` | `related_candidates.py`: add the floor inside the CTE, plus its two params (2) | `Rows Removed by Filter` |
| M9 | `test_related_vector_leg::test_floor_admits_exactly_the_rows_above_it` | `related_candidates.py`: `dist <= 1 - %s` → `dist <= 2 - %s` (1) | `Left contains` |
| M10 | `test_related_vector_leg::test_sql_shape` | `related_candidates.py`: `ORDER BY dist + 0, id` → `ORDER BY dist` (1) | `dist + 0, id` |
| M11 | `test_connect_vector_leg::test_embedding_affinity_matches_the_oracle_on_an_hnsw_index` | `connect.py`: `STRICT_SCAN_SETTINGS` → only the `strict_order` pair (1) | `Left contains` |
| M12 | `test_connect_vector_leg::test_knn_query_runs_under_strict_design_d_settings` | `connect.py`: import `RELAXED_SCAN_SETTINGS as STRICT_SCAN_SETTINGS` (1) | `relaxed_order` |
| M13 | `test_related_vector_leg::test_leg_query_runs_under_relaxed_design_d_settings` | `related_candidates.py`: `_SCAN_SETTINGS` with `strict_order` (1) | `strict_order` |
| M14 | `test_connect_vector_leg::test_settings_do_not_leak_from_embedding_affinity` | `connect.py`: `scoped_settings(...)` → `conn.transaction()` + session `SET hnsw.ef_search = 1000` (1) | `1000` |
| M15 | `test_related_vector_leg::test_include_confidential_returns_the_ungated_pool` | `related_candidates.py`: `confidential_clause` always gates (1) | `Right contains` |
| M16 | `test_related_compute::test_compute_related_ranks_title_overlap_first` | `tests/ordered_ids.py`: issue ids in DESCENDING order (1) | `QXBFILLER` |
| M17 | `test_hnsw_scan::test_scan_settings_rejects_an_unknown_mode` | `hnsw_scan.py`: the mode check → `if False:` (1) | `DID NOT RAISE` |
| M18 | `test_hnsw_scan::test_scoped_settings_rejects_an_empty_tuple` | `hnsw_scan.py`: `if not settings:` → `if False:` (1) | `DID NOT RAISE ValueError` |
| M19 | `test_hnsw_scan::test_scoped_settings_revert_when_the_block_raises` | `hnsw_scan.py`: `with conn.transaction():` → `if True:` (1) | `1000` |
| M20 | `test_related_vector_leg::test_zero_norm_chunk_never_passes_the_floor` | `related_candidates.py`: `WHERE dist <= 1 - %s` → the old `WHERE 1 - dist >= %s` (1) | `not in ['` |
| M21 | `test_related_vector_leg::test_floor_boundary_is_inclusive` | `related_candidates.py`: `WHERE dist <= 1 - %s` → `WHERE dist < 1 - %s` (1) | ` in ['` |
| M22 | `test_connect_vector_leg::test_an_under_filled_scan_falls_back_to_the_exhaustive_query` | `connect.py`: on `len(rows) < k`, `return None` → `return _group_per_doc_max(...)` (the old "under-fill is complete") (1) | `== ['` |
| M23 | `test_connect::test_embedding_affinity_forced_truncation_converges_to_exhaustive` | `connect.py`: `below_floor = float(rows[-1][1]) < vector_sim_floor` → `below_floor = True` (1) | `assert [` |

M22 and M23 were added in the second review round, with the under-fill fallback. M1b,
M11, M12 and M14 were re-run after that change, and all still pass all five clauses.
Not guarded: `below_floor = False`. It only removes an early stop, so the loop
doubles k and still converges or falls back. That costs time, never correctness.

M20 and M21 were added in the review round. Before them, both floor-semantics changes
(NaN rejection, and the inclusive boundary) were pinned only by `test_sql_shape`'s
string check. A zero-norm chunk's `1 - NaN >= 0.25` is TRUE in Postgres, so the old
form admitted it on any exact plan. The tests run on the default 4096-dim schema,
where every plan is exact.

M18's needle was first written as `SyntaxError`, predicting that the empty `SELECT `
would fail in Postgres. It does not: a bare `SELECT` is valid and returns zero columns.
The mutation still went red, for the reason the test names, and the needle was
corrected and the row re-run. It is recorded because a wrong prediction that happens
to go red is exactly what clause 4 exists to catch.

M16 is the guard for the fixture change in spec §3.2. It reverses the tie order, and
the title-overlap test goes red deterministically, which proves the ranking fixtures
depend on `tests/ordered_ids.py`.

Not mutated: `tests/hnsw_fixtures.py` (test plumbing whose preconditions are asserted
inside each fixture), and `search_vector_leg`'s move onto `scoped_settings`, which is
covered by its existing, unedited `tests/test_search_vector_leg.py` (44 tests green).

## 4. Read-only prod probe shape (for the revisit in spec §4)

For N source documents sampled by `md5(id)`, use the source's `avg(embedding)` as the
query vector. Compare each leg's SQL under its settings with the same SQL under
`SET LOCAL enable_indexscan = off`. Use a `read_only = True` connection and run no
`brain` command.
