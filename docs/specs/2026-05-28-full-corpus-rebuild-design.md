# Full-corpus rebuild orchestrator (`brain-rebuild`) — design

**Date:** 2026-05-28
**Status:** Design — pending implementation plan
**Author:** Pat Morgan (via Claude)

## Problem

Bringing the whole corpus current after ingests requires running ~8 separate
commands in the right order: `reembed`, `enrich --backfill`, `backfill search`,
`graphrag build --backfill`, `graphrag refresh`, `graphrag communities refresh`,
plus the vault→wiki tail. Nothing chains them, so derived layers silently drift —
most visibly, `brain doctor` keeps reporting `communities stale` because an ingest
mutated `graph_relationships` edge weights and the community fingerprint
(`compute_source_graph_hash` over `(src,dst,weight)` triples) was never rebuilt.

There is already a partial orchestrator — the `brain-rebuild` console script — but
it only covers the vault→wiki tail (`vault export` → `sync-summaries` →
`prune-orphans` → `render --overlay` → `build_swap`). It does not touch
embeddings, summaries, search denorm, the entity graph, or communities.

## Goal

One command that brings the **entire** corpus current — embeddings, summaries,
search denorm, entity graph, edge weights, communities, and the wiki — in the
correct dependency order, safely, and idempotently. Reuse the existing
`brain-rebuild` name rather than introducing a parallel command.

