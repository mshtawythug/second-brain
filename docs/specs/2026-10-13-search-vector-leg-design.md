# Search vector leg: exact candidate scan — design

**Date:** 2026-10-13 · **Status:** implemented as **option D** (§4a). Option C (§4) was approved and implemented first, then superseded the same day after the latency audit · **Branch:** `fix/search-vector-leg-cap`
**Scope:** `hybrid_search`'s vector leg only. `related.py` and `connect.py` share the
pattern and are listed as follow-ups (§8), not changed here.

## 1. Problem

`hybrid_search` fuses two candidate legs of up to `CANDIDATE_LIMIT = 50` chunks each.
The vector leg does not deliver what its SQL asks for:

- **It returns ~40 rows, not 50, on every unfiltered query.** Measured on the live
  corpus (21,281 chunks, 31 golden queries): 40–42 rows on all 25 unfiltered queries.
- **It starves on filtered queries.** The 6 queries carrying `source_kind` or
  `since_days` got **14, 19, 21, 30, 33 and 40** rows. The two `recency` queries got 19
  and 14.
- **Its top 40 are often not the nearest 40.** Against an exact scan, the mean
  recall@50 of the current leg is **0.757**, and the minimum is **0.00**: on one
  interview query, not one returned chunk is among the true nearest 50.
- **Symptom that surfaced it.** On the SEO-strategy query (q03), the result list shrank
  from 17 documents (2026-09-21) to **6** after the 2026-10-05/08 ingests. All 40
  vector rows came from 6 new Confluence docs, contributing 13/12/8/3/3/1 chunks.
  `brain explain` before the fix shows 6 results, all vector-only (FTS matched nothing),
  with a top cosine of 0.31, and **0 of the 9 judged-relevant docs**.

## 2. Root cause (verified)

The vector leg (`search.py`, the `vec_sql` block in `hybrid_search`) is

```sql
SELECT … FROM chunks c {join} WHERE {filters AND} 1 - (c.embedding <=> q) >= floor
ORDER BY c.embedding <=> q LIMIT 50
```

That `ORDER BY` is exactly the shape the HNSW index `chunks_embedding_idx`
(`001_init.sql:41`, defaults `m=16`, `ef_construction=64`) serves, so the planner uses
it (confirmed with `EXPLAIN ANALYZE`: `Index Scan using chunks_embedding_idx … rows=42`).
pgvector documents the consequences (`/pgvector/pgvector` README via ctx7, 2026-10-13):

- `hnsw.ef_search` defaults to **40**: "Results are limited by the size of the dynamic
  candidate list (`hnsw.ef_search`), which is 40 by default." A `LIMIT 50` therefore
  gets at most ~40 rows.
- "With approximate indexes, filtering is applied *after* the index is scanned." Both
  the metadata predicate and the cosine floor are filters, so a filtered query keeps
  only the fraction of those ~40 that match. That explains the 14–33 rows.
- Approximate means approximate. On this index, recall@50 against exact (index forced,
  iterative scan on) is ef=40 **0.757**, ef=64 0.801, ef=100 0.858, ef=200 0.910 and
  ef=400 **0.994**. At every ef up to 200, the minimum is 0.00.

The per-document crowding seen on q03 is an artefact of that low recall, **not** a
property of the true neighbourhood. An exact scan of q03 returns 50 chunks over 25
documents, not 6.

## 3. Options considered

