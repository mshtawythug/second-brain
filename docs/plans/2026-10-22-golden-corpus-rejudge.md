# Re-judge the 29 non-recency golden rows and re-record `ci.json` — plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `eval/rejudge-golden-corpus-2026-10-22`, base `second-brain/master` @ `bb004fe` (PR #47).

## Problem

The retrieval golden corpus (`tests/eval/golden_corpus.yaml`, local and
gitignored) has 31 rows. Their judgments were made before the 2026-10-05 Krisp
ingest and the 2026-10-08 Confluence import, which together added 608
documents, about 30% of today's document set. A document that arrived after a
row was judged can be relevant and still score as a miss, so the committed
means understate retrieval quality. PR #41 (`eec002f`) re-judged the two pinned
`recency` rows on 2026-10-21. The other 29 have not been re-judged. This is item
4 of "Remains the user's" in `docs/handoff/2026-10-13-merge-all-handoff.md`.

Means in `tests/eval/baselines/ci.json` at `bb004fe` (nDCG@5 / MRR / Recall@20):
**0.3253 / 0.4310 / 0.7581**. Re-derive them from the file.

## What the corpus format allows

Read from `src/brain/eval/corpus.py` and `src/brain/eval/metrics.py`, not
assumed:

- Judgments are **binary**. A row's `expected_doc_ids` lists the relevant
  documents as 8-hex id prefixes (or full UUIDs). There is no grade field, and
  `ndcg_at_k` uses binary gain. The re-judge keeps this format. It adds no grade
  scale, because `load_corpus` refuses unknown fields and the metrics could not
  read one.
- `notes` is free text and is not graded. It stays local with the file.
- `_normalize_ids` silently drops a prefix that matches no document, and
  raises on a prefix that matches two or more. Both are checked for every row
  before and after the edit.

## Rulings

| # | Question | Ruling |
|---|---|---|
| **J1** | Which rows? | **The 29 non-recency rows.** The two pinned `recency` rows were re-judged on 2026-10-21 and are left alone unless a check below finds one broken (a judged id that no longer resolves, or one that falls outside the range). |
| **J2** | What is pooled for each row? | **The union of five channels**, each run against the prod DB read-only: (1) the hybrid ranking as `brain eval` calls it (`hybrid_search` with the config's halflife, snippet context and vector floor, and the row's own filters), taken to depth 30, deeper than the 20 that are scored; (2) an any-term FTS leg, `ts_rank` over `documents.tsv` with the query's lexemes OR'd, top 30, which finds lexical matches that the hybrid leg's AND-ed `plainto_tsquery` misses; (3) the same leg with the lexemes AND-ed, top 15; (4) an exact vector leg, the query embedded by the configured embedder and each document scored by its best chunk's cosine with a sequential scan (no HNSW), top 30; (5) the row's existing judgments. A row with a `source_filter` is pooled inside that source only, since nothing outside it can be retrieved. |
| **J3** | And rows whose existing notes say the set was *enumerated* (every document of a kind)? | **Re-enumerated as well.** Those rows' judged sets were built by listing every document of the kind, not from a ranking. They get a sixth channel: a title and content-type listing for the kind, read in full, so a new document of that kind is found even when no ranking channel surfaces it. |
| **J4** | What makes a document relevant? | **The rule each row already states, applied unchanged.** The corpus header says a candidate is kept only when the document is *primarily* about the query topic, and each row's `notes` record the exclusions it makes (passing mentions, title-only matches with stub or synthetic bodies, invitations versus reminders, and so on). A new candidate is judged by the same rule as the old ones. Each candidate is read (title, content type, source, date, auto-summary, and the body where those leave it open). |
| **J5** | Are existing judgments re-opened? | **Only on evidence.** An existing judgment is kept unless reading it again shows it fails the row's own rule. Any removal is counted and its reason recorded in the local `notes`. A judged id that no longer resolves is removed and counted separately. |
| **J6** | Duplicates (the same content ingested twice, or a PDF export beside its markdown)? | **Judged on their own.** Existing rows already count a PDF export and its markdown separately, so a new duplicate of a relevant document is relevant too. |
| **J7** | Confidential or draft documents? | **Judged like any other.** `brain eval` does not filter on sensitivity, and the prod DB holds no drafts today (measured). Excluding a retrievable relevant document would penalize the ranker for returning it. |
| **J8** | Re-record order | **Before any edit**, an eval of the unedited corpus against the `bb004fe` baseline, read-only, to separate drift from the re-judge. After the edit, `brain eval --record-baseline ci`. If it refuses because a query was degraded, retry once the embedder is free; never `brew services restart` Ollama. |
| **J9** | What may be written down? | **Counts, row indices, metric values and opaque id prefixes inside the local corpus file only.** No query text, title, body, summary, or person name goes into this plan, the README, CLAUDE.md, the handoff, a commit message or the PR body. `ci.json` carries each row's `query` string already. That is the harness's own format and the corpus header states those strings are synthetic. The re-judge does not change any `query` string, so the `ci.json` diff touches only ids, metrics and `generated_at`. |

## Safety

- Every connection to the prod DB (port 55432) sets
  `PGOPTIONS='-c default_transaction_read_only=on'`, and the pooling helper
  asserts the setting before it runs a statement. A probe write was refused
  (`ReadOnlySqlTransaction`).
- `brain eval` was read before it was run: `run_eval` calls `hybrid_search`
  and `_normalize_ids` only, and neither issues a write. `record_search_query`
  (the search-failure log) is called by `brain search`, `brain recall` and the
  MCP server, not by `eval`. `brain eval` runs under the same `PGOPTIONS`, so a
  write would fail rather than land. It writes one file, `tests/eval/baselines/ci.json`.
