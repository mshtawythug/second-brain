# Search vector leg: a full, near-exact candidate pool — plan

*Originally planned as an exact scan (option C). Superseded the same day by option D,
a scoped iterative HNSW scan, after the latency gate (spec §4a). Tasks 1–10 below are
the C-era plan, kept as written; the amendment after the guard table records what changed.*

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Spec:** `docs/specs/2026-10-13-search-vector-leg-design.md` (decision: option C, an exact scan
with no per-doc cap).
**Branch:** `fix/search-vector-leg-cap`, based on `chore/eval-ci-baseline-2026-10` (PR #20).
Do not push until PR #20 has merged; then rebase onto master.
**Constraints:** prod (55432) is read-only. Tests run on `second_brain_t_evalc` (5434). There
is no PII in fixtures; use synthetic text only. `search.py` is over the 800-line ceiling and
must not grow.

## 1. Files

| File | Change |
|---|---|
| `src/brain/search_vector_leg.py` | **New.** `vector_leg_sql(*, predicate, query_embedding, vector_sim_floor, limit) -> tuple[str, list[Any]]`. One-line docstring, then the root-cause reasoning. |
| `src/brain/search.py` | Replace the inline `vec_sql` / `vec_params` block with one call. Update the module docstring's ceiling trail. Net line count goes **down**. |
| `tests/test_search_vector_leg.py` | **New.** Unit tests on the SQL builder, plus real-DB tests (§3). |
| `CLAUDE.md` | `search.py` ceiling row: promote the descriptive hop to `826 (f0abf64)` and add a descriptive hop for this change. The `ci.json` item 2 wording if the baseline is re-recorded. |
| `tests/eval/baselines/ci.json`, `README.md` | Re-record only if the eval improves, naming the trigger "a change to search ranking". **This PR must update README "Known retrieval issues"** (per the PR #20 re-review, `6c2cb0a`). Mark the vector-leg entry FIXED, with a pointer to the spec and the new means. Keep its dated, sourced recall figure ("measured 2026-10-13 on the live corpus (1,999 docs / 21,281 chunks) … averaged 0.76"), and keep the caveat that the figure depends on HNSW graph quality. Rebasing onto `6c2cb0a` conflicts in that section; resolve it by merging both changes. |
| Auto-memory `search.md` | Text reported to team-lead; not written by the implementer. |

## 2. Ordered tasks

1. **RED: SQL-builder unit tests.**
   - the ORDER BY is `score DESC, c.id` and never `embedding <=>`. *(C only. Under D the
     inner query is `ORDER BY c.embedding <=> %s::vector` on purpose, and the outer re-sort
     is `dist + 0, id`; see the shape rows in §3.)*
   - LIMIT equals the argument;
   - the params order is right with and without filters;
   - the floor is bound as a parameter, never interpolated.
   Run them: they fail on import.
2. **RED: real-DB regression tests** (`@pytest.mark.fresh_schema`).
   - Build the production shape: `ensure_embedding_column` + `FakeEmbedder(dim=1024)`, then
     `finalize_embedding_index`, so a real HNSW index exists.
   - Ingest synthetic one-chunk docs and set `enable_seqscan = off` on the test connection.
   - (a) The **full pool**: the set of chunks the vector leg contributes equals the exact
     top-`CANDIDATE_LIMIT` set computed independently by the test. *(Under D this holds on
     the 90-row fixture at `ef_search = 1000`, not as a guarantee: D is approximate and its
     recall@50 is measured, spec §5 (ii), §6.5.)*
   - (b) **Filter starvation**: with a tag filter matching a minority of docs, every
     matching chunk above the floor is a vector candidate.
   - (c) The **floor** is still honoured.
   - Before the implementation, run them against the current `search.py` and record that
     (a) and (b) FAIL. This is the bug reproduced.
3. **GREEN.** Add `search_vector_leg.py` and wire it into `hybrid_search`. Run tasks 1 and 2
   plus `tests/test_search*.py`, `tests/test_search_vector_floor.py`, `tests/test_recall*.py`
   and `tests/test_eval*.py`.
4. **Mutation proof.** Apply each mutation by script, confirm it landed (diff non-empty), run
   the targeted test, confirm it fails, then revert:
   - **M1**: restore `ORDER BY c.embedding <=> %s::vector`. Expect test (a), test (b) and the
     shape test to fail.
   - **M2**: drop the `c.id` tie-break. Expect the shape test to fail.
   - **M3**: drop the floor predicate. Expect test (c) to fail.
   - **M4**: drop the predicate's `where_sql`. Expect test (b) to fail.
   Record each result in the commit message.
5. **Ceiling records.**
   - `search.py`'s module docstring and the CLAUDE.md row: trail hop promotion plus the
     new descriptive hop.
   - Confirm `wc -l src/brain/search.py` is below 826 and that the new module is under 800.
6. **Measure on prod (read-only)**, recording all results in the PR body:
   - `brain eval --baseline ci --diff` (per-query deltas and means);
   - `brain explain` on q03, before and after;
   - end-to-end latency over the 31 golden queries, median of 3, before and after.
   - **If the eval regresses on any mean, stop and report.**
7. **Re-record `ci.json`** if it improved. Then `--diff --fail-below` must exit 0. Update
   README §2 and CLAUDE.md item 2: the means and the trigger; the window is unchanged.
8. **Verify.**
   - `ruff check`, and `mypy src/` (2.4.0).
   - The full `pytest` suite on `second_brain_t_evalc`, read from exit-code files.
   - Run `brain doctor` and a sample `brain search`.
9. **Review and audit loop** (CLAUDE.md rule 14) until the code reviewer approves and the
   auditor passes the same commit.
10. **Commit** (conventional, with the `Claude-Session` trailer). Do not push until
    team-lead says to. Then rebase onto master, re-run the suite, push and open the PR.

## 3. Guard table (each must be proven able to fail)

| Guard | Fails under |
|---|---|
| (a) full pool equals the exact top-N (under D: on the 90-row fixture at `ef_search = 1000`, not a guarantee) | M1 (HNSW returns ≤ `ef_search` = 40 rows from a pool of more than 50) |
| (b) filtered query keeps every match | M1 (post-filter starvation), M4 |
| (c) floor honoured | M3 |
| **C only** — shape: no `<=>` in ORDER BY; tie-break present | M1, M2 |
| **D** — shape: inner index-shaped scan (`ORDER BY c.embedding <=> %s::vector` inside a `MATERIALIZED` CTE); outer `WHERE dist <= 1 - %s` and `ORDER BY dist + 0, id` | M2 (no tie-break) |
| zero-norm chunk excluded (review fix; NaN cosine) | M5 (the `1 - (e <=> q) >= floor` form; under D, `1 - dist >= floor`), M3 |
| floor boundary inclusive (audit #6) | M6 (strict `<`), M3 |
| **D (audit, latency):** filtered pool fills with ef_search pinned at 40 | M9 (iterative off), M1, M4 |
| **D:** scan settings never leak (caller txn, fresh session) | M7 (no restore) |

**Amendment, 2026-10-13 (audit):** the latency gate measured C's `vector_ms` p90 at 248.8 ms
(1-min loadavg 67–71), above 150 ms. Per the gate, the leg became option D: an iterative
HNSW scan, `ef_search` 1000, `relaxed_order`, settings scoped and restored, with C's exact
scan as the recall reference. See spec §4a. `SearchDiagnostics.vector_ms` was added so the
trigger measures the leg itself. M8 (the NULL-prior fallback) was an equivalent mutant, and
its redundant code was deleted.

## 4. Out of scope

- `related.py` / `connect.py` share the HNSW shape (spec §8); they are separate follow-ups.
- Any `REINDEX` (spec §7). *GUC tuning was out of scope for C; it became the substance
  of option D (scoped `set_config`, spec §4a), so this line is superseded for D.*
