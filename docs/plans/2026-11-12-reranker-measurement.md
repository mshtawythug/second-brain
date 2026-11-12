# Reranker measurement (RM-14c): built, measured, not adopted (latency)

- **Ticket:** RM-14c, in `docs/specs/2026-10-23-roadmap-completion-design.md`. The plan is
  `docs/plans/2026-11-12-rm-14c-reranker-measurement.md`.
- **Measured:** 2026-11-12, on the owner's machine (Apple Silicon, macOS 26), in one session.
- **Decision:** **not adopted.** The quality gain is large and clears the bar by a wide margin. The
  added latency fails the 1.0 s p95 limit by a factor of about 2.6. `BRAIN_RERANKER` stays
  `none`, both in code and on the owner's machine.
- **Shipped anyway:** a fix to the launchd template. With llama.cpp's default physical batch,
  `llama-server` refused any reranker pair longer than 512 tokens, which made more than half of
  the golden queries skip the reranker (§3).

Every number below is an aggregate. The golden corpus is private, so no query text, document id
or per-query row is recorded here or anywhere else in this PR.

## 1. Setup

| | |
|---|---|
| Database | `rm14c_eval_20261112`, a scratch database on the 5434 test server. It was restored with `pg_restore` (PG 16, inside the test container) from the DB dump in `brain-backup-20261112-000417-pre-031`. The dump's sha256 matched the manifest. The vault was never extracted. Prod (55432) was never contacted. |
| Document set | `brain status`: **2,114 documents / 22,322 chunks / 1,243 sources**, equal to the backup manifest's counts. 0 chunks had a NULL embedding. Migration head was `030_near_duplicates.sql`. The set was re-checked after a container restart mid-session, with the same counts. The database is frozen, so it did not move between runs. |
| Retrieval config | Pinned to `ci.json`'s `config_signature`: `embedder` arctic, `recency_halflife_days` 180.0, `snippet_context_tokens` 200, `vector_sim_floor` 0.25. `BRAIN_DOC_LEG` off and graph off. |
| Reranker | `llamacpp`: `llama-server` **0.7.0** (build 11669, commit 620d2225c, Homebrew), serving `bge-reranker-v2-m3-Q8_0.gguf` (the guide's file) on `127.0.0.1:8091`, because 8081 is LanguageTool's. Stage knobs at their defaults: depth 20, `max_chars` 1,500, timeout 5.0 s. |
| Golden corpus | 31 queries in 7 categories (email 4, interview 5, meeting 5, people 2, recency 2, semantic 9, source-specific 4). |
| Machine state | One model workload at a time, under the shared model lock, with 10–12 GiB of free disk at each start. Swap was heavily used throughout the session (about 16–17 GB), which probably inflated the latencies (§6). |

## 2. Runs

Each configuration was run twice. Both reranked configurations produced **identical rankings** in
run 1 and run 2.

| Run | Server flags | Rows reranked | Rows skipped | Degraded | nDCG@5 | MRR | Recall@20 |
|---|---|---|---|---|---|---|---|
| **off** (`none-2`) | — | — | — | 0 | 0.3096 | 0.4449 | 0.7226 |
| **(a) shipped flags**, runs 1 and 2 | template as shipped (no batch flags) | 14 | **17** | 0 | 0.4663 | 0.6032 | 0.7226 |
| **(b) batch 2048**, runs 1 and 2 | `--batch-size 2048 --ubatch-size 2048` | **31** | 0 | 0 | **0.6721** | **0.8199** | 0.7226 |

The first reranker-off run (`none-1`) is excluded. A cold-start Ollama under heavy swap failed
the query embed for 10 of its 31 queries (transient `EmbedError`s), and `brain eval
--record-baseline` correctly refused to record it. That is why the planned on-machine
`--fail-below` run had no baseline to read (§4). `none-2` ran clean straight afterwards.

Recall@20 cannot move: the stage reorders only the first `depth` = 20 rows, which is the eval's
`limit_per_query`, so the set of 20 is unchanged.

### Per category, (b) against off (means of nDCG@5 / MRR / Recall@20)

| Category | n | off | (b) batch 2048 | Δ nDCG@5 | Δ MRR |
|---|---|---|---|---|---|
| email | 4 | 0.0000 / 0.0806 / 0.7500 | 0.6377 / 0.6250 / 0.7500 | +0.6377 | +0.5444 |
| interview | 5 | 0.3892 / 0.4440 / 0.9714 | 0.8674 / 0.8667 / 0.9714 | +0.4781 | +0.4227 |
| meeting | 5 | 0.0292 / 0.0936 / 0.3537 | 0.4848 / 0.7000 / 0.3537 | +0.4556 | +0.6064 |
| people | 2 | 0.2544 / 0.6667 / 0.3542 | 0.4122 / 0.6667 / 0.3542 | +0.1578 | +0.0000 |
| recency | 2 | 1.0000 / 1.0000 / 0.8704 | 1.0000 / 1.0000 / 0.8704 | +0.0000 | +0.0000 |
| semantic | 9 | 0.4827 / 0.6735 / 0.8436 | 0.6680 / 0.8611 / 0.8436 | +0.1853 | +0.1877 |
| source-specific | 4 | 0.1630 / 0.3469 / 0.6833 | 0.6715 / 1.0000 / 0.6833 | +0.5085 | +0.6531 |

Per row: MRR rose on 19 rows, fell on 1 and was unchanged on 11.

### Per category, (a) shipped flags (both runs identical)

| Category | n | nDCG@5 | MRR | Recall@20 |
|---|---|---|---|---|
| email | 4 | 0.4077 | 0.4167 | 0.7500 |
| interview | 5 | 0.6596 | 0.7154 | 0.9714 |
| meeting | 5 | 0.2176 | 0.3600 | 0.3537 |
| people | 2 | 0.4122 | 0.6667 | 0.3542 |
| recency | 2 | 1.0000 | 1.0000 | 0.8704 |
| semantic | 9 | 0.4832 | 0.6735 | 0.8436 |
| source-specific | 4 | 0.3163 | 0.5656 | 0.6833 |

### Rerank latency (`EvalResult.stage_ms["rerank"]`, nearest-rank percentiles, applied rows only)

| Run | n | p50 | p95 | max |
|---|---|---|---|---|
| (a) shipped flags, run 1 | 14 | 2,224 ms | 2,647 ms | 2,647 ms |
| (a) shipped flags, run 2 | 14 | 2,131 ms | 2,422 ms | 2,422 ms |
| (b) batch 2048, run 1 | 31 | 2,080 ms | 2,585 ms | 2,929 ms |
| (b) batch 2048, run 2 | 31 | 2,255 ms | 2,817 ms | 2,909 ms |

`llama-server` resident memory (`ps -o rss`, sampled at the start and end of each configuration):
735–793 MB with the shipped flags and 742–790 MB with batch 2048. The larger physical batch costs
no measurable memory.

## 3. The shipped template skipped most queries

In `llama-server`, `--ubatch-size` (the physical batch) defaults to **512 tokens**. A reranker is a
non-causal model, so each query+passage pair has to fit in one physical batch. A synthetic probe,
with 20 passages of 1,500 characters and no corpus text, showed:

| Text | Tokens per passage | Reply |
|---|---|---|
| English-like prose | 305 | HTTP 200 |
| lorem | 278 | HTTP 200 |
| token-dense: ids, versions, symbols | 827 | **HTTP 500**: `input (840 tokens) is too large to process. increase the physical batch size (current batch size: 512)` |

One oversized pair fails the whole request. The backend raises `RerankError`, the stage returns
hybrid order, and the row is marked `stages_skipped`. Against the real corpus this happened on
**17 of 31** golden queries. So the RM-14b job, as shipped, would have left a user who turned the
reranker on with hybrid order on most searches, plus a one-line stderr notice.

**Fix (this PR):** `com.brain.reranker.plist.j2` now passes `--batch-size 2048 --ubatch-size
2048`. A passage is capped at `BRAIN_RERANK_MAX_CHARS` (default 1,500 characters, so at most
1,500 tokens), and 2048 holds that plus the query. The model's context per slot is 8,192.
`tests/test_bin_launchd_reranker.py` pins both values and the invariant that the physical batch
is at least `DEFAULT_MAX_CHARS` plus 256 tokens of headroom, and at most the logical batch. The
proof:

- **RED:** the two tests failed against the old template.
- **GREEN:** the four launchd test files pass, 88 tests.
- **Mutation:** in a copy of the tree, physical batch 2048→512 fails both tests, and logical
  batch 2048→1024 fails both tests.

The batch-2048 measurement (b) is therefore the configuration the fixed template ships.

## 4. The adoption rule, evaluated on (b) paired with off on the same frozen database

| # | Condition | Measured | Verdict |
|---|---|---|---|
| (a) | No mean regresses by more than `1e-4` | Δ nDCG@5 +0.3625, Δ MRR +0.3750, Δ Recall@20 +0.0000 | **PASS** |
| (b) | nDCG@5 or MRR improves by at least 0.02 absolute | +0.3625 / +0.3750, about 18× the bar | **PASS** |
| (c) | p95 added latency at most 1.0 s | 2,585 ms (run 1) and 2,817 ms (run 2) | **FAIL** |

The gate's own arithmetic confirms (a). The live `--baseline rm14c-none --diff --fail-below` runs
exited 1 with "baseline file not found", because `none-1` was refused (§2). The same check was
re-run offline, with no model, through the repo's own functions: `save_baseline` of the clean
`none-2` report, then `load_baseline`, `diff_reports` and `mean_metrics_regressed`.

| Comparison | Shared rows | Excluded (skipped or degraded) | Δ nDCG@5 / MRR / R@20 | Gate exit |
|---|---|---|---|---|
| (b) run 1 vs off | 31 | 0 | +0.3625 / +0.3750 / +0.0000 | 0 |
| (b) run 2 vs off | 31 | 0 | +0.3625 / +0.3750 / +0.0000 | 0 |
| (a) run 1 vs off | 14 | 17 | +0.3470 / +0.3506 / +0.0000 | 0 |

The changed signature keys named in each case were `reranker`, `reranker_model` and
`rerank_depth`, as RM-14a specifies.

**Decision: not adopted (latency).** Condition (c) fails, so under the ticket's rule the
reranker stays off whatever the quality.

## 5. Against `ci.json`, for context only

`ci.json` was recorded on 2026-10-22, against about 2,068 documents. The restored set has 2,114,
so these deltas mix document-set drift with the change being measured. They are not the basis of
the decision.

| Run vs `ci` | Shared rows | Δ nDCG@5 / MRR / R@20 | Signature changed |
|---|---|---|---|
| off | 31 | −0.0096 / −0.0196 / +0.0050 | no |
| (a) shipped flags | 14 (17 skipped) | +0.3439 / +0.3357 / −0.0042 | yes |
| (b) batch 2048 | 31 | +0.3529 / +0.3554 / +0.0050 | yes |

The reranker-off row shows the drift: small, and in both directions. `ci.json` is **not**
re-recorded by this PR. Nothing in the ranking pipeline changed, and the reranker is off. The
drift is the usual "the document set moved" trigger in `docs/eval-gate.md`, and it belongs to
whoever next re-records.

## 6. What could change the verdict (not measured here)

The ticket rules out tuning on 31 rows, so none of these were tried. They are recorded for
whoever reopens the question:

- **The machine was swapping.** About 16–17 GB of swap was in use during the runs. On an idle
  machine the latency is probably lower, but halving p95 would still leave it at about 1.3 s.
- **Score fewer or shorter pairs.** Lower `BRAIN_RERANK_DEPTH` (for example 10) or
  `BRAIN_RERANK_MAX_CHARS`. The cost is roughly linear in pair tokens. Either change alters what
  is measured, so it needs its own run with all three conditions.
- **A smaller quantization.** `Q4_K_M` (438 MB). Its quality and latency are unmeasured.
- **The hosted backend.** Voyage `rerank-3-lite` is the latency-oriented model. It sends passages
  off the machine (confidential rows are withheld), so it needs the owner's explicit approval.

## 7. What changed in this PR

- `src/brain/templates/launchd/com.brain.reranker.plist.j2`: adds `--batch-size 2048
  --ubatch-size 2048` (§3).
- `tests/test_bin_launchd_reranker.py`: the exact-args test, plus the batch-fits-a-passage
  regression test.
- `docs/guides/local-reranker.md`: "What was tested" now covers the batch flags, the 512-token
  failure and the measured latency.
- The roadmap spec's Appendix B: RM-14c done. The RM-20 and RM-22b notes now say their
  measurements stack on the pipeline with the reranker off.
- `docs/plans/2026-10-23-backlog-reconciliation.md`: BL-5 and roadmap 5 marked "built, measured,
  not adopted (latency)".
- **Not changed:** `setup.py`, `ci.json`, the code default, and the owner's `.env`.

## 8. Owner step

None is required. The reranker stays off. If the owner later turns it on regardless of the
latency, they should re-run `brain-install-launchd --with-reranker` after this merges, so the
installed `com.brain.reranker` plist picks up the batch flags. A job installed from the earlier
template would skip most queries (§3). It is not installed on the owner's machine today.
