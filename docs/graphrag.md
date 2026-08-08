# GraphRAG (graph retrieval)

> Part of the [Second Brain](../README.md) docs — see [docs/README.md](README.md) for the full index.

![GraphRAG: "themes with a person" then a fused graph+vector search, over a synthetic corpus](assets/graphrag.gif)

*Entity-graph retrieval in action: the themes that cluster around a person, then a fused graph + vector search. (Synthetic Larkspur corpus; regenerate with `bin/brain-graphrag-gif`.)*

## How it works

GraphRAG adds **entity-centric graph retrieval alongside** the existing
hybrid vector + FTS search — it does not replace `brain search`. Where hybrid
search ranks documents by lexical + semantic similarity, GraphRAG builds a
graph of the people and concepts that co-occur across your corpus and
retrieves over that structure. It answers questions hybrid search struggles
with — "what themes come up in my conversations with X", "which clusters of
people and topics dominate my notes" — by traversing relationships instead of
matching text.

It exposes a `brain graphrag …` CLI command group (with full MCP parity via
the `brain_graphrag_*` tools), with five retrieval modes:

| Mode | What it does |
|---|---|
| `local` | Entity-centric — seeds on an entity and traverses its co-occurrence neighbourhood. |
| `themes` | "Themes with X" — scopes to a person and returns ranked theme groups (the headline use case; requires `--person`). |
| `global` | Community-level — RRF over detected communities (run `brain graphrag communities build` first). |
| `fuse` | RRF-merges the local-graph document leg with the vector/FTS hybrid leg into one ranked list. |
| `auto` *(default)* | Heuristic router — picks `themes` / `global` / `local` based on the query and whether a person resolves. |

The graph runs on [Apache AGE](https://age.apache.org/) (an openCypher graph
extension) inside the same Postgres. Both aspects — **people** (always on) and
**concepts** (LLM entity extraction over topics/projects/orgs/tools) — are
default-on; flip `BRAIN_GRAPH_CONCEPTS=false` to disable the concept aspect if
you don't have an LLM available locally. No raw Cypher is ever accepted or
shown; every command takes structured params and the backend injects the
tenant + traversal caps.

## Enabling GraphRAG

GraphRAG is on by default after `brain setup` (the one-liner installer). The
packaged `docker-compose.yml` template provisions a custom Postgres image —
`second-brain-age:pg16-v1.5.0-rc0-pgv0.8.6` (PostgreSQL 16 + pgvector 0.8.6 +
pgcrypto + Apache AGE 1.5.0-rc0), built from
`src/brain/templates/docker/age/Dockerfile` — and `brain init` bootstraps the
AGE extension and the recomputable graph mirror automatically.

