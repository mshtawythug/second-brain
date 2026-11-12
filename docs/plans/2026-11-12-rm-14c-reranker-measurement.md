# RM-14c: measure the reranker, then adopt it or record the result

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

- **Ticket:** RM-14c, in `docs/specs/2026-10-23-roadmap-completion-design.md` (§ "RM-14c — Measure,
  then adopt or record", and Appendix B).
- **Status:** APPROVED by team-lead on 2026-11-12. Rulings:
  - Decisions 1–3 as recommended.
  - O1: the test container is started with `docker start second-brain-age-test` by another
    teammate.
  - O2: yes. Restore into `rm14c_eval_20261112` only.
  - O3: yes. Use the brain-qa lock convention from `brain-qa/README.md`.
  - O4: yes, adopt branch only. Name the trigger and the snapshot in the commit.
  - O6: yes.
  - O7: rejected.
  - O8: becomes an "Owner step" section in the PR body.
  - O9: if R1 fires, stop and report. The intended variant is `-ub 2048`, labelled, with the
    template fixed in the same PR, but only after confirmation.
- **Branch:** `feat/rm-14c-reranker-measurement`, cut from `second-brain/master` at `a8b4a1d1`.
- **Why now:** the owner moved RM-14c back in front of the v0.4.0 tag. The Appendix B row still
  reads "deferred past 0.4.0", and this PR corrects it.

## 1. What RM-14c requires

The spec text, restated:

1. Run on the owner's machine, not in CI. Run `brain eval --baseline ci --diff` twice in one
   session, once with `BRAIN_RERANKER=none` and once with `BRAIN_RERANKER=llamacpp`. Add a
   `voyage` run only if a key is configured. Check the document count with `brain status` before
   the runs.
2. Record the following for each run:
   - the three means (nDCG@5, MRR, Recall@20);
   - the per-category table;
   - the p50 and p95 of `EvalResult.stage_ms["rerank"]` over the golden queries.
3. **Adoption rule.** All three conditions must hold:
   - **(a)** no mean regresses by more than `1e-4`;
   - **(b)** nDCG@5 or MRR improves by at least **0.02** absolute;
   - **(c)** p95 added latency is at most **1.0 s**.
4. **If adopted:**
   - The owner's `$BRAIN_HOME/.env` sets `BRAIN_RERANKER=llamacpp`.
   - `brain setup` offers the reranker. That is growth in `setup.py`, which is over the ceiling, so
     the growth is recorded and the row's hop promoted.
   - `ci.json` is re-recorded, with the trigger "ranking change (reranker adopted)".
   - The `search.md` memory file is updated.
   - The **code default stays `none`**.
5. **If not adopted:** a measurement note at `docs/plans/<date>-reranker-measurement.md` records
   the numbers, and the backlog row is closed as "built, measured, not adopted".
6. **Acceptance criteria:**
   - **(1)** The PR carries both runs' means, per-category tables, latency percentiles, and the
     document count.
   - **(2)** Each of the three conditions is evaluated explicitly.
   - **(3)** If adopted, `brain eval --baseline ci --diff --fail-below` exits 0 with the reranker
     on, after the re-record.

What already exists on master, read in source:

| Piece | Where | Relevant facts |
|---|---|---|
| Stage | `src/brain/rerank.py` | It scores the head (`depth`) with passages built as `title + "\n" + snippet`, cut to `max_chars`. Any `RerankError` returns hybrid order with `applied=False`. `signature()` adds `reranker`, `reranker_model` and `rerank_depth`. |
| Backend | `src/brain/rerank_backends.py` | `LlamaCppReranker` sends `POST {url}/v1/rerank` with `{model, query, documents, top_n}`. There are no retries, and every call has an explicit timeout. |
| Settings | `src/brain/settings/rerank.py` | `BRAIN_RERANKER` defaults to `none`. Other defaults: `DEPTH` 20, `TIMEOUT_S` 5.0, `MAX_CHARS` 1500. `BRAIN_RERANK_URL` defaults to `http://127.0.0.1:8081` and must be a loopback address. |
| Eval wiring | `src/brain/eval/runner.py` | With `stages=None`, `run_eval` builds the stages from `os.environ`. `EvalResult.stage_ms` holds timings for applied stages only. `stages_skipped` marks rows that fell back to hybrid order. `save_baseline` refuses those rows, and `diff_reports` excludes them. |
| CLI | `brain eval` (`cli.py` `eval_cmd`) | The flags are `--json`, `--record-baseline`, `--baseline --diff --fail-below` and `--corpus`. Plain `--json` emits the full `EvalReport` (`dataclasses.asdict`), which includes **query text** and `stage_ms`. `--diff --json` emits only the diff. |
| Runtime | `docs/guides/local-reranker.md`, plist template | The documented setup is llama.cpp 0.7.0 with `bge-reranker-v2-m3-Q8_0.gguf` at about 804 MiB resident. The plist passes only `-m --embedding --pooling rank --reranking --host --port`, with no `-b`/`-ub`. |

Live machine state, checked while planning (2026-11-12) with read-only probes:

- `llama-server` is at `/opt/homebrew/bin`. The model file is in `~/.brain/models/`.
- `com.brain.reranker` is **not** installed.
- Port **8081 is taken** by a `java` listener, which is LanguageTool, as the guide warns. Port 8091
  is free.
- Ollama is up on 11434 with no model loaded. `~/workspace/brain-qa/ollama.lock`
  is free.
- **The 5434 test server is DOWN**: no `second-brain-age-test` container is running. See §5, O1.
- The host's `pg_restore` is 14.23. The dump was made by `pg_dump` 16.14, so a PG16 `pg_restore`
  is needed (§3, step 3).
- The latest backup is `~/.brain/backups/brain-backup-20261112-000417-pre-031.tar.gz`. Its
  manifest gives:
  - schema at migration head `030`; embedder `arctic/1024`; HNSW index present;
  - **2,114 documents / 22,322 chunks / 1,243 sources**;
  - `dump_excluded_schemas: [ag_catalog, brain_graph]`, so there is no AGE data to restore;
  - `db/second_brain.dump` of 155,699,943 bytes, sha256 `826f6827…896f`.
- `ci.json` was recorded on 2026-10-22, against about 2,068 documents. Its signature is `arctic`,
  halflife 180.0, snippet context 200, floor 0.25. The backup holds more documents than that
  baseline saw (§4, R4).

## 2. Deliverables

| # | Deliverable | Committed? |
|---|---|---|
| D1 | This plan, `docs/plans/2026-11-12-rm-14c-reranker-measurement.md`. | Yes |
| D2 | **Eval report and decision record**, `docs/plans/2026-11-12-reranker-measurement.md`, the file the spec names. It holds the aggregate numbers only (§6 lists exactly what) and is written whether or not the reranker is adopted. | Yes |
| D3 | Appendix B row RM-14c set to `DONE (#PR)`, with the outcome. If not adopted, also close the backlog row "built, measured, not adopted" in the backlog doc. The RM-20 and RM-22b rows say their measurement is "owed after RM-14c"; add a one-line note there saying which pipeline they now measure on top of. | Yes |
| D4 | **Adopt branch only:** the `brain setup` offer line in `setup.py`, recorded in both places (inline comment and module docstring); the `docs/ceiling-table.md` row (promote the trailing hop, then add ours); a TDD test for the offer; the re-recorded `tests/eval/baselines/ci.json`; and `docs/configuration.md` and the guide noting the measured result. | Yes |
| D5 | **Adopt branch only:** the `search.md` memory update. | Memory dir, not repo |
| D6 | Code: **none** in the not-adopted branch. RM-14c's test plan is "no new automated test; the recorded commands and outputs are the deliverable". The aggregation is a one-off `python -I` snippet run from the scratchpad and **not committed**. That keeps CLAUDE.md's rule that every committed piece of code has tests. | — |

Never committed and never pasted into a message, doc, commit or PR: the golden corpus, any
`--json` report (it contains query text and document ids), and any local baseline file.

## 3. Exact commands (primary: restore the backup into a scratch DB on 5434)

Every shell starts with this block. `$S` is this session's scratchpad. `BRAIN_HOME` points at a
scratch home, so `~/.brain/.env` and its prod `DATABASE_URL` are never read.
`BRAIN_IGNORE_CWD_DOTENV=1` stops the cwd walk-up from reaching the main checkout's `.env`, which
also names prod. The worktree has no `.env`.

```bash
export BRAIN_PROD_DATABASE_URL=postgresql://localhost:1/none BRAIN_SKIP_CANARY=1
S=<session scratchpad>/rm14c   # session-scoped, outside the repo
WT=~/workspace/second-brain/.claude/worktrees/impl-rm14c
SCRATCH_DB=rm14c_eval_20261112
export BRAIN_HOME=$S/home BRAIN_IGNORE_CWD_DOTENV=1
export DATABASE_URL=postgresql://brain:brain@localhost:5434/$SCRATCH_DB
export BRAIN_EMBEDDER=arctic BRAIN_GRAPH_ENABLED=false BRAIN_DOC_LEG=false
# pinned to ci.json's config_signature, so a diff against ci compares like with like
export BRAIN_RECENCY_HALFLIFE_DAYS=180 BRAIN_SNIPPET_CONTEXT_TOKENS=200 BRAIN_VECTOR_SIM_FLOOR=0.25
export BRAIN_RERANK_URL=http://127.0.0.1:8091   # 8081 is LanguageTool's
CORPUS=~/workspace/second-brain/tests/eval/golden_corpus.yaml   # read only
case "$DATABASE_URL" in *:5434/*) ;; *) echo "REFUSE: not 5434"; exit 1;; esac
```

**Step 0: preflight, before anything writes.**

```bash
df -h /                                   # ≥ 6.5 GiB for the restore; ≥ 8 GiB before each model run
nc -z -G 3 127.0.0.1 5434 || echo "5434 down: stop and ask team-lead (O1)"
mkdir -p $S/home $S/out $S/restore
```

**Step 1: an own venv**, so no venv is shared with another agent.

```bash
uv venv $S/venv --python 3.11 && uv pip install --python $S/venv/bin/python -e "$WT"
B=$S/venv/bin/brain
```

**Step 2: extract the DB dump only.** This reads `~/.brain` and never writes to it. `vault.tar`
is never extracted.

```bash
tar -xzf ~/.brain/backups/brain-backup-20261112-000417-pre-031.tar.gz -C $S/restore db/second_brain.dump
shasum -a 256 $S/restore/db/second_brain.dump   # must equal the manifest's 826f6827…896f
```

**Step 3: create the scratch DB and restore it on 5434.** The host `pg_restore` is 14 and the dump
is 16, so the test container's own PG16 `pg_restore` is used through `docker exec`. That is not a
start or a stop, but see O2.

```bash
psql "postgresql://brain:brain@localhost:5434/postgres" -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE $SCRATCH_DB"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pgcrypto;"
docker exec -i second-brain-age-test pg_restore --no-owner --no-acl -U brain -d $SCRATCH_DB \
  < $S/restore/db/second_brain.dump 2> $S/out/pg_restore.err
grep -c "error" $S/out/pg_restore.err   # expect 0 or only "extension already exists"; anything else → stop
```

The fallback for O2 is `docker cp` of the dump into the container, then `pg_restore` from the file
path, which also allows `-j 2`. **`brain restore` is not used.** Read in source, it is built to
replace the configured brain:

- it runs `pg_restore` inside the container named by `postgres_container_name()`, which is the
  **prod container**, `second-brain-postgres`;
- it swaps a staging DB in place of the target;
- without `--db-only` it swaps the vault;
- it writes a replica marker under `BRAIN_HOME`.

Pointing it at a scratch DB would take a code change.

**Step 4: verify the restore against the manifest.**

```bash
$B status        # expect 2,114 documents / 22,322 chunks / 1,243 sources (manifest counts)
psql "$DATABASE_URL" -Atc "SELECT max(name) FROM schema_migrations"   # 030_near_duplicates.sql
```

Migration 031 is **not** applied. The RM-20 document leg is off, and `doc_embeddings` checks for
its table with `to_regclass`, so eval does not need it. The scratch DB therefore mirrors the
backup exactly.

**Step 5: take the model lock and start the one runtime.** Run this immediately before the model
runs.

```bash
df -h /                                                     # ≥ 8 GiB or do not start
mkdir ~/workspace/brain-qa/ollama.lock && \
  echo "impl-rm14c RM-14c eval $(date -u +%FT%TZ) pid $$" > ~/workspace/brain-qa/ollama.lock/owner
ollama ps                                                   # expect nothing loaded
llama-server --version > $S/out/llama-version.txt 2>&1
llama-server -m ~/.brain/models/bge-reranker-v2-m3-Q8_0.gguf \
  --embedding --pooling rank --reranking --host 127.0.0.1 --port 8091 \
  > $S/out/llama.log 2>&1 &
echo $! > $S/out/llama.pid
until curl -sf http://127.0.0.1:8091/health; do sleep 1; done   # bounded by hand (~2 s measured)
```

The server runs with the **template's flags exactly**, so the measurement matches what
`brain-install-launchd --with-reranker` would ship.

**Step 6: synthetic smoke test of the batch limit** (risk R1). Send one rerank of 20 synthetic
passages of 1,500 characters each, using lorem-style text and never corpus text.

```bash
$S/venv/bin/python -I - <<'PY'
import httpx
docs = [("lorem ipsum dolor sit amet " * 60)[:1500] for _ in range(20)]
r = httpx.post("http://127.0.0.1:8091/v1/rerank", timeout=30,
               json={"model": "bge-reranker-v2-m3", "query": "synthetic probe", "documents": docs, "top_n": 20})
print(r.status_code, len(r.json().get("results", [])) if r.status_code == 200 else r.text[:200])
PY
```

A non-200 reply means stop and report (R1). It does not mean change the flags silently.

**Step 7: the runs of record**, in one session, against a DB that cannot move. Each config runs
**twice**, so any nondeterminism (the HNSW vector leg, llama.cpp scoring) is visible.

```bash
# A. reranker off
BRAIN_RERANKER=none     $B eval --corpus $CORPUS --json --record-baseline rm14c-none > $S/out/none-1.json
BRAIN_RERANKER=none     $B eval --corpus $CORPUS --json > $S/out/none-2.json
BRAIN_RERANKER=none     $B eval --corpus $CORPUS --baseline ci --diff > $S/out/none-vs-ci.txt 2>&1
# B. reranker on (llamacpp)
BRAIN_RERANKER=llamacpp $B eval --corpus $CORPUS --json > $S/out/llamacpp-1.json
BRAIN_RERANKER=llamacpp $B eval --corpus $CORPUS --json > $S/out/llamacpp-2.json
BRAIN_RERANKER=llamacpp $B eval --corpus $CORPUS --baseline ci --diff > $S/out/llamacpp-vs-ci.txt 2>&1
# C. the adoption rule's condition (a), as the repo's own gate computes it
BRAIN_RERANKER=llamacpp $B eval --corpus $CORPUS --baseline rm14c-none --diff --fail-below \
  > $S/out/llamacpp-vs-none.txt 2>&1; echo "exit=$?" >> $S/out/llamacpp-vs-none.txt
```

`rm14c-none.json` is written to `$WT/tests/eval/baselines/`, which `.gitignore` ignores
(`*.json`, with only `!ci.json` allowed). It is deleted at the end of the work.

**Step 8: unload and release, always**, including after a failure.

```bash
kill "$(cat $S/out/llama.pid)"; sleep 2; kill -0 "$(cat $S/out/llama.pid)" 2>/dev/null && echo "still alive: report, do not escalate"
ollama stop snowflake-arctic-embed2      # unloads the query embedder; Ollama itself keeps running
ollama ps                                # expect nothing loaded
rm -rf ~/workspace/brain-qa/ollama.lock
```

**Step 9: aggregate.** A one-off `python -I` reads `$S/out/*.json` and prints only:

- means per run and their deltas;
- the per-category table (count plus the three means);
- the p50 and p95 of `stage_ms["rerank"]`, using the nearest-rank method over the non-degraded
  rows where the stage was applied;
- the count of `degraded` rows and of `stages_skipped` rows;
- whether run 1 equals run 2 for each config.

It never prints `query`, `expected_doc_ids` or `actual_doc_ids`. Those aggregates are what goes
into D2.

**Step 10: clean up the scratch data.** Do this after the team-lead accepts the numbers, or after
the adopt-branch re-record.

```bash
rm -f $WT/tests/eval/baselines/rm14c-none.json
rm -f $S/out/*.json $S/restore/db/second_brain.dump
psql "postgresql://brain:brain@localhost:5434/postgres" -c "DROP DATABASE $SCRATCH_DB"   # scratch DB only, named explicitly
```

Dropping the scratch DB is the only destructive statement in this plan. It names a database that
this plan created, on the test server, never `second_brain_test` and never 55432. The team-lead
confirms it before it runs (O6).

**Adopt branch** (only if (a), (b) and (c) all hold, and the owner agrees):

```bash
BRAIN_RERANKER=llamacpp $B eval --corpus $CORPUS --record-baseline ci     # re-record, reranker on
BRAIN_RERANKER=llamacpp $B eval --corpus $CORPUS --baseline ci --diff --fail-below; echo "exit=$?"   # AC3: must be 0
```

These repeat steps 5 and 8 around them. Then:

- the `setup.py` offer line, written TDD;
- `ruff check`, `mypy src/` and the targeted tests under `.claude/full-suite.lock`, never the full
  suite locally;
- `scripts/check_ceiling_table.py`.

The owner's own `~/.brain/.env` (`BRAIN_RERANKER=llamacpp`, `BRAIN_RERANK_URL=…:8091`) and
`brain-install-launchd --with-reranker` are **owner actions**. I never write to `~/.brain`.

## 4. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | **The llama.cpp batch limit.** `llama-server`'s default physical batch (`-ub`) is 512 tokens. A non-causal reranker has to fit each query+passage pair in one ubatch. A 1,500-character passage can approach or pass that, which turns into HTTP 500, then `RerankError`, then the row is skipped (`stages_skipped`). The guide only measured 3 tiny passages, and the template sets no `-ub`. | Step 6 probes this with synthetic text before any run of record. Any skipped row is reported. If it fails, I stop and report rather than change flags. Changing them would mean a template fix (an RM-14b follow-up) and measuring a configuration that does not ship. |
| R2 | **Memory.** A reranked run needs two models at once: the Ollama query embedder (arctic) and the llama-server reranker (about 0.8 GiB). This cannot be split, because each query is embedded and then reranked. The machine has been swapping heavily. | Both are small. Run `ollama ps` first, which must be empty. Nothing else runs under the lock. Step 8 unloads both. I ask team-lead to treat "embedder + reranker" as one eval workload (O3). |
| R3 | **Disk.** Step 2 writes 155 MB. The restored DB is about 1 GiB or less (22k × 1024-dim vectors plus HNSW plus text) and lands in the Docker VM's disk, which is the same host volume. Free space was 9.3 GiB when this plan was written. | Check `df -h /` before steps 2, 3 and 5. Nothing runs below 6.5 GiB, and model runs need 8 GiB. Clean up in step 10. |
| R4 | **Diffs against `ci.json` are noisy.** The backup has about 46 more documents than `ci.json` saw on 2026-10-22, so `--baseline ci --diff` mixes document-set drift into every delta, for both configs. | The adoption decision rests on the **paired comparison on one frozen DB** (steps 7A against 7B, and 7C's gate against `rm14c-none`), not on a diff against `ci`. The `ci` diffs are recorded only for context. This is stronger than the spec's design, because the scratch DB cannot move between runs (the spec's own stated risk). |
| R5 | **The snapshot is not live prod.** The backup is from 2026-11-12 00:04Z. A re-recorded `ci.json` (adopt branch) would match that snapshot, not whatever prod holds when the owner next gates. | The UUIDs are identical, because `pg_restore` keeps them. The usual "the document set moved" rule from `docs/eval-gate.md` applies. The owner decides (O4). |
| R6 | **Judgment noise.** There are 31 rows, and one row moving from rank 2 to rank 1 is worth about 0.016 MRR. | The spec's 0.02 bar already accounts for it. There is no tuning: no sweeps of depth or `max_chars`, because that would be fitting to 31 rows. |
| R7 | **Privacy.** `--json` output carries query text and document ids. | Outputs stay in the scratchpad. Only aggregates leave it. The step 9 script never prints those fields. Files are deleted in step 10. |
| R8 | **Accidentally touching prod.** | Process-env `DATABASE_URL`, a scratch `BRAIN_HOME`, `BRAIN_IGNORE_CWD_DOTENV=1`, and the `case` guard in the env block. `brain restore` is never used, because it targets the prod container. Nothing connects to 55432. |
| R9 | **Version drift.** Homebrew llama.cpp may now be newer than the guide's 0.7.0. | Record `llama-server --version` in D2. If it differs, note it. The request shape is covered by step 6. |

## 5. What needs the owner or team-lead

| # | Question | My recommendation |
|---|---|---|
| O1 | **The 5434 test server is down**, and I may not start containers. Who brings up `docker compose -f docker-compose.age-test.yml up -d`? Another team may own it. | Team-lead (or the owner) starts it, or tells me when it is up. Plain `up -d` without `-v`. Steps 3 onward wait for it. |
| O2 | Is it OK to run `docker exec -i second-brain-age-test pg_restore …` (the container's PG16 tools, no start or stop)? | Yes. The alternative is installing `postgresql@16` from Homebrew on the host, which adds about 100 MB of disk for one restore. |
| O3 | Is the embedder plus the reranker loaded together acceptable as "one model workload" under the lock? | Yes. The total is about 2 GiB, the lock is held, and both are unloaded in step 8. |
| O4 | **Adopt branch:** may `ci.json` be re-recorded from the restored 2026-11-12 snapshot rather than from live prod? | Yes. The UUIDs match, the alternative is a prod connection, and the trigger and snapshot are named in the commit. |
| O5 | Should Voyage be run? | **No.** No Voyage key is in this setup, the owner's embedder is local, and Voyage would send the passages to a hosted service. Run it only if the owner explicitly asks. |
| O6 | Drop the scratch DB at the end? | Yes, after team-lead confirms, by explicit name. |
| O7 | **Fallback only, needs explicit owner approval:** evaluate read-only against prod Postgres on 55432 with `llama-server`, as the ticket literally says. | **Not recommended.** Use it only if the restore path is impossible. It would need a read-only role or `default_transaction_read_only`, and it breaks the standing rule against connecting to 55432. |
| O8 | **Adopt branch:** the owner sets `BRAIN_RERANKER=llamacpp` and `BRAIN_RERANK_URL=http://127.0.0.1:8091` in `~/.brain/.env`, then runs `brain-install-launchd --with-reranker`. | These are owner actions. I never write to `~/.brain`. |
| O9 | If R1 fires (skipped rows), do we stop, or measure a variant with `-ub 2048 -b 2048` while a template fix is filed? | Stop and report first. If the owner wants the number, measure the variant and label it clearly as "not the shipped config", and file the template fix as a separate ticket. |

## 6. What the eval report (D2) contains, as aggregates only

- The date, the `llama-server --version` output, the model file with its sha256 checked against the
  guide, the backup label and manifest counts, and the `brain status` counts (they must match).
- The pinned config: the four `ci.json` signature keys, plus depth 20, `max_chars` 1,500 and
  timeout 5.0 s.
- For each config and each repetition: the three means and the per-category table.
- The paired deltas, with each of conditions (a), (b) and (c) given a verdict and its number.
- The 7C gate's exit code.
- `rerank` latency p50 and p95 in ms, with n.
- The degraded and skipped row counts, and whether run 1 equals run 2.
- The `ci` diffs, for context only, with the caveat from R4.
- The decision: **adopted** or **built, measured, not adopted**, and what follows (the adopt-branch
  list, or the closed backlog row).
