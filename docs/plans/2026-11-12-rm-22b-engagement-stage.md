# RM-22b — Engagement-aware ranking stage (default OFF)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

- **Ticket:** RM-22b, roadmap spec `docs/specs/2026-10-23-roadmap-completion-design.md` §RM-22 / RM-22b,
  Appendix B tracker row (currently "TODO (deferred past 0.4.0; owner decision)").
- **Owner ruling, 2026-11-12:** RM-22b ships **before the `v0.4.0` tag**. That reverses the
  CHANGELOG's "Not in this release" bullet.
- **Base:** `second-brain/master` at `a8b4a1d1`. Branch: `feat/rm-22b-engagement-stage`.
- **Parent shipped:** RM-22a (#88), `brain.engagement` plus `brain usage --engagement`. RM-09 (the
  pipeline) and RM-20 (`doc_leg`, the closest precedent: a re-scoring stage that ships off) are merged.
  RM-14c is deferred.

## Goal

Results the owner opens and rates well rank a little higher. The stage is a `SearchStage` wired once
in `search_pipeline.build_stages`, so it reaches every surface. It ships **off**, and while it is
off every surface's output and `brain eval`'s `config_signature` are byte-identical to today's.

## D0 — the data gate (for team-lead to rule on)

The spec makes RM-22b depend on "RM-22a (gate met)". I cannot read the live `interactions` table:
prod is 55432 and is banned. **Recommendation:** build and merge it **off**, as RM-20 merged off while
its measurement waited on RM-14c. The gate then becomes the precondition for **adoption**, not for
the build. The tracker row becomes `DONE (#PR; off; adoption owed: RM-22a gate + RM-14c)`.
Optionally, team-lead runs the read-only `brain usage --engagement --json` on the live machine and
pastes the gate block into the PR. Nothing in this plan needs that number to merge.

## Files

| File | Change |
|---|---|
| `src/brain/search_engagement.py` | **new leaf**, about 200 lines: constants, one SQL statement, pure functions `engagement_multiplier`, `engagement_order` and `engagement_reach`, and the frozen `EngagementStage`. |
| `src/brain/settings/engagement.py` | **new leaf**, about 50 lines: `EngagementSettings(alpha, beta)`, an `enabled` property, and `load_engagement_settings(env)`. |
| `src/brain/search_pipeline.py` (587 lines) | `build_stages` appends `EngagementStage` **last**, after the reranker (D3). The docstrings name it. About +15 lines, staying under 800. |
| `src/brain/migrations/032_search_queries_session_idx.sql` | **Conditional** (D5): a partial index, only if the benchmark says so. Additive. |
| `tests/engagement_isolation_plugin.py` | **new**: autouse fixture setting `BRAIN_ENGAGEMENT_ALPHA=0` and `BRAIN_ENGAGEMENT_BETA=0`. They are set, not deleted, because of `Config.load`'s `setdefault` (the docleg plugin's reason). |
| `tests/conftest.py` | +2 lines: register the plugin and add its comment line. conftest is in `tests/`, outside the ceiling table's `src/brain` scope, as for the RM-20 precedent. |
| `tests/test_settings_engagement.py`, `tests/test_engagement_order.py`, `tests/test_engagement_stage.py` (DB), `tests/test_engagement_build_stages.py`, `tests/test_engagement_surfaces.py` (DB), `tests/test_eval_runner_engagement.py` (DB), `tests/test_engagement_isolation.py` | new tests (below) |
| `CHANGELOG.md` | entry in `## 0.4.0 - 2026-11-08` (below) |
| `docs/configuration.md` | a new `### BRAIN_ENGAGEMENT_ALPHA / BRAIN_ENGAGEMENT_BETA` section after `BRAIN_DOC_LEG` |
| `docs/cli-reference.md` | one cross-reference line under `brain usage --engagement`: the gate decides adoption of the stage |
| `docs/specs/2026-10-23-roadmap-completion-design.md` | tracker row; §2.8 table only if 032 ships |
| `docs/plans/2026-10-23-backlog-reconciliation.md` | rank 14 row: SHIPPED (off), with the PR number |
| `src/brain/engagement.py`, `src/brain/cli_usage.py` | **untouched**. I import `READ_ACTIONS` from `brain.engagement`, so the meaning of an open and of a rating has one definition. |

