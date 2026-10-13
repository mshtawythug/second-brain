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
*The 2026-10-13 re-record was this case, not recency drift (2) alone.* Ingests
added 608 documents to the document set (Krisp on 2026-10-05, a Confluence
import on 2026-10-08) and moved 12 non-recency rows as well; the means fell from
0.4834 / 0.5835 / 0.7267 to **0.2589 / 0.3697 / 0.5852** (nDCG@5 / MRR /
Recall@20). The golden judgments predate ~30% of the document set, so the
absolute scores understate quality until `golden_corpus.yaml` is re-judged
against the new documents.

**2. Two `recency` queries are clock-dependent.** They filter on
`documents.ingested_at` via `since_days: 40`, so the matched set is stable only
while `today - 40 days` stays inside one gap in this brain's ingest history.
The baseline was re-recorded on **2026-10-13**, when that boundary sat inside a
21-day gap (last ingest 2026-08-31, next 2026-09-21), which makes it stable
from roughly **2026-10-10 to 2026-10-31**. Outside that range one or both of
those queries drift on their own and the baseline needs re-recording. Nothing
about search changed. *(The first recording, 2026-09-02, sat in a different
gap and drifted on 2026-09-20 exactly as this paragraph predicted — one query,
`-0.15` nDCG@5, every other row unchanged to four decimals.)*

Re-derive the window before you re-record; it is a property of the document
set, not of the code:

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

## Known retrieval issues

- **Vector-leg candidate pool — FIXED 2026-10-13.** The vector leg was served by
  the HNSW index, so it returned at most `hnsw.ef_search` (default 40) rows
  rather than `CANDIDATE_LIMIT`. Filters applied after the index scan, so
  filtered queries got fewer still. Measured 2026-10-13 on the live corpus
  (1,999 docs / 21,281 chunks): recall@50 against an exact scan averaged 0.76
  over the golden queries. That figure depends on the HNSW graph's quality, so
  it shifts with ingests and reindexes. On one query a few long new documents
  filled the whole pool, shrinking its results from 17 documents to 6. It is now
  an exact scan (`brain/search_vector_leg.py`; design and measurements in
  `docs/specs/2026-10-13-search-vector-leg-design.md`), which takes the HNSW
  graph out of this path entirely. `ci.json` was re-recorded for that ranking
  change: the means went from 0.2589 / 0.3697 / 0.5852 to **0.2779 / 0.3826 /
  0.7112**, on the same document set and the same window as in (2). The same
  HNSW shape remains in `related.py` and `connect.py`; those are unmeasured
  follow-ups.

## Who re-records, and when

Whoever makes the change that invalidates it, **in the same PR** — the same rule
as any other committed fixture. Re-record after any of:

- an ingest or deletion that changes the document set
- a re-embed, or an embedder swap
- a change to any key in the baseline's `config_signature`
- an edit to `tests/eval/golden_corpus.yaml`
- drift past the recency window in (2) above

```bash
brain eval --record-baseline ci      # needs a populated brain + Ollama
git diff tests/eval/baselines/ci.json
```

Commit the new JSON alongside the change that justifies the new numbers, and say
in the commit message *which* of the triggers above applied.

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
