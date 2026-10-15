# Eval recency determinism — design

**Status:** implemented on `fix/eval-recency-drift`.
**Plan:** `docs/plans/2026-10-15-eval-recency-determinism.md`.

## 1. Problem

`brain eval --baseline ci --diff --fail-below` exits `3` when any mean metric
drops by more than `1e-4` against the committed baseline `ci.json`. Two
`recency` golden queries filter with `since_days: 40`, which compiles to

```sql
d.ingested_at >= NOW() - make_interval(days => 40)
```

(`brain.search_predicate.build_predicate`). The matched set is therefore a
function of the wall clock, on two edges:

- **Trailing edge.** Old documents age out as `NOW()` advances. The README's
  "stable window" SQL tracks only this edge.
- **Leading edge.** Every newly ingested document lands inside the window at
  once. The vault watcher syncs a daily-brief note on most days, and every
  one is an unjudged competitor for the query "daily brief digest of recent
  captures and action items" (28 of 28 judged documents are briefs). That
  edge has **no** stable window.

`ci.json` was re-recorded on 2026-10-13 and again in PR #25 (2026-10-14) for
this drift alone. PR #25 left five options for the lead and implemented none.
The result is a gate that goes red most days with no change to search, which
teaches people to ignore it.

## 2. Options considered

| # | Option | Verdict |
|---|---|---|
| 1 | Accept the drift and re-record whenever it trips | Rejected: this is the status quo |
| 2 | **Pin the recency queries to an absolute date range** | **Chosen** |
| 3 | Report recency rows but exclude them from the `--fail-below` mean | Rejected as the primary fix (see §2.1) |
| 4 | Re-judge the daily-brief query periodically | Rejected: it still drifts between re-judgings |
| 5 | Swap the query for one whose target does not grow daily | Out of scope: the corpus is personal and gitignored |

### 2.1 Why pin rather than exclude

- **It removes the cause.** With a closed range, the query's candidate set
  depends only on the document set. Excluding the rows leaves them drifting
  and stops measuring them.
- **The rows stay gated.** Under option 3 the `recency` category would never
  gate. Under option 2 a real regression on those rows still trips exit 3.
- **No search-layer change.** `hybrid_search` already accepts `after` /
  `before` (Q1-C filters, inclusive lower / exclusive upper bound, on
  `coalesce(sent_at, ingested_at)`, stamped UTC by `_ensure_utc`). The fix is
  confined to `brain.eval.corpus` and `brain.eval.runner`. `search.py` and
  `search_predicate.py` are untouched. `search.py` is over the 800-line
  ceiling, so this matters.
- **No baseline-format change.** `EvalResult` does not store filters, so
  `ci.json`'s schema and `load_baseline` are unchanged, and every old baseline
  still loads (§5).
- **The `--fail-below` contract is unchanged.** Exit codes 1, 2 and 3 and the
  `1e-4` rounded threshold stay as they are. `diff_reports` and
  `mean_metrics_regressed` are not touched.

**What pinning gives up.** A pinned query no longer exercises "recent relative
to today". The eval never measured that, though. `since_days` is a SQL
predicate, unit-tested in the search suite. The eval measures *ranking within
the filtered set*, and a fixed set measures that better.

### 2.2 The recency boost does not reintroduce the clock

`ci.json`'s `config_signature` has `recency_halflife_days: 180.0`, and the
boost reads `datetime.now()`. It does not make a pinned query clock-dependent:

```
score_i = rrf_i · 0.5^((now − t_i) / h)  =  0.5^(now/h) · [ rrf_i · 0.5^(−t_i/h) ]
```

Moving `now` multiplies every candidate's score by the same positive constant,
so the **order** is unchanged. The order is all that nDCG, MRR and Recall
read. Two conditions apply:

- **The clamp.** `age = max(0, now − t)` breaks the identity only for
  documents dated in the future. Inside a closed past range, none are. That
  is why `before` must already be in the past when the baseline is recorded
  (§3.3). A `before` later than `now` leaves room for a document dated after
  `now`.
- **Candidate pools.** The FTS and vector legs are `LIMIT`-ed on leg rank,
  before the boost is applied, so the pools do not depend on `now` either.

## 3. Design

### 3.1 Corpus schema (`brain.eval.corpus`)

Each corpus entry gains two optional fields, named after the `hybrid_search`
keywords and the CLI's `--after` / `--before`:

```yaml
- query: "daily brief digest of recent captures and action items"
  category: recency
  after: 2026-09-05     # inclusive, UTC midnight
  before: 2026-10-15    # exclusive, UTC midnight
  expected_doc_ids: [...]
```

`EvalQuery` gains `after: date | None = None` and `before: date | None = None`.
They default to `None`, so every existing construction site and corpus file
behaves as it does today.

`load_corpus` validates the new fields as follows. Each failure raises
`EvalCorpusError`, which the CLI already maps to exit `1`.

| Rule | Why |
|---|---|
| Each value is a calendar date: an unquoted YAML date, or a `"YYYY-MM-DD"` string. A `datetime`, `int` or other type is rejected | A time of day or a timezone is a second source of drift. Day granularity matches the CLI flags |
| `after` requires `before` | An open upper bound is the leading-edge drift this spec fixes, so it is rejected rather than allowed to look pinned |
| `after < before` when both are set | Half-open interval: `after == before` matches nothing, which would score 0 silently |
| `since_days` cannot be combined with `after` or `before` | The two filter different columns (`ingested_at` and `coalesce(sent_at, ingested_at)`), and mixing them keeps the clock dependency |
| `before` alone is allowed | It is deterministic: everything before a fixed date |

