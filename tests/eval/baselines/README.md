# Eval baselines

`brain eval --record-baseline NAME` writes `<NAME>.json` here. Local baselines are
gitignored; explicitly-named committed ones are allowlisted in this directory's
`.gitignore`, one file at a time (`ci.json` today). Never blanket-allow.

## Got `exit 3`? Read this before blaming the ranker

`brain eval --baseline ci --diff --fail-below` exits `3` when any mean metric —
nDCG@5, MRR, or Recall@20 — drops by more than `1e-4` against the baseline. That
is *usually* a regression in search. It is not always. Check these first, in
order of how often they are the real cause:

**1. The baseline is keyed to one machine's brain.** `ci.json` stores the
document UUIDs it ranked. On a different machine, or after a destructive
re-ingest, none of them resolve, every metric scores `0.0`, and the diff reports
a catastrophic regression that has nothing to do with retrieval quality. A run of
all-zero metrics is the signature. Confirm with `brain status` — if the document
count is not the one the baseline was recorded against, that is your answer.

A document set that has only *grown* resolves every UUID but still moves rows:
new documents compete for the top ranks against judgments that never saw them.
*The first 2026-10-13 re-record (corpus growth) was this case, not recency
drift (2) alone.* Ingests added 608 documents to the document set (Krisp on
2026-10-05, a Confluence import on 2026-10-08) and moved 12 non-recency rows as
well; the means fell from 0.4834 / 0.5835 / 0.7267 to 0.2589 / 0.3697 / 0.5852
(nDCG@5 / MRR / Recall@20). Those are no longer the committed file's means: a
second re-record the same day followed a ranking change, so for the current
means see "Vector-leg candidate pool" under "Known retrieval issues" below. The golden judgments predate ~30% of the document set, so the
absolute scores understate quality until `golden_corpus.yaml` is re-judged
against the new documents. As of 2026-10-21 only the two `recency` rows have
been (see "Re-judged 2026-10-21" in (2)); the other 29 rows' judgments still
predate those documents.

**2. Two `recency` queries were clock-dependent until 2026-10-15**, when they
were pinned to absolute dates (see "The fix" and "Applied" below). They filtered on
`documents.ingested_at` via `since_days: 40`, so the matched set is stable only
while `today - 40 days` stays inside one gap in this brain's ingest history.
The baseline was re-recorded on **2026-10-13**, when that boundary sat inside a
21-day gap (last ingest 2026-08-31, next 2026-09-21), which made it stable
from roughly **2026-10-10 to 2026-10-31**. While the queries were on
`since_days` (until 2026-10-15), outside that range one or both of them drifted
on their own and the baseline needed re-recording, with nothing about search
changed. That no longer applies to the committed baseline: both queries are
now pinned, and the clock does not move them. *(The first recording,
2026-09-02, sat in a different gap and drifted on 2026-09-20 exactly as this
paragraph predicted — one query, `-0.15` nDCG@5, every other row unchanged to
four decimals.)*

**The gap protects only the trailing edge.** The SQL below finds when old
documents cross the 40-day boundary. It says nothing about the leading edge:
every newly ingested document lands inside the window at once. That mattered for
one query in particular. The daily-brief query's judged documents are all daily
briefs (28 then; 27 since the 2026-10-21 re-judge), and on 2026-10-14 its top
20 was all daily briefs too. The vault watcher syncs a brief note
(`daily/<year>/<date>-brief.md`) on most days, and while that query was on
`since_days` each one was an unjudged competitor for it. Re-recorded
**2026-10-14** for this reason: after a restart the watcher synced 7 notes (6
briefs and `index.md`), which moved that query alone (nDCG@5 0.5296 → 0.3601,
Recall@20 0.2857 → 0.2143) and the means by −0.0055 nDCG@5 / −0.0023 Recall@20,
with every other row at ±0.0000. In the `since_days` era (until 2026-10-15) the
same happened on most days a brief landed, and an exit 3 in which only that
`recency` row moved was this drift, not a regression. The other `recency` query
(the planning query) had 3 briefs in its top 20 and was not moved by those 6.

**Since the pin, that reading is wrong.** Both `recency` queries now end at a
`before` that is already past (`2026-10-15` for the daily-brief query; since
the 2026-10-21 re-pin below, `2026-09-02` for the planning query), so a newly
synced brief, dated on or after that day, is outside their range and cannot
compete. A pinned row moves only when the
document set changes *inside* its range, or, rarely, through the approximate
vector leg (see "The fix" below). An exit 3 in which only a `recency` row moved
is therefore **no longer automatically the known drift: investigate it** before
re-recording. Check whether anything with a `coalesce(sent_at, ingested_at)`
inside the range was added or deleted, then treat it like any other row that
moved.