| | Option | Measured outcome |
|---|---|---|
| A | **Per-doc chunk cap** on the vector leg, mirroring FTS's `PER_DOC_CHUNK_CAP=3` | Treats the symptom: on HNSW it still reads the same wrong 40. On an exact scan (A+C) it is **worse than C alone on all three means**, and MRR falls below the current baseline (0.3685 vs 0.3697). Rejected. |
| B | **`SET LOCAL hnsw.ef_search` ≥ the candidate limit** (+ `hnsw.iterative_scan` for filters) | Raising ef_search moves pgvector's cost estimate and the planner **switches to a sequential scan** (EXPLAIN: `Seq Scan … 449 ms` at ef=100). Keeping the index needs `enable_seqscan = off` as well. That makes three GUCs, and `SET LOCAL` outlives a savepoint `RELEASE`, so it leaks into the caller's transaction unless each value is saved and restored. Recall is 0.86 at ef=100 and 0.994 at ef=400, and it depends on graph quality, which is how this defect stayed silent. Rejected for now (§7). |
| A+B | Both | Inherits B's mechanism and A's measured harm. Rejected. |
| **C** | **Exact scan.** Order by the score expression, which the index cannot serve | Recall 1.0 by construction. Every query gets its full 50 rows, filtered ones included. **Better than baseline on all three means** (§6). Costs latency (§5). |

## 4. Decision (first version, superseded by §4a)

**C: make the vector leg an exact scan.** No per-doc cap, and no GUCs. *Kept here as
the reasoning of record. C is still the recall reference that §4a is measured
against.*

The query orders by `score DESC, c.id` (`score = 1 - (c.embedding <=> q)`). An HNSW
index serves only `ORDER BY embedding <=> q`, so the planner cannot pick it, regardless
of `ef_search`, table statistics or session settings. The `c.id` tie-break makes the
order total, so equal-cosine chunks rank deterministically. The SQL moves to a new
module, `brain/search_vector_leg.py` (§9), with the reasoning recorded there.

Why C over B, with B's latency advantage on the table:

1. **Correct by construction, not by tuning.** The defect was a silent recall loss that
   no test and no metric reported for months. B fixes today's number and re-arms the
   same failure: graph quality degrades with inserts, and nothing measures it.
2. **Filters stay exact.** Under C a filter can only remove non-matching rows, never
   rows it was not shown.
3. **No transaction side effects.** `hybrid_search` runs on caller-owned connections
   (CLI, MCP, `brain ask`, the UI, the eval harness). C changes one SQL string; B changes
   session state.
4. **Precedent.** CLAUDE.md already accepts a sequential cosine scan at personal-corpus
   scale for the `qwen3` backend, which has no HNSW index at all.

The HNSW index stays: `related.py` and `connect.py` still order by it.

## 4a. Decision revised (2026-10-13, after the latency audit): D, an iterative HNSW scan

**What changed.** The audit measured C under load and found its leg time over budget.
`vector_ms` (added to `SearchDiagnostics` for this purpose) was then measured with the
§5 procedure. No lull came within the 20-minute wait, so the conditions are recorded
next to the numbers:

| Measurement | Conditions | Leg p50 / p90 | Recall@50 vs exact (mean / min) | Eval means |
|---|---|---|---|---|
| Old (HNSW, pgvector defaults) | (earlier, unrecorded) | 3.7 ms median | 0.757 / 0.00 | 0.2589 / 0.3697 / 0.5852 |
| C, exact scan (`vector_ms`, real code path) | 1-min loadavg 67–71, 10 pytest procs | **186.8 / 248.8 ms** | 1.0 (reference) | 0.2779 / 0.3826 / 0.7112 |
| C, exact (interleaved with D, raw SQL) | loadavg 57 → 100 | 174.9 / 258.8 ms | 1.0 | — |
| D at ef=100 / 200 / 400 (raw SQL) | same window | 19 / 25 / 26 ms p50; 39 / 42 / 69 ms p90 | 0.870 / 0.910 / **0.990**; min 0.00 / 0.00 / 0.90 | — |
| **D at ef=1000** (raw SQL) | same window | **41.6 / 81.3 ms** | **0.9987 / 0.98** | — |
| **D at ef=1000 (`vector_ms`, real code path)** | loadavg 82–96 (heavier than C's run), 7 pytest procs | **35.9 / 52.7 ms** | 0.9987 / 0.98 (§5 snippet, verbatim) | **0.2779 / 0.3826 / 0.7112** |

C's leg p90 was over the 150 ms bar under the only conditions available. Cost grows
linearly with chunk count, so there is no headroom on today's 21k chunks. The rule set
for this decision was: above 150 ms, take the best-recall option that stays fast, keep
recall ≥ 0.99 and keep the eval means ≥ C's. **D at ef=1000 meets all three.** Its eval
means equal C's exactly: 0 of 31 queries differ on any metric, and 29 of 31 top-20 lists
are identical (the other 2 differ only in order, at positions that do not change a
metric). ef=400 also clears 0.99, but only just, with a 0.90 minimum; ef=1000 is
pgvector's maximum.

**D, precisely** (`brain/search_vector_leg.py`):
- The leg's own `conn.transaction()` sets three settings locally with
  `set_config(…, true)`: `hnsw.ef_search = 1000`, `hnsw.iterative_scan = relaxed_order`
  and `enable_seqscan = off`. pgvector's iterative scan docs were checked via ctx7.
- `enable_seqscan = off` is needed because a raised `ef_search` raises the index scan's
  estimated cost, and the planner would otherwise sequential-scan.
- The scan sits in a `MATERIALIZED` CTE, re-sorted outside by `dist + 0, id`.
  `relaxed_order` may return rows slightly out of order, and the `+ 0` is pgvector's
  documented Postgres 17+ pattern.
- The floor stays in the NaN-safe form `(e <=> q) <= 1 - floor`.

**How D answers the reasons C was chosen (§4):**
1. *Correct by tuning, so it must be measured.* Recall is no longer correct by
   construction. §5 therefore adds a read-only recall@50-vs-exact procedure with a 0.99
   revisit trigger, so a degrading graph is detectable rather than silent. It was
   undetectable for months before this change.
2. *Filters stay complete.* The iterative scan keeps scanning until `limit` rows survive
   the filters (bounded by `hnsw.max_scan_tuples`, default 20,000, about the whole
   table). A test pins `ef_search` at 40 and proves a filtered pool still fills. Any plan
   that does not use the index is exact.
3. *No transaction side effects.* The prior values are read first and restored after the
   query, because a released savepoint keeps local settings. On error, the rollback
   reverts them. A NULL prior means pgvector has not loaded yet in the session (it is not
   preloaded); `set_config(name, NULL, true)` then resets to the session default. Three
   tests cover the autocommit path, a caller's own transaction with prior local values,
   and a fresh session. Mutant M7 (no restore) is killed.

## 5. Risks

- **Latency.** The vector-leg SQL goes from 3.7 ms to ~106 ms median (p90 ~120 ms). End
  to end, `hybrid_search` is **35 ms median / 51 ms p90 today**, so expect ~135 ms. The
  exact scan is linear in chunk count, about 5 ms per 1,000 chunks at 1024 dims on this
  machine. That is fine for the CLI, MCP and `brain ask`, whose LLM calls take seconds.
  It is noticeable but acceptable for the UI's debounced search.
  **Measured after implementation** (§6.3): end to end it is **161 ms median /
  234 ms p90**, above the ~135 ms estimate; `sql_ms` is 113 ms median / 148 ms p90.
- **Revisit triggers (D).** (i) `SearchDiagnostics.vector_ms` p90 over the 31 golden
  queries above **150 ms**, or (ii) mean recall@50 against the exact scan below **0.99**.
  `vector_ms` times only the leg's candidate query. It was added for this; the earlier
  `sql_ms` proxy, which included the FTS leg and the doc fetch, is retired. Both
  procedures are read-only and run from a checkout with the prod `.env` loaded and the
  local golden corpus present. Neither uses `brain search`, which writes a
  `search_queries` telemetry row. **Record the conditions with every measurement**: the
  1-minute load average before and after, and the number of concurrent test processes.
  Measure at the quietest moment available.

  *(i) Latency.* One warm-up pass, then 3 runs per query. Keep the per-query median; p90
  is the 28th of the 31 sorted medians.

  ```python
  import os, statistics, psycopg, yaml
  from brain.config import Config
  from brain.embeddings import make_embedder
  from brain.search import SearchDiagnostics, hybrid_search
  cfg = Config.load(); emb = make_embedder(cfg)
  qs = yaml.safe_load(open("tests/eval/golden_corpus.yaml"))["queries"]
  def run(conn, q):
      d = SearchDiagnostics()
      hybrid_search(conn, embedder=emb, query=q["query"], limit=20,
                    source_kind=q.get("source_filter"), tag=q.get("tag_filter"),
                    since_days=q.get("since_days"), vector_sim_floor=cfg.vector_sim_floor,
                    recency_halflife_days=cfg.recency_halflife_days,
                    snippet_context_tokens=cfg.snippet_context_tokens, diagnostics=d)
      return d
  print("loadavg before", os.getloadavg())
  with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
      conn.execute("SET default_transaction_read_only = on")
      for q in qs:
          run(conn, q)                                   # warm-up
      med = [statistics.median(run(conn, q).vector_ms for _ in range(3)) for q in qs]
  print("loadavg after", os.getloadavg())
  s = sorted(med); print("vector_ms p50", s[15 - 1], "p90", s[28 - 1])
  ```

  *(ii) Recall.* Per golden query: D's chunk ids (from `run_vector_leg`) against the
  exact top 50 for the same predicate and floor. The exact reference orders by the score
  expression, which no HNSW index can serve. This is option C's SQL.

  ```python
  import os, statistics, psycopg, yaml
  from brain.config import Config
  from brain.embeddings import make_embedder
  from brain.search_predicate import build_predicate
  from brain.search_vector_leg import run_vector_leg
  cfg = Config.load(); emb = make_embedder(cfg)
  qs = yaml.safe_load(open("tests/eval/golden_corpus.yaml"))["queries"]
  recalls = []
  with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
      conn.execute("SET default_transaction_read_only = on")
      for q in qs:
          p = build_predicate(source_kind=q.get("source_filter"), tag=q.get("tag_filter"),
                              since_days=q.get("since_days"))
          v = emb.embed([q["query"]], input_type="query")[0]
          d_rows, _ = run_vector_leg(conn, predicate=p, query_embedding=v,
                                     vector_sim_floor=cfg.vector_sim_floor, limit=50)
          fp = "(c.embedding <=> %s::vector) <= 1 - %s"
          where = f"WHERE {p.where_sql} AND {fp}" if p.has_filters else f"WHERE {fp}"
          exact = conn.execute(
              f"SELECT c.id FROM chunks c {p.join_clause} {where} "
              "ORDER BY 1 - (c.embedding <=> %s::vector) DESC, c.id LIMIT 50",
              [*p.where_params, v, cfg.vector_sim_floor, v]).fetchall()
          ex = {r[0] for r in exact}
          recalls.append(len({r[0] for r in d_rows} & ex) / max(len(ex), 1))
  print("recall@50 mean", statistics.mean(recalls), "min", min(recalls))
  ```
- **`fts_only` / `BRAIN_EMBEDDER=none`** skip the vector leg entirely, so they are
  unaffected.
- **Metric movement is intended.** Ranking changes, so `ci.json` must be re-recorded on
  this branch. The trigger is named "a change to the ranking code (SQL or fusion)",
  which is now listed in both re-record trigger lists (README, CLAUDE.md).
- **Test-DB realism.** The test corpus is tiny, so the planner would sequential-scan
  anyway, and a naive test could pass on the old SQL. The regression tests therefore set
  **both** `enable_seqscan = off` **and `enable_sort = off`** on their own connection.
  `enable_sort = off` is the setting that actually forces the plan: with only seqscan
  disabled, the 90-row table planned the *filtered* query as a nested loop over
  `documents` plus an exact sort, which is exact by accident and let the old SQL pass.
  A precondition asserts the old query shape is served by `chunks_embedding_idx`, filtered
  and unfiltered. The tests are mutation-proven against the old `ORDER BY` (§9).
- **NaN rows (found in review, fixed).** A zero-norm embedding has a cosine distance of
  NaN. Postgres orders NaN above every number and evaluates `NaN >= x` as TRUE, so the
  first form of the exact scan (`1 - (e <=> q) >= floor`, `ORDER BY score DESC`) would
  have ranked such a row **#1**. The HNSW path never returned it, because pgvector does
  not index zero vectors for cosine. The floor is now `(e <=> q) <= 1 - floor`, and
  `NaN <= x` is FALSE. Prod held **0** zero-norm (and 0 NULL) chunks on 2026-10-13, so
  this was latent. A regression test (RED first) and mutant M5 guard it.

## 6. Measurement — before / after, same corpus, same day

Spike measurements (2026-10-13, live corpus 1,999 docs / 21,281 chunks; the spike SQL
was reverted before implementation):

| Means (nDCG@5 / MRR / Recall@20) | |
|---|---|
| Baseline `ci.json` (HNSW) | 0.2589 / 0.3697 / 0.5852 |
| **C: exact** | **0.2779 / 0.3826 / 0.7112** |
| A+C: exact + cap 3 | 0.2625 / 0.3685 / 0.6767 |

Under C, 16 queries move. The large gains are on queries where HNSW recall was near
zero: q12 nDCG +0.63 and R@20 +1.0, q01 R@20 +1.0, q03 R@20 +0.89. The losses are real
and are itemised in §6.1. *(This paragraph originally called them "re-rankings among
documents the judgments never saw … not a recall loss". That was wrong on both counts,
and was corrected after review.)*

### 6.1–6.3 Implementation results (2026-10-13, same corpus, back to back)

The old code, run from the PR #20 worktree immediately before, reproduced the
baseline exactly (0.2589 / 0.3697 / 0.5852), so the document set had not moved.

1. **Eval:** **0.2779 / 0.3826 / 0.7112**, identical to the spike. 16 queries moved.
   The means improve on all three metrics. **Three categories lose on at least one
   metric:**

   | Category (n) | Before nDCG@5 / MRR / R@20 | After | Δ |
   |---|---|---|---|
   | semantic (9) | 0.4369 / 0.5741 / 0.6650 | 0.5074 / 0.6750 / 0.9304 | +0.0705 / +0.1009 / +0.2654 |
   | interview (5) | 0.3097 / 0.3622 / 0.7500 | 0.3620 / 0.3582 / 0.9000 | +0.0524 / **−0.0040** / +0.1500 |
   | meeting (5) | 0.1310 / 0.2412 / 0.5467 | 0.0690 / 0.1411 / 0.5333 | **−0.0619 / −0.1002 / −0.0133** |
   | email (4) | 0.0000 / 0.0729 / 0.5000 | 0.0000 / 0.0806 / 0.7500 | 0 / +0.0076 / +0.2500 |
   | source-specific (4) | 0.2429 / 0.3531 / 0.7500 | 0.2429 / 0.3460 / 0.7083 | 0 / **−0.0071 / −0.0417** |
   | recency (2) | 0.2648 / 0.2500 / 0.1429 | unchanged | 0 / 0 / 0 |
   | people (2) | 0.1952 / 0.5357 / 0.1932 | 0.1952 / 0.5417 / 0.1932 | 0 / +0.0060 / 0 |

   **Named recall losses: three judged docs fell out of the top 20**, each from the tail
   (ranks 16, 18 and 19):
   - q13 (interview) lost `c1648384`, displaced by 3 Krisp transcripts ingested
     2026-10-05 and 1 markdown doc ingested 2026-04-30.
   - q15 (meeting) lost `06d4ba1c`, displaced by 9 entrants: 6 markdown docs ingested
     2026-04-30, 1 from 2026-07-13, and 2 Confluence pages from 2026-10-08.
   - q26 (source-specific) lost `2771ddf9`, displaced by 2 emails ingested 2026-04-28.

   Against that, **17 judged docs entered the top 20** across q01 (5), q02 (1), q03 (8),
   q12 (1) and q21 (1).

   **Why q15 regressed: two mechanisms, neither of them "unjudged new docs".**
   - *Exact neighbours that HNSW never surfaced.* `f25525e1`, `559bbf47` and `2414da2a`
     are judged-relevant for **q00** (offline-first mobile app). `a1a4afd3`, from
     2026-07-13 and unjudged, is in the same cluster. They were ingested 2026-04-30 or
     2026-07-13, so they **predate the judgments**. Their best-matching chunks are dense
     with "status" (4–12 occurrences), and the embedder puts them closer to "daily
     engineering standup blockers and updates" (cosine 0.41–0.42) than q15's own judged
     transcripts (the lost `06d4ba1c` scores 0.332). They now hold vector ranks 1, 2 and
     4 and land at overall ranks 3–10. In every HNSW-era q15 result list we hold
     (2026-09-21 and 2026-10-13), none of them appears in the top 20, so they were very
     likely never pooled for q15 rather than assessed and rejected; no rejection is
     recorded. By the query's intent they read as precision losses: the vector leg now
     returns the embedder's true nearest neighbours, and for this query those are
     status-tracking docs, not standups.
   - *Recency boost on deep vector ranks.* The new #1 for q15, `2ece0575`, is a Confluence
     page ingested 2026-10-08 that sits at **vector rank 37** (cosine 0.353). HNSW's
     40-row pool never contained it. In the full 50-row pool its RRF score is small, but
     its recency boost (0.981, versus ~0.63 for the June transcripts) lifts it past
     stronger matches. `ced40638` (rank 43) does the same at #4.

   **Net justification.** The pool is now what the SQL asks for: the true top 50, with
   every filter exact. All three means improve: nDCG@5 +0.019, MRR +0.013, R@20 +0.126.
   The judged-doc ledger is +17 / −3. The cost is real: a meeting-category regression
   driven by true-but-off-intent neighbours and by recency-boosted deep ranks, plus small
   MRR and R@20 losses in interview and source-specific. That is accepted as the price of
   a correct candidate pool. Both q15 mechanisms are ranking issues *downstream* of the
   pool, recorded as follow-ups in §8.
   - `ci.json` was re-recorded on this branch, with the trigger named "a change to the
     ranking code (SQL or fusion)". The diff gate exits 0.
2. **`brain explain` q03:** before, 6 results (all vector-only), top cosine 0.310,
   **0 of 9** judged-relevant docs. After, 20 results, top cosine **0.473**, **8 of 9**
   judged-relevant at ranks 5–15. The HNSW scan had missed the true nearest
   neighbours entirely.
3. **Latency:** see §5 and §6.4.

### 6.4 Latency gate (audit), real code path, 31 golden queries

Measured with the §5 procedure (warm-up, then the median of 3 runs per query). No
1-minute load average below 5 occurred in either 20-minute wait, so both runs happened
under heavy contention from concurrent test suites. D's run had the *higher* load.

| | Conditions (1-min loadavg before → after; pytest procs) | `vector_ms` p50 / p90 / max | `sql_ms` p50 / p90 | `total_ms` p50 / p90 |
|---|---|---|---|---|
| C, exact scan | 67.5 → 71.1; 10 | 186.8 / **248.8** / 470.7 | 206.3 / 265.2 | 483.5 / 689.9 |
| **D, iterative HNSW, ef=1000** | 92.2 → 81.7; 7 | 35.9 / **52.7** / 103.5 | 43.8 / 60.1 | 127.8 / 229.6 |

Gate: a vector-leg p90 of at most 150 ms. C fails at 248.8 ms; D passes at 52.7 ms under
heavier load. Quiet-machine figures for D are expected to be lower still; re-measure
with §5 and record the conditions.

The plan for measuring the implementation (kept for reference):

1. `brain eval --baseline ci --diff` on the fix branch, reporting per-query deltas and
   means. **If quality improves, re-record `ci.json`; if it regresses, stop.**
2. `brain explain` on q03, before and after: the result count, how many judged docs
   appear, and the leg each result came from.
3. End-to-end `hybrid_search` latency (`SearchDiagnostics.total_ms` / `sql_ms`) over the
   31 golden queries, median of 3, before and after.

## 7. Revisit criteria (not in scope)

D's two triggers (§5) answer different failures:

- **Recall below 0.99** means the HNSW graph has degraded (with ingests), and ef_search
  is already at its maximum. Remedies: a `REINDEX` with higher `m` / `ef_construction`,
  as a prod write the user runs deliberately; or C's exact scan if the corpus is still
  small enough for it.
- **`vector_ms` p90 above 150 ms** means even D is too slow: lower ef_search with
  measured recall, or partition the scan.
- Not done here, and worth doing: wire the §5 recall check into `brain doctor`, so
  approximate recall can never degrade silently again. *(Updated from the C-era text,
  whose trigger was a `sql_ms` proxy and whose remedy was "take option B with a guard",
  which is what §4a now is.)*

## 8. Follow-ups found during this investigation

- `related.py:510` and `connect.py:378` use the same `ORDER BY embedding <=> q` + `WHERE`
  floor shape. They are subject to the same 40-row cap and post-filter starvation.
  Not measured; each needs its own eval before changing.
- **Recency boost × a full pool (q15).** Now that the pool reaches vector rank 50, the
  post-RRF recency boost (half-life 180 d) can lift a freshly ingested, weakly matching
  chunk (rank 37, cosine 0.353) above strong older matches. Candidate remedies: a cosine-
  or rank-gated boost, or boosting before fusion. Each needs its own eval.
- **Off-intent true neighbours (q15).** Status-tracking docs are the embedder's genuine
  nearest neighbours for a standup query. That is a property of the embedder and the
  corpus, not of the pool. If q15 is re-judged against the full pool (the ~30% unjudged
  caveat), assess these explicitly rather than inheriting their absence from the
  HNSW-era pool.

## 9. Implementation shape

- **New module `src/brain/search_vector_leg.py`** (under 800 lines): `vector_leg_sql()`
  builds the SQL and its params from the shared `SearchPredicate`, the floor and the
  candidate limit; `run_vector_leg()` executes it under the scoped scan settings (§4a)
  and returns the rows plus the leg's own elapsed time, which feeds
  `SearchDiagnostics.vector_ms`. It carries the reasoning above in its docstring.
- **`search.py` gets smaller**, not bigger: the inline `vec_sql` block is replaced by
  one call. `vector_ms` added 2 lines back; the file is still net below 826. Per CLAUDE.md's ceiling rule, the `search.py` row's trailing descriptive hop
  is promoted to `826 (f0abf64)`, a fresh descriptive hop is added for this change, and
  the module-docstring trail is updated to match.
- **Tests (real Postgres, fake embedder):**
  - full-pool count under `enable_seqscan = off` (the 40-cap regression);
  - a filtered query returns every matching chunk above the floor (the starvation
    regression);
  - ordering is exact and deterministic (C: `score DESC, id`; D: inner index-shaped scan,
    outer `dist + 0, id`);
  - the floor and predicate are still applied, and the floor boundary is inclusive (the
    k-th chunk exactly at `1 - floor`, verified equal in float8, is in; the (k+1)-th is out);
  - a zero-norm (NaN-cosine) chunk never enters the leg (added after review);
  - D only: with `ef_search` pinned at 40, a filtered pool still fills (iterative scan),
    and the scan settings never leak (autocommit, inside a caller's transaction with prior
    local values, and in a fresh session before pgvector loads);
  - `vector_ms` is set by the leg, is ≤ `sql_ms`, and is `None` under `fts_only`.
  Mutation harness for D: M1 pgvector defaults, M2 no tie-break, M3 floor neutralised,
  M4 predicate dropped, M5 NaN-admitting floor, M6 strict `<`, M7 no restore, M9
  iterative off. Each is killed by exactly its declared tests. An M8 (break the
  NULL-prior fallback) survived as an *equivalent* mutant, because
  `set_config(name, NULL, true)` already resets to the session default. The redundant
  fallback was deleted rather than kept untestable.
