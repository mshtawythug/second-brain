---
name: brain-graph
description: >
  Graph retrieval over the user's local "second brain" — an entity-centric
  GraphRAG layer (Apache AGE) alongside hybrid search. Use for THEMES,
  PATTERNS, or CONNECTIONS across interactions, or to ENUMERATE the graph
  (people / orgs / projects / topics). For single-doc lookups, quotes, or
  voice writing, use `consult-brain` instead.
  MANDATORY TRIGGERS: themes with, themes in my conversations, patterns
  across, what connects, what keeps coming up, recurring themes, recurring
  topics, how has my thinking evolved, map out my, who is related to, what is
  related to, the bigger picture across, graph of my, cluster my notes,
  overall themes in my brain, what orgs are in my brain, what people are in
  my brain, list all projects, what topics are in my brain, what entities,
  how big is my graph, what's in my graph, graph overview, graph stats.
---

# Brain Graph (GraphRAG)

Use the `brain graphrag` command tree to query the second brain's **entity
graph** — people and concepts as nodes, co-occurrence as edges, plus detected
community clusters. This is a *parallel* retrieval surface to plain
`brain search`; the underlying hybrid FTS+vector index is untouched.

**It's live.** GraphRAG is enabled (`BRAIN_GRAPH_ENABLED=true`), already built,
and queryable **from any working directory** — `brain` loads its config from a
fixed install path, not the cwd, so these commands work from any conversation
without setup. Just run them. (If a command *does* error, see
"[If a command errors](#if-a-command-errors)" at the bottom.)

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
| **Enumerate what's in the graph** ("what orgs/projects/topics are in my brain", "list all projects") | `brain graphrag entities [--type …]` |
| **Size up the graph** ("how big is it / what's in it") | `brain graphrag stats` |
| Best blend of graph + vector/FTS for one query | `brain graphrag search "<q>" --mode fuse` |
| Not sure — let the router decide | `brain graphrag search "<q>" --mode auto` (default) |

Rule of thumb: **flat answer about content → plain search; relationships,
themes, or clustering → graph.** When in doubt and a person is named, `themes`
is the headline move. To answer "what's *in* the brain" rather than "what does
the brain *say*", reach for `entities` / `stats`.

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

### Retrieval (`search` / `themes` / `entity`)

All support `--json` for clean parsing, plus `--limit/-n`, `--depth`, `--tenant`:

```bash
# Themes in conversations with a person (the headline) — synthetic example
brain graphrag themes --person "Jane Doe" --json

# Same thing via search, explicit mode
brain graphrag search "leadership" --mode themes --person "Jane Doe" --json

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

`search` flags:

| Flag | Purpose |
|---|---|
| `--mode auto\|local\|themes\|global\|fuse` | Retrieval strategy (default `auto`) |
| `--person "<name or email>"` | Required for `themes`; scopes via the directory. Ambiguous/unknown name is a usage error — narrow it down |
| `--depth N` | Traversal/neighbourhood depth (default `BRAIN_GRAPH_DEPTH`) |
| `--limit/-n N` | Max documents returned (default 10) |
| `--tenant T` | Tenant to query (default `BRAIN_GRAPH_TENANT`); avoid pairing a non-default tenant with `--mode fuse` |
| `--json` | Structured output — prefer this for synthesis |
| `--synthesize` | *Opt-in* best-effort per-theme Ollama summary (search/themes only; never required) |

### Exploring the graph (`entities` / `stats`)

When the user asks **what's in** the brain (not what it says), enumerate or
size up the graph directly. Both are read-only:

```bash
# What entities exist — enumerate orgs / people / projects / topics / tools
brain graphrag entities --json                       # all types, top 50 by doc_count
brain graphrag entities --type project --json        # just projects ("list all projects")
brain graphrag entities --type org --sort name --limit 0 --json   # every org, A–Z