**Not touched:** `search.py` (838 lines, in the ceiling table), `cli_search.py`, `mcp_server.py`,
`ui/*`, `recall.py`, `ask.py` and `eval/runner.py`. Every surface gets the stage through
`run_search(stages=None)` or `override_stages`, and eval gets it through `build_stages(env)`. The
surface tests prove that, so no surface needs editing. **Ceiling:** no file crosses 800 and
`docs/ceiling-table.md` is unchanged. I re-derive this with
`find src/brain -name '*.py' | xargs wc -l | sort -rn` before the PR.

## Design

### The aggregate: one statement per search, by primary key

```sql
SELECT i.document_id::text,
       COALESCE(SUM(power(0.5, GREATEST(EXTRACT(EPOCH FROM NOW() - i.at), 0) / %(half_life_s)s))
         FILTER (WHERE i.action = ANY(%(reads)s)
                   AND i.at >= NOW() - make_interval(days => %(window_days)s)
                   AND (i.session_id IS NULL
                        OR (i.session_id IS DISTINCT FROM %(session)s::uuid
                            AND EXISTS (SELECT 1 FROM search_queries s
                                        WHERE s.session_id = i.session_id)))), 0),
       COUNT(*) FILTER (WHERE i.action = 'rated_useful'),
       COUNT(*) FILTER (WHERE i.action = 'rated_irrelevant')
FROM interactions i
WHERE i.document_id = ANY(%(ids)s::uuid[])
GROUP BY i.document_id
```

- **Reads** are `READ_ACTIONS` (`opened`, `clicked`) from `brain.engagement`. `pinned` is neither a
  read nor a rating.
- **Decay:** a 30-day half-life (`ENGAGEMENT_HALF_LIFE_DAYS = 30`). `GREATEST(age, 0)` stops a
  future-dated row (clock skew) from weighing more than 1.
- **Window:** `ENGAGEMENT_WINDOW_DAYS = 300`, ten half-lives, where weight < 0.001. This bounds the
  scan without changing any visible order.
- **Same-session exclusion (AC3):** a read whose `session_id` equals this request's is not counted.
  A `NULL`-session read always counts. `IS DISTINCT FROM NULL` is true, so `session_id=None`
  (`brain eval`) excludes nothing. `run_search` passes a `str`, which the stage parses with
  `uuid.UUID`. A malformed id becomes `None`, which excludes nothing; it can match no row anyway.
