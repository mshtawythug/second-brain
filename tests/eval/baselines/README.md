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

**2. Two `recency` queries are clock-dependent.** They filter on
`documents.ingested_at` via `since_days: 40`. The window was chosen so its
boundary falls inside a 25-day gap in this brain's ingest history, which makes
the matched set stable only from roughly **2026-08-22 to 2026-09-16**. Outside
that range those two queries drift on their own and the baseline needs
re-recording. Nothing about search changed.

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
