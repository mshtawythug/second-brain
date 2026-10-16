# Related-docs and `connect` vector legs: the design-D scan settings — design

**Status:** approved by the coordinator (follow-up track A to PR #22).
**Plan:** `docs/plans/2026-10-15-related-connect-vector-leg.md`.
**Precedent:** `docs/specs/2026-10-13-search-vector-leg-design.md` ("design D"), shipped in
PR #22 (`c30449e`) as `src/brain/search_vector_leg.py`.

## 1. Problem

PR #22 fixed `brain search`'s vector leg. Two other chunk-vector queries kept the old shape:

| Site | Shape before this change |
|---|---|
| `related._vector_candidates` | Eligibility filters, F6 gate **and the cosine floor** in one `WHERE`, `ORDER BY embedding <=> q LIMIT 50`, pgvector's default session settings. |
| `connect._embedding_affinity_knn` | Same filter shape without a floor, `LIMIT k` (k = 400 at the default `connect_candidate_limit` of 50), `SET LOCAL hnsw.iterative_scan = strict_order`, adaptive doubling of k, an exhaustive fallback, and a written argument that the result is **exact**. |

On the live corpus (21,286 embedded chunks at the morning probe on 2026-10-15; 21,288 by
the afternoon re-check, every one of them eligible; pgvector 0.8.2) the planner serves both from
`chunks_embedding_idx` (verified with `EXPLAIN`).

## 2. Evidence (measured 2026-10-15)

All prod measurements are read-only: the probe connection had `read_only = True`, ran only
`SELECT`/`EXPLAIN`, and never ran `brain search` (which writes telemetry). The exact
reference for each query is the same SQL under `SET LOCAL enable_indexscan = off`.

### 2.1 `related._vector_candidates` under-returns

30 source documents, sampled by `md5(id)`:

- rows returned: **25–39** in every case, never the 50 the exact scan returns;
- recall against the exact top 50: **0.69 mean, 0.50 minimum**.

This is the PR #22 defect exactly: the HNSW scan hands over at most `hnsw.ef_search` (40)
rows, and every `WHERE` condition, including the F6 gate, is applied after the scan. A
document the F6 gate drops still uses up one of the 40 slots.

Design-D shape (filters inside a `MATERIALIZED` CTE, floor outside, `ef_search = 1000`,
`iterative_scan = relaxed_order`, `enable_seqscan = off`), 60 sources: recall **1.0000
mean, 1.00 minimum**.

**Cost: the same as search's leg.** The query vectors were 40 source averages, and each
was run through this leg and through `search_vector_leg`'s SQL in alternation. The
machine was busy, with a load average of about 30 from other sessions' suites.
Whichever query ran *first* on a vector paid that vector's cache misses:

| Order | related p50 / p90 | search p50 / p90 |
|---|---|---|
| related first | 72–124 / 180–358 ms | 34–53 / 86–123 ms |
| search first | **33–35 / 60–69 ms** | 51–86 / 113–188 ms |

On one warm source, `EXPLAIN (ANALYZE, BUFFERS)` gives the same plan shape for both:
an HNSW index scan feeding the `LIMIT` (related adds a memoized `documents_pkey` probe).
Related took 15.6 ms and 5,593 + 99 buffer hits; search took 20.5 ms and 5,972 hits.
The old related query was about 20 ms p50 because it stopped after 40 rows. The extra
cost is the `ef_search = 1000` walk, which buys the full pool.

The planner uses the index for this leg on 20 of 20 sampled sources, with or without
`enable_seqscan = off`. The setting is kept for uniformity, and it is load-bearing for
`connect` (§2.2).

### 2.2 `connect`'s "exact" KNN is not exact

The exactness argument in `_embedding_affinity_knn` starts from "with `strict_order` the
`LIMIT k` rows are the true k nearest eligible chunks". That premise is false. The
`strict_order` mode guarantees the rows come back **in** distance order. It does not make
them the true nearest: HNSW is approximate at any `ef_search`, and pgvector's README says
`strict_order` gives *lower* recall than `relaxed_order`. Over 200 sources, the output of
the shipped loop compared with the exhaustive `MAX(...) GROUP BY` query:

| Settings | Byte-identical (ids, order, scores) | Same doc set | Mean doc-set recall | Min |
|---|---|---|---|---|
| default `ef_search` 40, `strict_order` (shipped) | 106 / 200 | 117 / 200 | 0.9800 | 0.72 |
| `ef_search` 1000, `strict_order`, `enable_seqscan` off | **199 / 200** | 199 / 200 | 0.9999 | 0.98 |
| `ef_search` 1000, `relaxed_order`, `enable_seqscan` off | 59 / 60 (sample of 60) | — | — | — |

The top 3 matched in every case, so the visible effect is in the tail of the 50.
Timing over 60 sources (KNN loop including doublings): ef 40 p50 17.7 / p90 38.8 ms;
ef 1000 strict p50 20.3 / p90 42.8 ms; the exhaustive query p50 176 / p90 259 ms.

`enable_seqscan = off` is load-bearing here. At `ef_search = 1000` without it, the
planner switches the `LIMIT 400` KNN to a sequential scan: 733 ms, against 127 ms with
it, in one `EXPLAIN ANALYZE` on a cold source.

The existing equivalence tests in `tests/test_connect.py` could not see this. The test
database's column is `vector(4096)`, above pgvector's 2,000-dim HNSW cap, so it has no
HNSW index and every query there is exact.

### 2.3 Reproduced on the test database (5434)

The tests rebuild the production shape under `fresh_schema`: a 1024-dim column, the real
HNSW index, and session `enable_seqscan`/`enable_sort` off, because a small table is
otherwise planned as an exact sequential scan (the same "planner pressure" PR #22 uses).
They assert, as a precondition, that the old query shape is served by the index.

- related: 90 one-chunk documents. The old leg returns at most 40 candidates where the
  oracle has 50. With every other document confidential it returns about 20 where 45 are
  eligible.
- connect: 200 documents × 4 chunks of seeded Gaussian 1024-dim vectors. The shipped
  loop disagreed with a Python-computed oracle for **18 of 20** sources at
  `candidate_limit = 50` (identical on two consecutive runs; the vectors are seeded).
  With 800 chunks and `ef_search = 1000` the scan reaches the whole graph, so the fixed
  loop must equal the oracle exactly.

## 3. Decisions

### 3.1 One shared settings primitive: `brain/hnsw_scan.py` (new)

`search_vector_leg.run_vector_leg` already does the hard part: it reads the prior values,
sets them with `set_config(…, is_local => true)` inside its own `conn.transaction()`, and
restores them explicitly, because a released savepoint keeps local settings. Copying that
into two more modules is the drift risk. It moves to a new module:

- `HNSW_EF_SEARCH = 1000` (moved; `search_vector_leg` re-exports it).
- `scan_settings(iterative_scan) -> tuple[(name, value), ...]` for
  `relaxed_order` / `strict_order`. `RELAXED_SCAN_SETTINGS` and `STRICT_SCAN_SETTINGS`
  are the two instances.
- `scoped_settings(conn, settings)`, a context manager over any number of settings,
  with the same read / set / restore protocol and the same SQL as before.

`search_vector_leg` keeps `_SCAN_SETTINGS` as a name bound to `RELAXED_SCAN_SETTINGS`,
looked up at call time, so its existing tests, which substitute that attribute, keep
testing the leg. Behaviour of `brain search` is unchanged.

### 3.2 related: adopt design D

- Filters (draft, `vault_path`, self-exclusion, `embedding IS NOT NULL`, **and the F6
  predicate**) go inside the `MATERIALIZED` CTE, so the iterative scan keeps walking
  until `CANDIDATE_LIMIT` *eligible* chunks are found. A confidential chunk can never use
  up a slot, and it never reaches the outer query.
- The floor goes outside, written `dist <= 1 - floor`. Inside the CTE, an off-corpus
  source would walk the index up to `hnsw.max_scan_tuples`. The `<=` form rejects
  zero-norm (NaN) rows. Same reasoning as `search_vector_leg`.
- The outer query re-sorts by `dist + 0, id` (relaxed order, total order).
  **Consequence for tests, found while running them:** the old leg had no tie-break, so
  tied cosines came back in physical row order, which is insertion order. Six ranking
  fixtures (`test_related_compute`, `test_build_related{,_signal,_schema,_idempotence}`,
  `test_ui_routes_related`) give two or more candidates the *same* vector and rely on
  that order without saying so. Rank fusion cannot be neutral about a tie: in
  `_seed_a_b_c`, if C's chunk sorts first, B's one-rank FTS lead and C's one-rank
  vector lead cancel exactly (`1/61 + 1/62` each), and the title sort puts C first.
  With random chunk UUIDs those tests became a coin flip: 1–4 failures per run, varying
  between runs. The production tie-break stays: it makes the related list
  deterministic across rebuilds, which matters for duplicate chunks such as quoted
  email threads. The fixtures instead get chunk ids in insertion order
  (`tests/ordered_ids.py`), which makes their unstated assumption explicit, and the two
  docstrings that claimed a shared vector makes the vector leg "identical" now say what
  actually happens.
- `relaxed_order`, not strict: this is the same RRF-candidate role as search's leg, and the
  recall figures above were measured on that shape.

**Extraction.** `related.py` was over the ceiling at 851 lines, and its docstring said to
extract before the next addition. Both candidate legs (`_fts_candidates`,
`_vector_candidates`) and the `_confidential_clause` they share move to a new
`brain/related_candidates.py`. The FTS leg moves unchanged. The vector leg is rewritten
there. `related.py` drops under 800, so its CLAUDE.md row is replaced by a
"deliberately absent" note, like `backup/restore.py`'s.

### 3.3 connect: keep the algorithm, fix its settings and its claim

The adaptive loop, the early-stop rules and the exhaustive fallback stay. Only the
scan's settings change: `STRICT_SCAN_SETTINGS` (`ef_search = 1000`,
`iterative_scan = strict_order`, `enable_seqscan = off`) through `scoped_settings`,
replacing the single `SET LOCAL`.

- **`strict_order` stays.** The loop's early stop ("the smallest returned cosine is
  already below the floor") and the per-doc-max argument both depend on the rows arriving
  in distance order. The two modes measured the same on recall.
- **`ef_search = 1000`** is what moves the result from 106/200 to 199/200.
- **`enable_seqscan = off`**, because a higher `ef_search` raises the index scan's
  estimated cost (PR #22, spec §4a). A sequential plan would be exact but slow, about the
  exhaustive query's 176 ms.
- **The docstring is corrected.** The exactness argument now holds *relative to the rows
  the scan returns*, and the result is near-exact, with the measured figures above.

**Rejected: always run the exhaustive query.** It is exact by construction, but costs
about 9× at p50 (176 ms against 20 ms per document, so roughly 5.5 minutes against
40 seconds for a full ~2,000-document refresh). The near-exact path loses one document
from the tail of one 50-row list in 200, and those are ranking inputs for review
suggestions, not results anyone reads directly.

`connect.py` must not grow. Its trail hops are promoted in two places, which had
different trailing hops. In the module docstring the trailing descriptive hop was
`f4d914d` (1,091 lines). In CLAUDE.md's row, `f4d914d` was already SHA-bound and the
trailing descriptive hop was the 2026-09-02 merge, now promoted to `f0abf64` (1,091,
no line changed).

**An under-fill now concedes to the exhaustive query (review round).** The shipped
loop treated a batch of fewer than k rows as "every eligible chunk seen". The
iterative scan also stops at `hnsw.max_scan_tuples` (20,000 visited tuples, and
approximate), so a short batch proves nothing about exhaustion. (An earlier draft of
this round "justified" the premise by saying all eligible chunks fit under the cap.
That is false: prod had 21,288 embedded chunks on the 2026-10-15 afternoon re-check,
all eligible, against a cap of 20,000.) Now `len(rows) < k` returns "not
converged", and the caller runs the exact `_embedding_affinity_exhaustive`. This
costs nothing on the live corpus, where every embedded chunk is eligible and k
(≤ 400·2³ = 3,200) fills long before the cap. On a corpus too small to fill k (demo,
tests) the exhaustive query is cheap by definition. The early-stop argument no longer
depends on `max_scan_tuples` at all. The test forces a real under-fill by lowering
`ef_search` and the cap to 1 (91 of 400 rows). At ef 40 / cap 100, pgvector still
returned all 400 rows, so the precondition asserts the short batch first.

**The exhaustive query's floor (review round 3).** Sending every under-fill to
`_embedding_affinity_exhaustive` exposed its floor: `1 - dist >= floor`, which admits
a zero-norm chunk because `1 - NaN >= x` is TRUE in Postgres. The NaN-scored document
then sorted FIRST under `ORDER BY cosine DESC` and took a candidate slot. On the base
branch a small corpus never reached that query: its under-fill was taken as complete,
and the Python floor dropped the NaN. So this was a regression this change introduced,
on exactly the corpora (demo, fresh installs, under 400 eligible chunks) that now take
the exhaustive path. The floor is now `dist <= 1 - floor`, the same form as the other
two legs, and a test on the default schema pins it. No other SQL floor of the old form
remains in `src/`. The Python-side floors in `connect` and `review/scans` compare with
`>=` / `<`, where a NaN compares False and is dropped.

## 4. Revisit

Re-measure with the read-only procedure in §2, using
`docs/plans/2026-10-15-related-connect-vector-leg.md` §4 for the probe shape, when any of
these happen:

- pgvector is upgraded past 0.8.x;
- the corpus doubles;
- `search_vector_leg`'s own revisit trigger fires. It shares these settings.

Open the probe connection with `prepare_threshold=None`. Otherwise psycopg's
auto-prepare (after 5 executions) can keep a cached plan past a later
`SET LOCAL enable_indexscan = off`, and the "exact" reference stops being exact.
Plan §4 explains why the 2026-10-15 references were unaffected.

## 5. Out of scope

- `graph_rag/global_.py` orders `community_summaries` by `summary_embedding <=>`, a
  separate table of a few hundred rows with no metadata filter. It cannot under-return
  through filtering. Not touched.
- No eval re-baseline. `brain search` ranking is unchanged, and related docs and connect
  suggestions are not in the golden eval.