**The fix: pin the recency queries to absolute dates (2026-10-15).** A corpus
entry can now carry `after:` / `before:` (`YYYY-MM-DD`) in place of
`since_days:`. These are `hybrid_search`'s own `after` / `before` filters on
`coalesce(sent_at, ingested_at)`: `after` is inclusive and `before` is
exclusive, both at UTC midnight. A closed range does not move with the clock,
on either edge, so a pinned query's candidate set depends only on the
document set, not on the clock. That is trigger (1), and it is a real
re-record. Usually only an ingest *inside* the range moves the row, but not
always: the vector leg is an approximate HNSW iterative scan, with the date
filter applied inside the walk (`src/brain/search_vector_leg.py`, recall@50
mean 0.9987, minimum 0.98, against an exact scan). An ingest *outside* the
range still changes the index graph, so it can, rarely, change which in-range
chunks reach the leg's top 50. The
recency boost does not bring the clock back: `0.5^(age/halflife)` scales every
candidate by the same factor as `now` advances, so the ranked order, which is
all the metrics read, is unchanged, *provided no candidate is dated after
`now`*. The boost clamps a future document's age to 0, which breaks the
identity. A `before` that is already in the past guarantees the condition. See
`docs/specs/2026-10-15-eval-recency-determinism-design.md` §2.2.

`load_corpus` enforces the rules that keep a pinned query pinned:

- `after` requires `before`, because an open upper bound is the leading-edge
  drift above.
- `after` must be earlier than `before`.
- Neither can be combined with `since_days`.
- Values must be plain dates, so `datetime`s and the ISO basic form are
  refused.
- `before` alone is allowed.
- `since_days` must be a positive `int`. `true`, `40.7` and `'40'` used to be
  coerced (to 1, 40 and 40); `0` and negatives used to pass through unchanged
  (`0` as no window at all, `-5` as a window starting in the future). All of
  them are now refused. This is stricter than before: a corpus that loaded with
  one of those values now exits `1` until the value is fixed.

A positive-int `since_days` still loads, for corpora that have not been
converted, and still drifts exactly as described above.

To convert, edit your local `tests/eval/golden_corpus.yaml`, which is
gitignored, so no PR does it for you. On each `recency` entry, replace

```yaml
    since_days: 40
```

with

```yaml
    after: 2026-09-05    # example: before - 40 days (the half-open range is 40 days)
    before: 2026-10-15   # example: <= TODAY's UTC date when you re-record, never later
```

`before` must not be later than the UTC date on which you re-record, so its
midnight is already past. With a `before` of tomorrow, a brief ingested after
the recording but before UTC midnight lands inside the "pinned" range, and the
drift comes back for good. Briefs ingested earlier on the re-record day are
left out; that is the intended trade-off.

Then **re-record `ci.json`**. A converted row filters a different column
against a fixed range, so its numbers move once, by design. Before
re-recording, check that every judged document of a pinned query falls inside
its range. This query lists the ones that do not. Fill in that query's
`expected_doc_ids` prefixes and its two dates:

```sql
SELECT left(id::text, 8) AS id8, coalesce(sent_at, ingested_at) AS ts
FROM documents
WHERE left(id::text, 8) = ANY (ARRAY['abcd1234', 'ef567890'])
  AND NOT (coalesce(sent_at, ingested_at) >= timestamptz '2026-09-05 00:00Z'
       AND coalesce(sent_at, ingested_at) <  timestamptz '2026-10-15 00:00Z');
```

An empty result means the range covers the judgments. A judged document
outside the range can never be retrieved, so it caps that query's recall.

**Applied 2026-10-15.** Both `recency` entries were converted to
`after: 2026-09-05` / `before: 2026-10-15`, and `ci.json` was re-recorded. The
check above was **not** empty: 18 of the daily-brief query's 28 judged
documents and all 5 of the other query's fall before 2026-09-05. The
`since_days` window at the previous recording already excluded the same set.
That was measured, not inferred from the ingest gap: the gap is in
`ingested_at`, and the new filter's `coalesce(sent_at, ingested_at)` has no gap
there (29 documents fall in [2026-09-01, 2026-09-21) on that column). The
reachable judged sets were checked under both filters and are identical: 10 of
28 and 0 of 5, the same documents, so the conversion lost no reachable
judgment. Both rows stayed
capped until the 2026-10-21 re-judge below. The means were unchanged at
**0.2724 / 0.3826 / 0.7089**, and only the second query's top 20 moved: four
Krisp meetings ingested 2026-10-05 but sent in July and August dropped out
under the new filter column. That row still scored 0.0.

