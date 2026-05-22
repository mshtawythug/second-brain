---
name: brain-graph
description: >
  Graph retrieval over the user's local "second brain" — an entity-centric
  GraphRAG layer (Apache AGE) that sits alongside the plain hybrid search. Use
  this skill when the question is about THEMES, PATTERNS, or CONNECTIONS across
  the user's interactions rather than a single-document lookup: "what themes
  keep coming up in my conversations with X", "what topics connect A and B",
  "how has my thinking on Z evolved", "what are the recurring themes in my
  brain", "show me everything around person/topic Q and what it links to".
  Trigger on phrases like "themes with", "patterns across", "what connects",
  "what keeps coming up", "recurring topics", "the bigger picture across my
  notes", "map out", "who/what is related to", or any request that wants the
  GRAPH shape (entities + relationships + clusters) instead of a flat ranked
  list of documents. For plain "find docs about X" lookups, voice writing, or
  per-document Q&A, use `consult-brain` instead.
  MANDATORY TRIGGERS: themes with, themes in my conversations, patterns across,
  what connects, what keeps coming up, recurring themes, recurring topics, how
  has my thinking evolved, map out my, who is related to, what is related to,
  the bigger picture across, graph of my, cluster my notes, overall themes in
  my brain.
---

# Brain Graph (GraphRAG)

Use the `brain graphrag` command tree to query the second brain's **entity
graph** — people and concepts as nodes, co-occurrence as edges, plus detected
community clusters. This is a *parallel* retrieval surface to plain
`brain search`; the underlying hybrid FTS+vector index is untouched.

For plain document lookup, voice writing, and per-doc Q&A, use `consult-brain`.
For ingest, see `ingest-brain`; authoring `brain-authoring`; TODO/action-items
`brain-todo`; health/maintenance `brain-maintenance`.

## Graph vs. plain search — pick the right tool

| The user wants… | Reach for |
|---|---|
| "Find docs about X" / a quote / a fact from one doc | `consult-brain` → `brain search` |
| **Themes/patterns in conversations with a person** | `brain graphrag themes --person "X"` |
| **Overall themes/clusters across the whole brain** | `brain graphrag search "<q>" --mode global` or `brain graphrag communities` |
| A single entity's neighbourhood ("what links to X") | `brain graphrag entity "X"` (or `--mode local`) |
| Best blend of graph + vector/FTS for one query | `brain graphrag search "<q>" --mode fuse` |
| Not sure — let the router decide | `brain graphrag search "<q>" --mode auto` (default) |

Rule of thumb: **flat answer about content → plain search; relationships,
themes, or clustering → graph.** When in doubt and a person is named, `themes`
is the headline move.

## Prerequisites (check first if a command errors)

GraphRAG is **opt-in and default-off**, and still **experimental**:

- Requires `BRAIN_GRAPH_ENABLED=true` in the environment (defaults to `false`).
- Requires the graph to have been **built** at least once
  (`brain graphrag build --backfill`). `--mode global` and `communities`
  additionally need `brain graphrag communities build`.
- Runs only on the custom **Apache AGE** Postgres image, not the stock
  pgvector image. If AGE is absent the commands exit with a clear
  "Apache AGE is not available" message.
- `brain doctor` reports graph health: an `age` line, a `graph drift` line
  (relational ↔ AGE parity), and a communities line. These are **soft checks** —
  run `brain doctor` if a graph query behaves oddly.

If the graph isn't enabled/built, say so and point the user at
`brain graphrag build --backfill` (and `communities build` for global) rather
than silently falling back — or use `consult-brain` for a plain-search answer.

## The five retrieval modes

`brain graphrag search "<query>" --mode <mode>`:

- **`auto`** *(default)* — heuristic router. A thematic query **with** a
  resolvable person → `themes`; a thematic query **without** a person →
  `global`; otherwise → `local`. Let it pick when intent is fuzzy.
- **`themes`** — *the headline.* "Themes in my conversations with X." Requires
  `--person`. Groups the person's co-occurrence subgraph into ranked theme
  groups (key entities + representative docs).
- **`global`** — community-level retrieval: RRF over the detected community
  clusters (FTS over community summaries ⊕ vector over summary embeddings).
  Needs `communities build` first. Best for "overall themes in my brain."
- **`local`** — entity-centric: resolve the seed entity, traverse its bounded
  `CO_OCCURS` neighbourhood, return the seed + reached entities and their docs.
- **`fuse`** — RRF-merge the local-graph doc leg with the vector/FTS hybrid leg
  into one ranked doc list. Explicit-only (`auto` never routes here). **Note:
  fuse currently works on the default tenant only** — don't pair it with a
  non-default `--tenant`.