`since_days` stays valid, so existing corpora still load. Its clock
dependency is documented in the README and in CLAUDE.md, not enforced.

### 3.2 Runner (`brain.eval.runner`)

`run_eval` passes `after` / `before` to `hybrid_search` as UTC-aware midnight
datetimes, built by one helper, `_utc_midnight(day: date | None)`. Building them
UTC-aware in the eval layer means the bound does not depend on the session
time zone, and on `_ensure_utc` being called later.

### 3.3 What the user applies (the corpus is gitignored)

The real `tests/eval/golden_corpus.yaml` is not in the repo and is **not**
edited by this change. For each of the two `recency` entries, replace

```yaml
    since_days: 40
```

with

```yaml
    after: <YYYY-MM-DD>
    before: <YYYY-MM-DD>
```

Then re-record: `brain eval --record-baseline ci`. That needs the prod read
path and Ollama. The re-record is **required**, because the filter column
changes from `ingested_at` to `coalesce(sent_at, ingested_at)` and the window
becomes fixed. Choosing the dates:

- `before` = a UTC date **no later than today's UTC date at re-record time**,
  so its midnight is already in the past. The bound must be closed *before*
  the recording, not after it. A `before` of tomorrow would leave the window
  open until the next UTC midnight: a brief ingested after the recording but
  before that midnight lands inside the pinned range, and the drift this spec
  removes comes back permanently, until someone re-records. It would also break
  the "closed past range" premise of the clamp argument in §2.2.
- `after` = `before − 40 days`. The half-open `[after, before)` is then the
  same 40 days the current `since_days: 40` judgments were made against.

A brief ingested on the re-record day itself, before the recording, is
excluded when `before` is today. That is the intended trade-off: the range
covers only days that are complete.

The README has a read-only SQL check that every judged document of a pinned
query falls inside its range.

## 4. Tests

Unit tests, in `tests/test_eval_corpus.py`:

1. `after` / `before` parse from an unquoted YAML date and from a quoted string.
2. They default to `None`.
3. `after` without `before` is rejected.
4. `after >= before` is rejected, including the equal case.
5. `since_days` combined with `after`, or with `before`, is rejected.
6. A `datetime` value, a non-ISO string and an `int` are each rejected.
7. `before` alone is accepted.

Integration tests, in `tests/test_eval_runner.py` against the real test DB:

8. **Regression test, reproducing the bug.** Seed a judged document with an
   `ingested_at` 10 days in the past. Run a `since_days: 40` query, then ingest
   a new competing document now and run it again. The ranked list changes.
   This is the drift, reproduced.
9. **The fix.** The same scenario with a pinned `after` / `before` range that
   contains the judged document. The ranked list is identical before and after
   the new ingest.
10. **Bounds are passed through, half-open.** A document dated exactly on
    `before` is excluded. A document dated exactly on `after` is included.
11. `_utc_midnight` returns a UTC-aware midnight for a date, and `None` for
    `None`.

Regression tests (CLAUDE.md rule 12), added after review, in
`tests/test_eval_corpus.py`:

12. An **unquoted** impossible date (`before: 2026-02-30`) raises
    `EvalCorpusError`. PyYAML's timestamp constructor raises a bare
    `ValueError` inside `safe_load`, before `_parse_date_bound` runs, so this
    escaped as a traceback.
13. A `since_days` that is not a positive `int` raises `EvalCorpusError`. The
    cases: `true`, `40.7`, `'40'`, `forty`, `[40]`, `0` and `-5`. Before the
    fix, `int()` either raised a bare `ValueError`/`TypeError` or silently
    coerced (`true` to 1, `40.7` to 40, `'40'` to 40). `0` is refused because
    `build_predicate` tests `if since_days:`, so 0 silently means no window.

## 5. Compatibility

- **`ci.json`:** no schema change. `load_baseline` is untouched, and the
  committed file loads unchanged (covered by existing tests).
- **Corpora without the new fields** parse to identical `EvalQuery` values.
- **`--fail-below`:** exit codes, threshold and intersection semantics are
  unchanged. `diff_reports` matches by query string, so a query that changes
  from `since_days` to a pinned range keeps its row.

## 6. Mutation catalog

Each new guard must go red under its mutation. The plan's §Mutation proofs has the exact needles, their counts, and the `>`/`E` needles:

| ID | Mutation | Expected red |
|---|---|---|
| M1 | Drop the `after`-requires-`before` check | test 3 |
| M2 | Change `after >= before:` to `after > before:` (the colon makes the needle unique, since docstrings quote the bare expression) | test 4 (the equal case) |
| M3 | Drop the `since_days` exclusivity check | test 5 |
| M4 | Accept `datetime` (remove the `isinstance(v, datetime)` rejection) | test 6 |
| M5 | Runner does not pass `after` / `before` to `hybrid_search` | tests 9 and 10 |
| M6 | Runner swaps `after` and `before` | test 10 |
| M7 | `_utc_midnight` returns a naive datetime | test 11 |
| M8 | `safe_load` catches only `yaml.YAMLError` | test 12 |
| M9 | `bool` accepted as an `int` | test 13 (`true`) |
| M10 | the `isinstance(value, int)` check dropped | test 13 (`40.7`) |
| M11 | `value < 1` becomes `value < 0` | test 13 (`0`) |