If you installed by hand (the "Step by step" path in the
[README](../README.md#quick-start)) on a stock pgvector image, GraphRAG will
run in soft-degraded mode: ingest succeeds, but graph sync is a silent no-op
and `brain graphrag …` commands return a friendly "AGE not available" guard.
To enable the full graph stack, follow the
[Upgrading an existing brain to the AGE image](#upgrading-an-existing-brain-to-the-age-image)
recipe below.

After install, backfill the graph from your existing documents and (optionally)
detect communities for `--mode global`:

```bash
brain graphrag build --backfill          # idempotent + resumable
brain graphrag communities build         # required only for --mode global
brain doctor                             # AGE + graph-drift + communities health
```

The relevant `.env` knobs are documented in `.env.example` (search for
`BRAIN_GRAPH_`): `BRAIN_GRAPH_ENABLED` (default `true`), `BRAIN_GRAPH_CONCEPTS`
(default `true`), `BRAIN_GRAPH_TENANT`, `BRAIN_GRAPH_DEPTH`,
`BRAIN_GRAPH_EXTRACT_MODEL`, the community-detection tuning
(`BRAIN_GRAPH_COMMUNITY_*`), and more — all with sensible defaults.

## Query modes

```bash
# Graph retrieval. --mode defaults to auto (the heuristic router).
brain graphrag search "platform migration tradeoffs"
brain graphrag search "platform migration tradeoffs" --mode local
brain graphrag search "hiring plans" --mode fuse        # graph leg ⊕ vector/FTS leg
brain graphrag search "..." --json                      # machine-readable

# "Themes in my conversations with X" — the headline. --person is required.
brain graphrag themes --person "Jane Doe"
brain graphrag themes --person "Jane Doe" --limit 5 --synthesize

# Inspect one entity's co-occurrence neighbourhood. High-degree entities cap
# rendered neighbours at -n (default 30; -n 0 shows all); --json is never capped
# and documents stay at the graph default regardless of -n.
brain graphrag entity "Project Phoenix"
brain graphrag entity "Project Phoenix" -n 50

# Community admin (global mode).
brain graphrag communities build        # detect + summarize (skips if graph unchanged)
brain graphrag communities refresh      # force a rebuild regardless of the dirty gate
brain graphrag communities list         # admin view of materialized communities

# Index maintenance. build (with concepts) + refresh also auto-collapse cross-type
# duplicate concept entities — the same name extracted as `org` in one doc and
# `project` in another merges into one row (highest-precedence type wins), keeping
# the best surface form (e.g. `AcmePlatform`, not `acmeplatform`).
brain graphrag build --backfill         # reconcile every existing doc into the graph
brain graphrag build --force            # authoritative full rebuild (recover a dropped mirror)
brain graphrag refresh                  # recompute edge weights + collapse cross-type duplicates
```

The same surface is available to any MCP client (Claude Desktop, Claude Code, and others) through the
`brain_graphrag_search`, `brain_graphrag_themes`, `brain_graphrag_entity`,
`brain_graphrag_build`, and `brain_graphrag_communities_build` MCP tools.

## Upgrading an existing brain to the AGE image

If your brain was installed before 2026-05-22 (or you used the step-by-step
path on the committed `docker-compose.yml`, which intentionally stays on stock
pgvector), the graph stack runs in soft-degraded mode until you flip the
container image. The cutover is about the container, not the data — the AGE
graph itself is a recomputable mirror you'll rebuild with
`brain graphrag build --force` after the swap.

```bash
# 1. Back up your database before any image change (host bind-mount; mandatory).
mkdir -p ~/brain-backups
docker exec second-brain-postgres pg_dump -U brain -Fc -d second_brain \
  > ~/brain-backups/second_brain-precutover-$(date +%Y%m%d-%H%M%S).dump

# 2. Build the AGE image locally. The Dockerfile is packaged in this repo.
docker build -t second-brain-age:pg16-v1.5.0-rc0-pgv0.8.6 \
  -f src/brain/templates/docker/age/Dockerfile src/brain/templates/docker/age/

# 3. Create a gitignored docker-compose.override.yml that pins the AGE image
#    only on your machine — the committed docker-compose.yml stays on stock
#    pgvector as the repo default.
cat > docker-compose.override.yml <<'YAML'
services:
  postgres:
    image: second-brain-age:pg16-v1.5.0-rc0-pgv0.8.6
YAML

# 4. Restart the container so Compose picks up the new image.
docker compose down
docker compose up -d

# 5. Bootstrap AGE + recompute the graph mirror.
brain init                               # creates the age extension + graph mirror
brain graphrag build --force             # authoritative full rebuild
brain graphrag communities build         # optional, needed for --mode global
brain doctor                             # AGE + graph-drift should now report OK
```

If something goes wrong, `docker compose down && docker compose up -d` with
the override removed returns you to stock pgvector (the data on disk is
unchanged — graph rows live in their own tables and AGE catalogue, both of
which are safely re-derivable from the backup).

**Postgres tuning + the override `command:` caveat.** The committed
`docker-compose.yml` starts Postgres with `command: postgres -c
shared_buffers=512MB` — the brain's working set (~266MB) exceeds Postgres's
stock 128MB default, which otherwise forces cold-cache FTS re-reads from disk.
The AGE override above replaces only `image:`, so it *inherits* that `command:`
unchanged. But Compose **replaces** the `command:` field — it does not merge
it — so if you ever add your own `command:` to `docker-compose.override.yml`,
you must restate `-c shared_buffers=512MB` there too, or the container silently
drops back to the 128MB default. The new setting takes effect on the next
container restart.