Non-goals: re-ingesting source content; changing any stage's own internal logic;
re-implementing `build_swap`; a backwards-compat shim for the old bash template.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Scope | Full corpus rebuild — all 7 stages below |
| Failure semantics | **Fail-fast** — stop at first failed stage, report which/why, leave the rest unrun |
| Concurrency guard | Refuse if a `brain ingest*` process is running (`--force` overrides); **ignore** the always-on watchers (`brain vault sync --watch`, `brain.wiki.build_watcher`) |
| Mutual exclusion | Hold a Postgres **advisory lock** for the whole run so two rebuilds can't overlap |
| Control surface | `--dry-run`, `--only <stages>`, `--skip <stages>` |
| Command | Update the existing **`brain-rebuild`** — do not add a parallel name |
| Implementation home | A new **testable Python module** (the repo mandates pytest + 85% coverage + mypy + ruff; bash can't satisfy that) |
| `graphrag refresh` | **Included** in the routine full run (stage 5), skippable via `--skip`. A full rebuild should authoritatively recompute edge weights so the community fingerprint lands stable. Idempotent, low cost. |
| Default behavior | **Full rebuild by default**; `--wiki-only` preserves today's fast wiki-only path |

## Architecture

### Wiring

- The `brain-rebuild` console script (`pyproject.toml [project.scripts]` →
  `brain-rebuild = "brain.bin.rebuild:main"`) is **repointed** from the bash
  template (`exec_shim` → `src/brain/templates/bin/brain-rebuild.sh`) to the new
  Python orchestrator.
- Safe because `brain-rebuild` has **zero internal callers**: `brain-up`
  (cold start) and `build_watcher` both invoke `python -m brain.wiki.build_swap` /
  `build_and_swap` directly, not `brain-rebuild`. `setup.py` only registers the
  console-script name.
- The packaged bash template `brain-rebuild.sh` is **retired**. Its 5 wiki-tail
  steps are reimplemented as ordered stage calls in the orchestrator (see Stage
  list). The shared shim/launcher machinery (`_launcher.py`, `ensure_shim`) stays
  intact for `brain-up`/`brain-down`/`brain-status` — only the rebuild entry
  changes.
- The orchestrator is **not** exposed as a `brain rebuild` Typer subcommand, to
  avoid `brain rebuild` (space) vs `brain-rebuild` (hyphen) confusion. One entry:
  the `brain-rebuild` console script.

### Stage execution model

Each stage runs as a **subprocess invocation of the existing `brain` subcommand**
(e.g. `brain reembed`, `brain enrich --backfill`). Rationale: each stage is
already an independently-tested CLI command with its own error handling and
output; the orchestrator's only job is sequencing + selection + guard + lock.
Fail-fast = stop on the first non-zero exit code. This keeps the orchestrator
thin and isolates each stage's failure cleanly.

The advisory lock is held by the orchestrator's own short-lived Postgres
connection for the duration of the run, independent of the stage subprocesses.

### Stage list (canonical order + IDs)

`--only` / `--skip` select by these stage IDs:

| # | Stage ID | Command invoked | Notes |
|---|---|---|---|
| 1 | `embeddings` | `brain reembed` | backfill NULL embeddings + finalize HNSW |
| 2 | `summaries` | `brain enrich --backfill` | LLM summary backfill (NULL-summary docs) |
| 3 | `search` | `brain backfill search` | denorm title/tags + recompute `search_extras` |
| 4 | `graph` | `brain graphrag build --backfill` | entities + `CO_OCCURS` edges (per-doc watermark) |
| 5 | `graph-weights` | `brain graphrag refresh` | recompute all edge weights from contributions |
| 6 | `communities` | `brain graphrag communities refresh` | Louvain + summaries |
| 7 | `wiki` | wiki tail (below) | the former bash template, reimplemented |

The `wiki` stage expands to the existing 5 steps, run in order, fail-fast:
`brain vault export` → `brain vault sync-summaries` → `brain vault prune-orphans
--apply` → `brain vault render --overlay --no-build` → `python -m
brain.wiki.build_swap`.

Dependency rationale: 1–3 are independent of each other but should complete so
later stages operate on complete data; 6 depends on 4 (graph) and benefits from 5
(stable weights); 7 (wiki) is independent of the graph stages but is placed last
so it renders the freshest summaries/mirrors.

### Flags

| Flag | Behavior |
|---|---|
| (default) | Full rebuild — all 7 stages |
| `--wiki-only` | Run only the `wiki` stage (today's fast path). Mutually exclusive with `--only`/`--skip` |
| `--dry-run` | Print the ordered stage plan (post-selection) and exit; run nothing, take no lock |
| `--only <ids>` | Comma-separated stage IDs to run (others skipped) |
| `--skip <ids>` | Comma-separated stage IDs to skip |
| `--force` | Override the in-flight-ingest guard |

`--only` and `--skip` are mutually exclusive. Unknown stage IDs are a fast
`BadParameter` error listing valid IDs.

### Concurrency guard + lock

1. **Ingest guard (pre-flight):** scan for a running `brain ingest*` process
   (the same `ps`/`pgrep` signal that diagnosed the original staleness). If found
   and `--force` is not set, exit non-zero with a clear message ("an ingest is in
   flight; wait for it to finish or pass --force"). The always-on watchers are
   explicitly **not** matched.
2. **Advisory lock:** acquire a Postgres session-level advisory lock (a fixed,
   namespaced key) on the orchestrator's connection. If already held, exit
   non-zero ("another rebuild is running"). Released on exit. `--dry-run` skips
   both the guard and the lock.

### Failure semantics

Fail-fast. On the first stage that exits non-zero: print which stage failed and
its exit status, do not run subsequent stages, release the lock, exit non-zero.
A summary line reports completed vs. remaining stages.

## Error handling

- Guard/lock failures exit with distinct, documented non-zero codes and
  actionable messages (no stack traces for the expected "ingest running" /
  "rebuild already running" cases).
- A stage subprocess failure surfaces the child's exit code; the orchestrator's
  own exit code signals "stage N (`<id>`) failed".
- `--dry-run` never touches the DB, never takes the lock, never runs a stage.

## Testing

Per repo policy (pytest, real-Postgres fixture, fake embedder, 85%+ coverage,
CLI commands ≥85%):

- **Unit:** stage-selection resolution (`--only`/`--skip`/`--wiki-only`,
  mutual-exclusion errors, unknown-ID errors); plan ordering; dry-run output.
- **Unit (mocked subprocess):** fail-fast stops after the first non-zero exit;
  the remaining stages are not invoked; exit code identifies the failed stage.
- **Unit:** ingest-guard detection (process present → refuse; `--force` →
  proceed; watcher processes → ignored) with the process scan mocked.
- **Integration (real DB):** advisory lock is acquired/released; a second
  concurrent orchestrator refuses; `--dry-run` takes no lock and runs nothing.
- **Regression:** reproduce the original "communities stale after refresh"
  motivation — a full run on a quiescent graph leaves `brain doctor` reporting
  `fingerprint current`.
- Console-script repoint covered by the existing `tests/test_bin_scripts.py`
  pattern (update for the new entry).

## Out of scope

- Re-ingesting or fetching new source content.
- Changing any individual stage's internal behavior.
- Parallelizing stages (sequential + fail-fast only).
- A `brain rebuild` Typer subcommand (console script only).
