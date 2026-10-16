# Related-docs and `connect` vector legs: the design-D scan settings — plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Spec:** `docs/specs/2026-10-15-related-connect-vector-leg-design.md`.
**Branch:** `fix/related-connect-vector-leg`, cut from `second-brain/master` @ `e1fc28e` and
rebased onto `293a3b9` on 2026-10-15 (PR #34).
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
| `tests/test_search_vector_leg.py` | Review round only: `_floor_hitting_distance_exactly` moved to `tests/hnsw_fixtures.py` (as `floor_hitting_distance_exactly`), shared with the related floor test. No test changed. |
| `tests/test_related_fts_leg.py` | **New** (review round 3): the FTS leg's F6 gate, pinned at the leg. |
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
   `tests/test_search_vector_leg.py` must stay green; its only edit is the review round's
   move of the exact-floor helper into `tests/hnsw_fixtures.py`).
5. **GREEN:** `related_candidates.py`; `related.py` imports from it.
6. **GREEN:** `connect.py` settings + docstring.
7. Docs: the ceiling records (module docstrings + CLAUDE.md).
8. `ruff check`, `mypy src/`, `bin/brain-ci` (full suite of record). Commit.
   **What actually ran.** `bin/brain-ci` could not take the shared `second_brain_test`
   (another session held its lock), so the first commit's gate was a bare `pytest` in
   brain-ci's environment on the sibling DB (1 load-induced watcher-timing failure,
   unrelated, passed 10/10 alone). After the rebase onto `293a3b9`, the gate for the
   review-round commit was replaced, at the coordinator's direction, by three things:
   - 71 targeted test files (everything touching related, connect, the scan settings,
     the search leg, the build-related tests, the fixture helpers, and the
     CHANGELOG/CLAUDE.md/docs readers): 1394 passed, 2 skipped;
   - CI's browser-harness command over all 17 `tests/test_ui_browser*.py` modules:
     230 passed;
   - GitHub CI on PR #34 as the full-suite evidence.

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
| M24 | `test_related_vector_leg::test_ineligible_twins_of_the_source_never_enter_the_pool` | `related_candidates.py`: drop `AND d.vault_path IS NOT NULL` from the vector CTE (1) | `not in ['` |
| M25 | same | `related_candidates.py`: `d.draft = FALSE` → `TRUE` in the vector CTE (1) | `not in ['` |
| M26 | `test_connect_vector_leg::test_ineligible_twins_of_the_source_never_score` | `connect.py` `_KNN_SQL`: `WHERE d.draft = FALSE` → `WHERE TRUE` (1) | `not in {` |
| M27 | same | `connect.py` `_KNN_SQL`: drop `AND d.vault_path IS NOT NULL` (1) | `not in {` |
| M28 | `test_connect::test_embedding_affinity_matches_old_exhaustive_semantics` | `connect.py` exhaustive SQL: `WHERE d.draft = FALSE` → `WHERE TRUE` (1) | `assert [` |
| M29 | same | `connect.py` exhaustive SQL: drop `AND d.vault_path IS NOT NULL` (1) | `assert [` |
| M30 | `test_search_vector_leg::test_filtered_vector_leg_keeps_every_matching_chunk` | `search_vector_leg.py`: drop `{where}` from the CTE, and its params (2) | `Left contains` |
| M31 | `tests/test_search_metadata_filters.py` (file) | `search_predicate.py`: `d.draft = %s` → `d.draft <> %s` (1) | `== ['Draft']` |
| M32 | `tests/test_sensitivity_reembed_and_search.py` (among the sensitivity files) | `search_predicate.py`: `d.sensitivity = %s` → `d.sensitivity <> %s` (1) | `expected only the confidential doc` |
| M33 | `test_connect_vector_leg::test_a_zero_norm_chunk_never_scores_on_the_exhaustive_path` | `connect.py` exhaustive SQL: `(dist) <= 1 - floor` → the old `1 - dist >= floor` (1) | `not in {` |
| M34 | `test_related_fts_leg::test_confidential_document_is_gated_out_of_the_fts_leg` | `related_candidates.py`: drop the F6 clause from `fts_candidates` (1) | `not in [` |
| M35 | `test_connect_vector_leg::test_the_exhaustive_floor_is_inclusive` | `connect.py` exhaustive SQL: `<= 1 - %(floor)s` → `< 1 - %(floor)s` (1) | ` in {` |

M35 was added in the PR #34 re-audit. The rewritten exhaustive floor's inclusive
boundary survived all 101 connect tests under `<`. The test puts the floor exactly on
one doc's distance, computed with the query vector production binds, and checks the
equality in float8.

M33 and M34 were added in the third review round, from the reviewer's findings.
- **M33.** Sending every under-fill to the exhaustive query exposed its NaN-admitting
  floor. The test runs on the default schema, where 5 documents always under-fill.
- **M34.** The FTS leg's F6 gate was invisible to `compute_related`: an FTS-only
  candidate cannot reach `_MIN_RRF_SCORE`. So the gate is pinned at the leg, on a
  confidential document with no embedding.

M24–M32 were added in the third review round (audit finding #4).
- **The gap.** Related's eligibility filters had no deterministic guard. Deleting the
  vault-path predicate survived every related test, and inverting the draft predicate
  was caught only 2 times in 8, by chance.
- **The fix.** The twin tests seed a draft doc and a vault-less doc whose chunk EQUALS
  the source's query vector (cosine 1.0). Losing either predicate therefore puts that
  doc first, every time. The same twins guard connect's KNN SQL. The KNN path really
  runs there: 800 eligible chunks fill k = 400, so there is no under-fill. Connect's
  exhaustive SQL was already guarded through `test_connect`'s `draft_hi`/`novault_hi`
  docs (M28/M29).
- **Search's leg.** It has no eligibility filters of its own: its filters are the
  shared, opt-in predicate. M30 guards their placement inside the CTE. M31/M32 show the
  predicate's draft and sensitivity clauses are guarded by existing filter tests.
- **Needles.** M30's first form dropped `{where}` without its params. It went red on a
  placeholder-count error, which is the wrong reason, so the mutation was made
  faithful. M31's and M32's first needles were too generic and were corrected to the
  failing assertions.

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
covered by `tests/test_search_vector_leg.py` (33 tests) plus `tests/test_search_vector_floor.py`
(11), 44 green together. The leg file's only edit is the helper move noted in §1.

## 4. Read-only prod probe shape (for the revisit in spec §4)

For N source documents sampled by `md5(id)`, use the source's `avg(embedding)` as the
query vector. Compare each leg, under its settings, with an exact reference. Use a
`read_only = True` connection and run no `brain` command. The exact references were not the legs' own SQL. For related it was the same eligibility and F6 filters with the floor in the `WHERE`, ordered by `(dist) + 0, c.id` with `LIMIT 50`. For `connect` it was the exhaustive `MAX(...) GROUP BY` query. Each ran under `SET LOCAL enable_indexscan = off`. Neither shape can be served from the HNSW index, because the `+ 0` defeats index ordering and the aggregate has no distance order at all, so each is exact on any plan.

**Auto-prepare caveat.** Open the probe connection with `prepare_threshold=None`. psycopg prepares a statement after its 5th execution, and from then on a cached plan can outlive a later `SET LOCAL enable_indexscan = off`. A reference that relies on that setting alone then silently stops being exact. The 2026-10-15 probes did not set the threshold. Their references were unaffected only because of the index-proof shapes above. A re-run that uses the legs' own SQL as the reference must set it.