**Re-judged 2026-10-21**, on the document set of that day (2,011 documents),
with the code at `6948c6f`. Before the edit the check above still found 18 of
28 and 5 of 5 judged documents out of range. Candidates for both queries were
pooled the same way as the rest of the corpus: raw Postgres FTS over the
range, plus the hybrid top 20 recorded in `ci.json`. Each was read.

- **Daily-brief query** (range unchanged, 2026-09-05 to 2026-10-15). The
  judged set is now the 27 brief notes dated inside the range: the 10 judged
  briefs still in range were kept, the 18 outside it were dropped, and 17 were
  added. No pooled non-brief document is a daily digest. Recall@20 is capped at
  20/27 by construction.
- **Planning query: re-pinned, not re-judged.** Pooling its range (41
  candidates) found no document primarily about its topic, and all five judged
  documents are dated 2026-08-27. Rather than judge off-topic documents
  relevant, it moved to `after: 2026-07-24` / `before: 2026-09-02`. That is
  the 40-day `since_days` window of the first recording (2026-09-02), the one
  its judgments were made against. The window is placed on the column the
  pinned filter actually reads: `after`/`before` bind
  `coalesce(sent_at, ingested_at)` (`build_predicate` in
  `src/brain/search_predicate.py`); only `since_days` reads `ingested_at`.
  All five judged documents have a NULL `sent_at`, so the two columns agree
  for them: 2026-08-27 on both, and 5 of 5 in range under either one
  (measured 2026-10-21). Pooling the new range (raw FTS, plus the
  hybrid top 20 of the re-recorded run) kept the same five judgments. The
  sixth, excluded probe document is in range and stays excluded.

After the edit the check above returns no rows for either query: 27 of 27 and
5 of 5 in range. Before re-recording, an eval of the unedited corpus against
the 2026-10-15 baseline moved no row by more than `1e-4`. So the re-record
moved only these two rows, and every other row's metrics and top 20 are
identical:

| Query | nDCG@5 | MRR | Recall@20 |
|---|---|---|---|
| Daily brief | 0.3601 → 1.0 | 0.5 → 1.0 | 0.2143 → 0.7407 |
| Planning | 0.0 → 1.0 | 0.0 → 1.0 | 0.0 → 1.0 |
| **Mean (31 queries)** | 0.2724 → **0.3253** | 0.3826 → **0.4310** | 0.7089 → **0.7581** |

Both rows now sit at their ceilings, so from here they can only register a
regression. The planning query's five judged documents are near-identical
records, so that row mostly exercises the date filter.

For a query still on `since_days`, re-derive the window before you re-record.
It is a property of the document set, not of the code:

```sql
WITH b AS (SELECT now() - interval '40 days' AS cut)
SELECT max(ingested_at) FILTER (WHERE ingested_at <  cut) AS last_before,
       min(ingested_at) FILTER (WHERE ingested_at >= cut) AS first_after
FROM documents, b;
```

The baseline is stable from `last_before + 40 days` to `first_after + 40 days`.

**3. The retrieval config moved.** Changing the embedder,
`recency_halflife_days`, `vector_sim_floor`, or `snippet_context_tokens` shifts
every metric exactly as a quality regression does, and produces the same exit
`3`. Since 2026-09-02 the command prints the changed keys and their before/after
values on stderr before it exits, so this case now announces itself — but the
exit code alone still cannot distinguish it.

**4. The corpus changed.** Editing `golden_corpus.yaml` — adding a query,
re-judging a query's `expected_doc_ids` — invalidates the baseline for the
queries it touched. `diff_reports` compares the *intersection* of the two query
sets and reports added/removed queries separately, so an added query cannot
dilute the mean; but a re-judged one silently changes what "correct" means.

## Got `exit 1` with "queries are degraded"? Retry

A query is **degraded** when its embed call failed *transiently*, on any
backend: a timeout (usually another process has a large model loaded in
Ollama), a dropped or refused connection, or HTTP 408 / 429 / 5xx (a Voyage
rate limit lands here). The runner scores it `0.0` without having measured
retrieval and marks the row `degraded`.

A *permanent* embed failure is not a degraded row: a model that is not pulled
(HTTP 404), a bad API key, a response the client cannot use. Every query would
fail the same way, so the run stops with exit `1` and a message naming the
error, before any report or baseline is written. Fix the configuration; a retry
will not help. (Under `BRAIN_EMBEDDER=none` nothing is embedded at all: the run
is a real FTS-only measurement, not a degraded one, and `config_signature`
names the backend.)

- **`--record-baseline`** refuses to write a degraded run. It names how many
  queries were degraded (never which: the corpus is personal), writes nothing,
  leaves any existing `<NAME>.json` exactly as it was, and exits `1`. Free the
  embedder and run it again.