- `brain search` is not run, because it logs the query.
- No migration, `brain init`, ingest, re-embed or enrich is run. `~/brain-vault`
  is not touched.
- The corpus file is backed up to `~/.brain/backups/golden_corpus-<UTC>.yaml`
  (checked with `cmp`) before it is copied into the worktree, and again before
  the re-judged copy is written back to the main checkout.

## Risks

- **The document set is live.** It grew by 18 documents between the PR #41
  re-record (2,011) and the start of this work (2,029). The re-record is a
  snapshot of the day; the drift eval (J8) shows how much of the movement is
  not the re-judge.
- **Pool bias.** Judging only what rankers return favors those rankers. Two
  channels that know nothing about RRF (J2 (2)–(4)) and the enumeration
  channel (J3) are there to limit it. Hard rows are allowed to score badly.
- **Larger judged sets lower Recall@20 and can raise or lower nDCG@5.** The
  new means are not comparable to the old ones as a quality trend. They
  measure the same ranker against more complete judgments.
- **Single assessor.** One reader judged every row. The rule in J4 and the
  per-row exclusions in `notes` are what keep it consistent with the original
  judgments.

## Deliverables

1. The re-judged local corpus, written back to the main checkout's
   `tests/eval/golden_corpus.yaml` after a second backup.
2. `tests/eval/baselines/ci.json`, re-recorded. Trigger: an edit to
   `golden_corpus.yaml` (and, incidentally, the document set's growth).
3. `tests/eval/baselines/README.md` and `CLAUDE.md` ("The committed `ci.json`
   baseline"): the re-judge, the counts, and the new means.
4. `docs/handoff/2026-10-13-merge-all-handoff.md`: "Remains the user's" item 4
   and the item-2 status row marked done by this PR.

## Results

**Which corpus the baseline belongs to.** `ci.json` was recorded on
2026-10-22 against **2,068 documents**. The newest `ingested_at` was
2026-10-22 18:03:28Z, and the record ran at about 18:20Z. A Krisp ingest by
another of the user's sessions ran during this work, taking the count from
2,029 to 2,068. A first record taken while it ran was discarded, because rows
moved between runs a few minutes apart. The final record waited until two
conditions held: no `brain ingest-stdin` process was running, and the count
and the newest `ingested_at` / `updated_at` had not changed for 15 minutes.
The ingest's 55 new transcripts were judged like the rest.

**Per-row changes** (row = 0-based index in the corpus file; counts only):

| Row | Category | Judged before → after | Added |
|---|---|---|---|
| 0 | semantic | 11 → 15 | +4 |
| 2 | semantic | 2 → 4 | +2 |
| 3 | semantic | 9 → 11 | +2 |
| 6 | semantic | 6 → 7 | +1 |
| 7 | semantic | 3 → 4 | +1 |
| 13 | interview | 4 → 7 | +3 |
| 14 | meeting | 15 → 17 | +2 |
| 15 | meeting | 15 → 53 | +38 |
| 17 | meeting | 1 → 4 | +3 |
| 18 | meeting | 1 → 2 | +1 |
| 20 | email | 4 → 5 | +1 |
| 23 | source-specific | 2 → 3 | +1 |
| 24 | source-specific | 6 → 10 | +4 |
| 29 | people | 4 → 6 | +2 |
| 30 | people | 22 → 24 | +2 |

- Rows 1, 4, 5, 8, 9, 10, 11, 12, 16, 19, 21, 22, 25 and 26 are unchanged.
  Rows 27 and 28 (`recency`) are untouched, and all of their judgments are
  still in range.
- 15 rows changed, +67 judgments in all. No grade was changed, because the
  format is binary. No judgment was removed, and every judged prefix resolves
  to exactly one document.
- **When the 67 additions were ingested:** 20 before 2026-10-05 (they existed
  but no channel of the original pool had reached them), 35 from 2026-10-05
  to 2026-10-21, and 12 on 2026-10-22.
- The local corpus file records the reason for each change in that row's
  `notes`, under "RE-JUDGED 2026-10-22". Those notes stay local.

**Eval evidence** (all runs under `PGOPTIONS='-c default_transaction_read_only=on'`):

| Run | Result |
|---|---|
| Unedited corpus vs the `bb004fe` baseline, on the final document set | 8 rows moved; means -0.0210 / -0.0066 / -0.0000. That is document-set growth, not the re-judge. |
| `brain eval --record-baseline ci` (re-judged corpus) | exit 0. No degraded query; the command refuses to write one. |
| Re-judged corpus `--baseline ci --diff --fail-below`, right after the record | exit 0; 0 rows moved by more than `1e-4`; 0 degraded |

| Means (nDCG@5 / MRR / Recall@20) | |
|---|---|
| `bb004fe` | 0.3253 / 0.4310 / 0.7581 |
| 2026-10-22 re-record | **0.3191 / 0.4645 / 0.7176** |

`config_signature` is unchanged (arctic, halflife 180, snippet context 200,
floor 0.25). Every row keeps its `query` and `category`, so the `ci.json`
diff touches only document ids, metrics, the per-category and overall means,
and `generated_at`.

**Backups.** The pre-edit corpus is
`~/.brain/backups/golden_corpus-20261022T173950Z.yaml`. The main checkout's
copy was backed up again, to `~/.brain/backups/golden_corpus-pre-writeback-20261022T182833Z.yaml`,
before the re-judged file was written back to it.
Both copies were checked with `cmp`.