# At-a-glance graph overview — size + composition + top entities
brain graphrag stats --json
```

`entities` flags — list the tenant's entities (admin view):

| Flag | Purpose |
|---|---|
| `--type org\|project\|tool\|topic\|person` | Filter to one entity type (default: all types) |
| `--sort docs\|name` | `docs` = doc_count DESC (default); `name` = alphabetical |
| `--limit/-n N` | Max entities to show (default 50; **`0` = all**) |
| `--tenant T` | Tenant to list (default `BRAIN_GRAPH_TENANT`) |
| `--json` | Emits `{tenant_id, count, entities:[{entity_type, name, doc_count, description, …}]}` |

`stats` flags — at-a-glance overview:

| Flag | Purpose |
|---|---|
| `--tenant T` | Tenant to summarize (default `BRAIN_GRAPH_TENANT`) |
| `--json` | Emits `{tenant_id, counts_by_type, total_entities, total_relationships, total_communities, top_entities:[…]}` |

Use `stats` first to gauge whether the graph is rich enough to answer a thematic
question, and `entities --type <t>` to give the user a concrete inventory ("here
are the projects in your brain: …").

## Querying — MCP (prefer when calling programmatically)

The MCP tools mirror the CLI 1:1 (same params, same JSON wire shape). Prefer
these over shelling out when you're already driving via tools:

- `brain_graphrag_search(query, mode="auto", person=None, depth=None, limit=None, tenant=None, synthesize=False)` — the five-mode retrieval surface
- `brain_graphrag_themes(person, depth=None, limit=None, tenant=None, synthesize=False)` — "themes with X"; `person` required
- `brain_graphrag_entity(name, depth=None, limit=None, tenant=None)` — one entity's neighbourhood
- `brain_graphrag_entities(entity_type=None, sort="docs", limit=50, tenant=None)` — **enumerate** the graph's entities (what orgs/people/projects/topics/tools exist; "list all projects" → `entity_type="project"`); returns `{tenant_id, count, entities:[…]}`
- `brain_graphrag_stats(tenant=None)` — **graph overview** (how big / what's in it); returns `{tenant_id, counts_by_type, total_entities, total_relationships, total_communities, top_entities:[…]}`
- `brain_graphrag_communities(tenant=None, limit=None)` — list materialized clusters

> Note: the MCP param is `entity_type` (not `type`) — `type` is a Python
> builtin. Everything else maps to the CLI flag names.

Admin (setup, not everyday querying):

- `brain_graphrag_build(tenant=None, concepts=False, backfill=False, force=False, limit=None)` — pass `backfill=true` (or `force=true`)
- `brain_graphrag_refresh(tenant=None)` — recompute a tenant's aggregate edges (no re-resolve)
- `brain_graphrag_communities_build(...)` — detect + summarize clusters
- `brain_graphrag_communities_refresh(tenant=None, limit=None)` — force re-detect + re-summarize past the dirty gate

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
- For `entities` / `stats`, present an actual inventory — group by type, lead
  with the highest `doc_count` entities, and report the totals plainly.
- Stay grounded. Don't invent relationships the graph didn't surface. If the
  graph is thin for the topic, say so and offer a plain `brain search` instead.
- Graph retrieval surfaces deliberately do **not** auto-log feedback the way
  `brain search` does — if the user reacts to a specific doc, you can still
  `brain rate <id-prefix> useful|irrelevant`.

## Admin / setup (not everyday querying)

The graph is already built, so you rarely need these. Reach for them only when
`brain doctor` shows drift/missing graph, or the user explicitly asks to
(re)build. These are slow write operations:

```bash
brain graphrag build --backfill        # build the people graph from existing docs
brain graphrag build --backfill --concepts   # also extract topic/project/org concepts
brain graphrag build --force           # authoritative rebuild of the AGE mirror
brain graphrag refresh                 # recompute a tenant's aggregate edges
brain graphrag communities build       # detect + summarize clusters (needed for --mode global)
brain graphrag communities refresh     # force re-detect regardless of dirty gate
```

For everyday questions, stick to `search` / `themes` / `entity` / `entities` /
`stats` / `communities`.

## If a command errors

The graph is enabled and built, so these are **fallbacks**, not preconditions —
only relevant if a `brain graphrag` command actually fails:

- **"Apache AGE is not available"** — the running Postgres isn't the custom
  Apache AGE image. The graph commands need it; plain `brain search`
  (`consult-brain`) still works regardless.
- **Empty/thin results or "graph not built"** — the tenant's graph may be
  missing or stale. Run `brain doctor` (it reports an `age` line, a `graph
  drift` line for relational ↔ AGE parity, and a communities line — all soft
  checks). Rebuild with `brain graphrag build --backfill` (and
  `communities build` for `--mode global`) only if the user wants it.
- **`--mode global` returns nothing** — communities haven't been built; run
  `brain graphrag communities build` first, or use another mode.

When the graph can't answer, fall back to `consult-brain` for a plain-search
answer rather than guessing — and tell the user the graph leg was unavailable.

## When NOT to use the graph

- Plain "find docs about X", a quote, a single fact, or voice writing →
  `consult-brain`.
- The graph errors and the user doesn't want to rebuild → use `consult-brain`
  for a plain-search answer and mention the graph leg was unavailable.
- Generic technical/coding/news questions → not what the brain is for at all.