- **`--diff`** leaves a degraded query that is in both runs out of the
  per-query and mean deltas and lists it under `degraded_queries`. A degraded
  query that is new since the baseline stays under `added_queries`; it was never
  in the deltas. Either way, a stderr warning gives the count.
- **`--diff --fail-below`** prints the diff, then exits `1` without judging it
  when `degraded_queries` is non-empty, i.e. when a query in both runs is
  degraded. Dropping those rows alone is not enough: if every embed failed,
  nothing would be compared, every delta would be `0`, and the gate would pass.
  A degraded *added* query does not block the gate, because it is outside the
  comparison anyway; the gate runs as usual and the warning still prints.
- A plain run still exits `0`, with a stderr warning that gives the count.

A baseline recorded **before** this guard existed (2026-10-15) could already
contain a degraded row. Such a row reads as a genuine miss: all three metrics
`0.0`, `actual_doc_ids` empty, on a query that ranks fine in an ad-hoc run. If
you find one, re-record.

## Known retrieval issues

- **Vector-leg candidate pool — FIXED 2026-10-13.** The vector leg was served by
  the HNSW index, so it returned at most `hnsw.ef_search` (default 40) rows
  rather than `CANDIDATE_LIMIT`. Filters applied after the index scan, so
  filtered queries got fewer still. Measured 2026-10-13 on the live corpus
  (1,999 docs / 21,281 chunks): recall@50 against an exact scan averaged 0.76
  over the golden queries. That figure depends on the HNSW graph's quality, so
  it shifts with ingests and reindexes. On one query a few long new documents
  filled the whole pool, shrinking its results from 17 documents to 6. It is now
  an iterative HNSW scan with `ef_search = 1000`, scoped per query. Its recall@50
  against an exact scan, measured for the shipped shape, is 0.9987 mean / 0.98
  min over the golden queries and 0.994 / 0.96 over low-similarity queries,
  returning the exact floor-passing set wherever the floor is binding (spec §6.5). The code is in `brain/search_vector_leg.py`; the design and
  measurements, including the exact-scan version it replaced, are in
  `docs/specs/2026-10-13-search-vector-leg-design.md`. Recall still depends
  on the graph, so spec §5 has a read-only recall check with a 0.99 trigger.
  `ci.json` was re-recorded for the ranking change (first for the exact scan,
  then for the iterative scan, whose metrics are identical): the means went
  from 0.2589 / 0.3697 / 0.5852 to 0.2779 / 0.3826 / 0.7112, on the same
  document set and the same window as in (2). The means re-recorded
  2026-10-15 when the recency queries were pinned in (2), unchanged from the
  2026-10-14 re-record for the synced daily briefs, were
  **0.2724 / 0.3826 / 0.7089**. The 2026-10-21 re-judge of the two `recency`
  rows in (2) moved them to **0.3253 / 0.4310 / 0.7581**, with no other row
  changed. The same
  HNSW shape remains in `related.py` and `connect.py`; those are unmeasured
  follow-ups.

## Who re-records, and when

Whoever makes the change that invalidates it, **in the same PR** — the same rule
as any other committed fixture. Re-record after any of:

- an ingest or deletion that changes the document set
- a re-embed, or an embedder swap
- a change to any key in the baseline's `config_signature`
- a change to the ranking code (SQL or fusion) — e.g. `search.py`,
  `search_vector_leg.py`, `rank_fusion.py`
- an edit to `tests/eval/golden_corpus.yaml`
- drift past the recency window in (2) above (only for queries still on
  `since_days`; a pinned `after`/`before` query does not drift)

```bash
brain eval --record-baseline ci      # needs a populated brain + Ollama
git diff tests/eval/baselines/ci.json
```

Commit the new JSON alongside the change that justifies the new numbers, and say
in the commit message *which* of the triggers above applied.
If the record exits `1` saying queries are degraded, nothing was written; see
"Got `exit 1`" above.

## What CI does with this

Nothing, by design. `.github/workflows/eval.yml` gates the regression step on
both `ci.json` **and** `tests/eval/golden_corpus.yaml`; the corpus is gitignored,
so on a hosted runner this is always the skip branch. Retrieval-quality gating is
a **local pre-merge check**. CI covers harness health only. See the header
comment in `eval.yml` for the four preconditions a hosted runner cannot supply,
and `CLAUDE.md` → "Eval gate (CI)".

## The corpus that backs `ci.json`

`tests/eval/golden_corpus.yaml` is gitignored: each developer authors their own
against their own documents, because relevance judgments are document ids and
those are personal. If you are writing one, judge honestly — pool candidates from
more than just the ranker under test, read the documents, and let hard queries
score badly. A corpus curated until the current ranker looks good is a baseline
that gates nothing.