- **Ask-citation exclusion (D4, beyond the spec's letter):** `brain ask` (CLI, MCP and UI) writes
  one `opened` row per **citation** under the ask's own session. With the stage on, that is a
  machine-made loop: boost, then retrieve, then cite, then log an open, then boost again. The spec's
  same-session guard does not catch it, because the citation rows are written after the ask returns.
  I reuse RM-22a's own ruling: a read with a session counts only if its session names a logged
  search. That is exactly how `engagement.py` tells the two kinds apart. A heuristic on
  `query IS NULL` would also drop real UI note opens, because the inspector sends no `q`.
- **Ratings** are not decayed or windowed: they are explicit judgements. They are counted as rows,
  so a re-rating appends (`brain_rate`'s documented behaviour), and the clamp bounds the effect.
- Graph-target rows (`document_id IS NULL`) cannot match `= ANY(ids)`.
- **No sensitivity predicate, deliberately,** with a comment as in `search_doc_leg._COSINES_SQL`. The
  statement reads counts for ids the gated search already returned and returns no content. It does
  not filter `draft`, so it is outside the F6 residue grep set. The comment records why anyway.

### The multiplier and the order (pure)

- `engagement_multiplier(opens_decayed, useful, irrelevant, *, alpha, beta) =
  clamp(1 + α·log1p(opens_decayed) + β·(useful − irrelevant), 0.8, 1.25)`. `MIN_MULTIPLIER` and
  `MAX_MULTIPLIER` are named constants. A document with no rows gets 1.0.
- **Positional base, not `SearchResult.score` (D2):** `engagement_order(n, multipliers)` ranks by
  `rrf_contribution(i, k=RRF_K) × m_i` with a stable sort. `score` is no longer monotone in the
  incoming order after the reranker (RM-14a.D1), the doc leg (RM-20 D7) or a `--rewrite` fusion.
  Re-sorting on `score × m` would silently undo every earlier stage. The positional base respects
  whatever order arrived. With k = 60, a 1.25 boost lifts a top row by up to about 15 places, the same
  size as one RRF leg. `score` is not overwritten (the D7 / RM-14a.D1 precedent).
- **Depth:** `candidate_limit(limit) = max(limit, engagement_reach(limit))`, where
  `engagement_reach(limit) = ceil((MAX/MIN)·(k + limit) − k − 1)`, computed with `Fraction`. That is
  the deepest row that can still enter the top `limit` when it is boosted to the maximum and
  everything above it is demoted to the minimum. It is 41 for limit 5, 49 for 10, and 64 for 20.
  This applies only while the stage is on.
- **`applied`:** the stage reports `applied=True` even when no candidate has engagement data, because
  multiplier 1.0 everywhere is a real measurement (D6). Unlike `doc_leg`'s `no_document_vectors`, a
  skip here would make `brain eval` refuse every row of a corpus that has no opens.
- **`signature()`** returns `{"engagement_alpha": α, "engagement_beta": β}`. The stage is built only
  while on, so an off run adds no key (§2.7, AC1).
- `needs_query_vector` is not declared, so the stage runs after a text-only search too. It needs no
  vector.

### Settings leaf

`BRAIN_ENGAGEMENT_ALPHA` and `BRAIN_ENGAGEMENT_BETA` are both `env_float`, default `0.0`, bounds
`[0.0, 1.0]`. Negative values are refused, so the stage can never demote popular documents. `nan`
and `inf` are refused by `env_float`, and the value is never quoted. **`enabled = alpha > 0 or
beta > 0`** (D1). Both knobs are validated even while the stage is off, as for `doc_leg` and
rerank.

## Tests (TDD: each written first, run red, output captured in this plan)

The RED for a new module is its collection `ModuleNotFoundError`, and I also record the first
assertion failure after the stub lands. The RED for wiring is an assertion (the stage is absent).
All data is synthetic: titles like `Probe 3`, UUIDs minted in the test.

1. **`test_settings_engagement.py`** (nodb): defaults are 0/0, off. Each knob alone turns it on.
   Blank means the default. The bounds 0 and 1 are accepted. `-0.1`, `1.01`, `nan`, `inf` and `abc`
   are each refused **without quoting the value**. A bad knob is refused even while the other is 0.
2. **`test_engagement_order.py`** (nodb, pure):
   - The clamp holds at 1.25 and 0.8. **Ordinary:** 1.1 and 0.9 pass through unclamped.
   - No data gives 1.0. α only, β only, and β demotes on net-irrelevant (D1 of the spec).
   - The order is stable on ties. All 1.0 is the identity.
   - **Positional base:** given rows whose `score` is *reversed* against their order (as after a
     reranker), all-1.0 multipliers keep the incoming order.
   - Reach: a row at `reach−1`, boosted to the maximum with every row above it demoted, lands in the
     top `limit`. A row at `reach` cannot. Checked for limits 1, 5, 10, 20 and 50.
3. **`test_engagement_stage.py`** (DB, 5434), the aggregate, each guard beside its ordinary case:
   - An open 30 days old weighs 0.5 ± 1e-6. A fresh open weighs 1.0.
   - A future-dated open weighs exactly 1.0 (guard). **Ordinary:** fresh = 1.0.
   - A 301-day-old open weighs exactly 0 (window). **Ordinary:** a 29-day-old open counts.
   - A current-session open is excluded (AC3). **Ordinary:** an open from another *logged search*
     session counts, and a `NULL`-session open counts (AC3).
   - An ask-citation open (its session is in no `search_queries` row) is excluded. **Ordinary:** the
     same row, once its session has a `search_queries` row, counts.
   - `pinned` and graph-target rows are not counted.
   - A malformed session string excludes nothing.
   - On: a synthetic heavily opened document moves up, by no more than the cap allows (AC2). A
     `rated_irrelevant` document moves down within the clamp (AC4).
   - A `source` filter and the sensitivity lens still exclude a heavily opened document, because the
     stage adds no rows.
4. **`test_engagement_build_stages.py`** (nodb):
   - Off (0/0): no stage, and `build_stages` returns exactly what it does today (AC1).
   - On: one `EngagementStage`, **last**, after `doc_leg` and `rerank`.
   - `--no-rerank` keeps it. `rerank=True` without a backend still raises.
   - `stages=None` builds it from `os.environ`.
   - It runs after a text-only search, because it declares no vector need.
5. **`test_engagement_surfaces.py`** (DB): with `BRAIN_ENGAGEMENT_ALPHA=0.5` in the env, the boosted
   document rises on `brain search`, `brain explain`, MCP `brain_search` and `brain_explain`,
   `GET /api/search`, `recall`, ask's `_retrieve_hybrid`, `mcp_tools.related` and
   `saved_search_check`. Each envelope's `stages` names `engagement`. **Off:** no stage on any
   envelope, and the result lists are byte-identical to an untouched run (AC1).
6. **`test_eval_runner_engagement.py`** (DB):
   - Off: `config_signature` is unchanged (AC1, §2.7).
   - On: the signature carries both keys, and the metric is scored on the boosted order.
   - On with zero interactions: no row is refused (D6).
7. **`test_engagement_isolation.py`** (nodb): a child pytest with both knobs exported to `0.5` and
   `1` sees the stage off. This mirrors `test_docleg_isolation.py`.

**Named repo rules before any push:** `tests/test_log_privacy_rules.py`, `tests/test_log_privacy.py`,
`tests/test_vault_delete.py`, `tests/test_import_hygiene.py`, `tests/test_settings_leaf_guards.py`,
`tests/test_search_pipeline_boundary.py`, `tests/test_repo_hygiene_files.py` and the src-wide scans.
Run as named files under `.claude/full-suite.lock`, with `df -h /` ≥ 6.5 GiB, in my own uv venv,
against my own DB `postgresql://brain:brain@localhost:5434/rm22b_test`. Never the full suite.

## Mutation proofs (each in a `git archive` copy, deleted after; the mutation's presence is grepped before the run)

| # | Mutation | Must fail |
|---|---|---|
| M1 | drop the upper clamp | clamp-at-1.25 and AC2 cap |
| M2 | drop the lower clamp | clamp-at-0.8 and AC4 |
| M3 | drop `IS DISTINCT FROM %(session)s` | current-session exclusion (AC3) |
| M4 | drop the `i.session_id IS NULL OR` branch | NULL-session counts (AC3) |
| M5 | drop the `EXISTS (search_queries)` clause | ask-citation exclusion |
| M6 | `GREATEST(age, 0)` becomes the bare age | future-dated weighs 1.0 |
| M7 | drop the window predicate | 301-day weighs 0 |
| M8 | `ANY(%(reads)s)` becomes "any action" | `pinned` not counted |
| M9 | `enabled` becomes `alpha > 0` | β-only turns on |
| M10 | `build_stages` always appends | off adds no stage / no signature key / isolation |
| M11 | base becomes `result.score` | reversed-score identity test |
| M12 | `engagement_reach` returns `limit` | reach tests |
| M13 | append the stage first instead of last | order-of-stages test |
| M14 | `applied=False` when no data | eval zero-interaction test |
| M15 | isolation plugin body becomes `pass` | child-pytest isolation test |
| M16 | settings max 1.0 becomes none | `1.01` refused |

## CHANGELOG (in `## 0.4.0 - 2026-11-08`; the heading stays unbracketed and its comment untouched)

- **Remove** the RM-22b bullet from `### Not in this release`. The RM-14c bullet stays.
- Upgrade step 6 gets: "So does the engagement stage (`BRAIN_ENGAGEMENT_ALPHA=0`,
  `BRAIN_ENGAGEMENT_BETA=0`)."
- A new `### Added — engagement-aware ranking stage, off by default (RM-22b)`, after the RM-20 entry.
  It covers:
  - what it multiplies, the 0.8 to 1.25 clamp and the 30-day decay;
  - that same-session and ask-citation reads do not count;
  - that it runs last and adds no results;
  - that it ships off and is unchanged while off;
  - that adoption waits for `brain usage --engagement`'s gate and an eval on and off.
- If 032 ships, step 4's "028 to 031" becomes "028 to 032", with one line saying 032 is an index on
  `search_queries`.

## Migration (D5, conditional)

There is no migration by default; the spec says "no migration". The ask-citation `EXISTS` probes
`search_queries.session_id`, which has **no index**, and `search_queries` is never pruned.
`engagement.py` records a SubPlan blow-up at about 300k searches. I benchmark the statement on a
synthetic DB at the RM-58 trigger scale: 1,000,000 `interactions` rows and 300,000 `search_queries`
rows in my own 5434 database, with 50 candidate ids, 200 runs, and the p95 recorded here.

- If p95 ≤ 20 ms (the D2 budget), there is no migration.
- If p95 > 20 ms, add `032_search_queries_session_idx.sql` with
  `CREATE INDEX IF NOT EXISTS search_queries_session_idx ON search_queries (session_id) WHERE session_id IS NOT NULL;`.
  It is additive and idempotent, takes the next free number (§2.8), and updates §2.8 in the same PR.
  `CONCURRENTLY` cannot run inside `brain init`'s transaction, so it takes a brief `SHARE` lock on
  `search_queries`. The writers are stopped at upgrade step 2 anyway.

The materialized table (spec D2) stays out unless the post-index p95 is still over 20 ms. I don't
expect that: it is a primary-key probe over at most about 64 ids. **Disk:** the synthetic tables are
about 300 MB. They are built only with ≥ 6.5 GiB free and dropped right after (`DROP DATABASE` of
*my own* `rm22b_bench` db only).

## Security / privacy

No ingest, no network and no user-supplied text. Interactions are local rows, and only counts are
read. The six classes:

- **Decompression bombs:** N/A, nothing is decoded.
- **SSRF:** N/A, there is no outbound call.
- **Secrets in titles:** none are read, and the stage logs only its class-only skip reason through
  `apply_stages`.
- **RCE via config:** two floats, parsed by `env_float`, bounded, finite-only and never quoted.
- **Unbounded parsing:** the statement is bounded by `ANY(ids)` (≤ reach) and the 300-day window.
- **Over-tight redaction that wipes ordinary data:** the "ordinary" twin of every exclusion (the
  table above) proves that ordinary reads still count.

`session_id` reaches the stage only as server-minted UUIDs. A junk value parses to `None`.

The stage cannot leak through ordering: it reorders only rows the surface already returned under
its own sensitivity lens.

## Risks

- **Rich-get-richer:** bounded by the cap, the decay, the same-session and ask-citation exclusions,
  and RM-22a's 100-document floor before adoption.
- **The eval becomes non-stationary while the stage is on:** it reads `NOW()` and a table every open
  changes. It is off by default, so `ci.json` is untouched. At adoption, `docs/eval-gate.md` gets one
  line: with engagement on, any open or rating moves the baseline. Re-record it, or measure on and
  off in one session.
- **Latency:** extra fetch depth (41 at the MCP default of 5) and one statement, only while on. It
  is measured by the D5 benchmark.
- **Release timing:** this lands after the 0.4.0 notes were cut on 2026-11-08, so the CHANGELOG
  edits above must merge before the tag.

## Decisions (each with my recommendation)

- **D0 gate:** build and merge off now. The gate and the RM-14c measurement are owed for adoption.
- **D1 on/off:** on when α > 0 **or** β > 0. The spec says "α = 0 means off", which holds in the
  default state, and a ratings-only stage (α 0, β > 0) is coherent.
- **D2 base:** the positional RRF base (above), not `SearchResult.score`.
- **D3 position:** **last**, after the reranker. Placed before it, the reranker's full re-sort of its
  head (depth 20) would erase the boost wherever it matters. The boost is a personal prior on top of
  relevance.
- **D4 ask citations:** excluded via the search-session rule. It goes beyond the spec, but closes a
  loop the spec's guard misses. Alternative: count them, as the spec reads literally.
- **D5 index:** benchmark-driven, with 032 only if p95 > 20 ms.
- **D6 `applied` with no data:** `True`.
- **D7 knob bounds:** `[0, 1]` each. Alternative: an upper bound of 5. 1 is already enough to hit
  the clamp at about 0.3 decayed opens.
- **D8 `brain explain` showing the multiplier:** **out of scope.** It needs `SearchExplanation` in
  `search.py`, which is over 800 lines and would owe a growth record. I'll file a backlog line.
- **D9 doctor line:** none, following the `doc_leg` precedent.

## Out of scope

- The materialized table, unless D5 escalates.
- Pruning (RM-58) and `brain explain` fields (D8).
- Turning it on, which needs adoption and an owner opt-in.
- Real Ollama, real accounts and prod data: none are needed. Every test uses the 5434 test DB and the
  fake or word embedders.
- After merge, rule 11 memory updates to `search.md` (stage list) and `types.md` (new dataclasses),
  by whoever closes the ticket.

## Rulings (team-lead, 2026-11-12, on this plan)

- **D0:** merge OFF now, as RM-20 did. Tracker: "DONE (#PR; off; adoption owed: data gate + RM-14c)".
  The owner's read-only `brain usage --engagement --json` is offered separately and does not block.
- **D1, D3, D6, D7, D9:** approved as recommended.
- **D2:** approved; plus a test pinning that a reranker / doc-leg / rewrite order survives the
  stage (all-1.0 multipliers and a reversed `score` keep the incoming order).
- **D4:** approved; the ask-citation feedback-loop test is required.
- **D5:** approved with care: the benchmark DB lives on 5434 under the exact name `rm22b_bench`, is
  created only when `df -h /` shows **≥ 8 GiB** free at that moment, and is dropped by exact name
  immediately after. Numbers recorded here. If p95 > 20 ms, add 032 and tell team-lead at once
  (it becomes an owner prod-migration step).
- **D8:** filed as backlog row **BL-57** in `docs/plans/2026-10-23-backlog-reconciliation.md`.
- **Process:** plan committed first; TDD only at ≥ 6.5 GiB free, under `.claude/full-suite.lock`, in
  my own uv venv; targeted tests plus the named repo-rule tests; never concurrently with another
  teammate's run (the lock serialises them).

## Deviations from this plan (recorded during execution)

- **`saved_search_check` is not a surface.** It calls `run_search(..., stages=())` on purpose: a
  watch is a match set, not a ranking. No stage ever applies there, so it was dropped from
  `tests/test_engagement_surfaces.py`. Every surface that passes `stages=None` or
  `override_stages(...)` is covered.
- **Order key.** `engagement_order` sorts on `m / (k + i + 1)`, one division, rather than
  `m * rrf_contribution(i)`. The two are mathematically equal. The division makes the exact
  boundary tie (`1.25/125` against `0.8/80`, limit 20) round to the same float, so the stable sort
  keeps the incoming order, as `engagement_reach`'s exact `Fraction` bound assumes.
- **Decay in `float8`.** `EXTRACT(EPOCH …)` returns `numeric` on PG 16, and `power(numeric,
  numeric)` is slow. The statement casts to `float8`, and `GREATEST(…, 0)` still makes a
  future-dated row weigh exactly 1.0.
- **conftest growth record.** `tests/conftest.py` keeps its own growth trail in its docstring. The
  plugin is its eighth entry, added there.
- **Commit order for RED.** The tests are committed on their own (`2dc6790a`), then the leaves
  (`bbf4de81`), then the wiring (`08ee7cce`). Each RED is run in a `git archive` copy of the SHA
  before the code it proves: collection errors at `2dc6790a`, assertion failures in the
  pipeline, surface, eval and isolation tests at `bbf4de81`.

## Runs of record (2026-11-12, own uv venv, Python 3.11.15, own DB `rm22b_test` on 5434, under `.claude/full-suite.lock`)

- **RED 1, at `2dc6790a` (tests only):** `4 errors during collection`. Each of
  `test_settings_engagement`, `test_engagement_order`, `test_engagement_build_stages` and
  `test_engagement_stage` fails on `ModuleNotFoundError` for the new leaves.
- **RED 2, at `bbf4de81` (leaves, not wired):** `21 failed, 76 passed`. Every wiring, surface, eval
  and isolation test fails: `build_stages` builds no engagement stage, the surfaces show no lift,
  and `_engagement_off` is not a fixture. The pure and aggregate tests already pass, which is the
  expected split.
- **GREEN, at `08ee7cce`:** `97 passed` across the seven new files.
- **ruff check:** clean. **mypy src/:** `no issues found in 371 source files`.
  **scripts/check_ceiling_table.py:** agrees (16 files over 800, one row each; none of them new).
- **Named repo rules, src-wide scans and every neighbour** that reads `build_stages`,
  `pytest_plugins`, CHANGELOG or the config docs. That is 33 files: `test_log_privacy_rules`,
  `test_log_privacy`, `test_vault_delete`, `test_import_hygiene`, `test_settings_leaf_guards`,
  `test_search_pipeline_boundary`, `test_repo_hygiene_files`, `test_ceiling_table_check`, the
  rerank and doc-leg build/surface/isolation suites, `test_eval_runner_stages`, `test_engagement`,
  and the others listed in the PR. Result: `1002 passed, 1 skipped`.
- **Mutation proofs:** each ran in a `git archive` copy of HEAD. Each anchor was asserted to match
  exactly once and checked as applied before the run, and every named test had to fail. All 17
  were **KILLED**. M12 (`engagement_reach` returns `limit`) first survived in one test of three,
  `test_the_stage_asks_for_at_least_its_reach`, which compared the stage with its own helper. The
  test now pins the value (49) as well (`670a46b5`), and M12 then failed 7 tests.

| # | Mutation | Result |
|---|---|---|
| M1 | upper clamp dropped | KILLED (1) |
| M2 | lower clamp dropped | KILLED (1) |
| M3 | same-session predicate dropped | KILLED (2) |
| M4 | NULL-session branch dropped | KILLED (1) |
| M5 | ask-citation `EXISTS` dropped | KILLED (2) |
| M6 | `GREATEST(age, 0)` dropped | KILLED (1) |
| M7 | 300-day window dropped | KILLED (1) |
| M8 | read-action filter dropped | KILLED (2) |
| M9 | `enabled` = alpha only | KILLED (2) |
| M10 | stage always appended | KILLED (3) |
| M11 | sort on `score` | KILLED (1) |
| M12 | reach = `limit` | KILLED (7, after `670a46b5`) |
| M13 | engagement appended first | KILLED (1) |
| M14 | skip when no data | KILLED (2) |
| M15 | isolation plugin a no-op | KILLED (2) |
| M16 | no upper bound on the knobs | KILLED (2) |
| M17 | malformed session raises | KILLED (1) |