## Querying — CLI

Everyday commands (all support `--json` for clean parsing, and `--limit/-n`,
`--depth`, `--tenant`):

```bash
# Themes in conversations with a person (the headline) — synthetic example
brain graphrag themes --person "Jane Doe" --json

# Same thing via search, explicit mode
brain graphrag search "" --mode themes --person "Jane Doe" --json

# Overall themes / clusters across the brain
brain graphrag search "pricing strategy" --mode global --json
brain graphrag communities            # admin view of materialized clusters
brain graphrag communities list --json

# A single entity's neighbourhood ("what connects to X")
brain graphrag entity "Project Atlas" --json
brain graphrag search "Project Atlas" --mode local --depth 2 --json

# Best blended retrieval for one query (default tenant only)
brain graphrag search "team scaling tradeoffs" --mode fuse --json

# Let the router decide
brain graphrag search "how my hiring philosophy changed" --json
```

Useful flags:

| Flag | Purpose |
|---|---|
| `--mode auto\|local\|themes\|global\|fuse` | Retrieval strategy (default `auto`) |
| `--person "<name or email>"` | Required for `themes`; scopes via the directory. Ambiguous/unknown name is a usage error — narrow it down |
| `--depth N` | Traversal/neighbourhood depth (default `BRAIN_GRAPH_DEPTH`) |
| `--limit/-n N` | Max documents returned (default 10) |
| `--tenant T` | Tenant to query (default `BRAIN_GRAPH_TENANT`); avoid pairing a non-default tenant with `--mode fuse` |
| `--json` | Structured output — prefer this for synthesis |
| `--synthesize` | *Opt-in* best-effort per-theme Ollama summary (search/themes only; never required) |

## Querying — MCP (prefer when calling programmatically)

The MCP tools mirror the CLI 1:1 (same params, same JSON wire shape). Prefer
these over shelling out when you're already driving via tools:

- `brain_graphrag_search(query, mode="auto", person=None, depth=None, limit=None, tenant=None, synthesize=False)`
- `brain_graphrag_themes(person, depth=None, limit=None, tenant=None, synthesize=False)` — `person` required
- `brain_graphrag_entity(name, depth=None, limit=None, tenant=None)`
- `brain_graphrag_communities(tenant=None, limit=None)` — list materialized clusters

Admin (setup, not everyday querying):

- `brain_graphrag_build(tenant=None, concepts=False, backfill=False, force=False, limit=None)` — pass `backfill=true` (or `force=true`)
- `brain_graphrag_communities_build(...)` — detect + summarize clusters

Results come back in the `graph_context_json` shape: a `session_id`, `mode`,
`tenant_id`, the ranked `themes` (themes mode) / `entities` (local mode) /
`communities` (global mode), the document hits (`docs`), and an `explanation`.
Raw Cypher is never accepted or returned — the backend injects the tenant and
caps automatically. **Never hand-write Cypher; always go through these
commands/tools.**

## After retrieval — synthesize, grounded

- Use the graph result to organize the answer **by theme / cluster / entity**,
  not as a flat list — that's the whole point of the graph leg.
- For each theme, name the key entities and cite the representative docs by
  title (id-prefix when useful). Read full text with `brain show <id> --json`
  (see `consult-brain`) before quoting.
- Stay grounded. Don't invent relationships the graph didn't surface. If the
  graph is thin for the topic, say so and offer a plain `brain search` instead.
- Graph retrieval surfaces deliberately do **not** auto-log feedback the way
  `brain search` does — if the user reacts to a specific doc, you can still
  `brain rate <id-prefix> useful|irrelevant`.

## Admin / setup (not everyday querying)

Only when the graph needs building or is stale:

```bash
brain graphrag build --backfill        # build the people graph from existing docs
brain graphrag build --backfill --concepts   # also extract topic/project/org concepts
brain graphrag build --force           # authoritative rebuild of the AGE mirror
brain graphrag refresh                 # incremental refresh (assumes build ran)
brain graphrag communities build       # detect + summarize clusters (needed for --mode global)
brain graphrag communities refresh     # force re-detect regardless of dirty gate
```

These are slow, write operations — reach for them only when `brain doctor`
shows drift/missing graph, or the user explicitly asks to (re)build. For
everyday questions, stick to `search` / `themes` / `entity` / `communities`.

## When NOT to use the graph

- Plain "find docs about X", a quote, a single fact, or voice writing →
  `consult-brain`.
- The graph isn't enabled/built (and the user doesn't want to build it) → use
  `consult-brain` for a plain-search answer and mention the graph is off.
- Generic technical/coding/news questions → not what the brain is for at all.
