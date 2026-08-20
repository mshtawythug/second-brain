# GraphRAG for second-brain — Design Spec (v5, Apache AGE)

**Date:** 2026-05-20 (v5 pivot: 2026-05-21)
**Status:** DRAFT v5.1 — **architecture pivot to Apache AGE**; Codex v5 validation incorporated; pending final Codex confirm + user approval
**Build status:** ✅ **G0–G4 IMPLEMENTED (2026-05-22, branch `graphrag`, uncommitted)** — all five waves (G0 foundations / G1 people aspect / G2 concepts + local/themes retrieval / G3 communities + global / G4 feedback + fuse + eval/DRY) complete; perf gates passed (local ≤ 750 ms · themes ≤ 2 s · global ≤ 1000 ms · community-build ≤ 5 min · fuse ≤ 1500 ms — measured P95 550 ms on the default-tenant slice, gate added in the G4 review wave); ruff/mypy clean. Decisions recorded in §17b (G2) / §17c (G3) / §17d (G4). **Known post-merge tech-debt:** `src/brain/cli.py` and `src/brain/mcp_server.py` now exceed the 800-line guideline; the file-size split was deliberately deferred to after merge.
**Author:** Claude (team `graphrag-build`) with Codex review
**Branch:** `graphrag` (no merge to master / no push without explicit user permission)

> **For agentic workers:** REQUIRED SUB-SKILL: Use `team-driven-development` to implement the plan; `team-parallel-dispatch` only for genuinely disjoint slices. Implementer/reviewer teammates run on **opus**.

> **Revision history:** v1→v4 designed a *single-user, local* GraphRAG on **plain relational tables + bounded Python BFS, no Apache AGE** (Codex-approved READY TO PLAN after 5 rounds; see §18 historical logs). **v5 pivots the storage/traversal engine to Apache AGE** because the product goal changed: this must be a **real graph database** usable by **other people at much larger, multi-user scale**, while staying in **one Postgres**. Codex (2026-05-21) recommended **AGE inside Postgres with a swappable `GraphBackend` abstraction + hard performance gates** (kill-switch to Neo4j/Memgraph if AGE misses scale). v5 keeps the v4 *correctness* model (relational source-of-truth + LazyGraphRAG-style indexing + scope-first "themes with X") and changes the *engine* (AGE vertices/edges + Cypher) and adds **multi-tenancy** + **ops**.

---

## 1. Goal & motivating use case

A **Graph RAG** retrieval mode alongside the existing **vector/hybrid search**, so an LLM (via the `brain` CLI and the MCP server) can answer *thematic/relational* questions, on a **real graph database** that scales to **multiple users / larger corpora**.

| Question shape | Tool | Example |
|---|---|---|
| Pin-point lookup | existing FTS+vector RRF | "find the call where we discussed pricing" |
| Thematic / relational | **Graph RAG (AGE)** | **"themes in my conversations with X"**, "how has my thinking on Y evolved", "who connects to this deal", multi-hop "how are A and B related" |

**Headline use case (graded against this):** *"What are the themes in my conversations with X?"* — an **entity-scoped thematic query**: scope to person X, build/traverse X's co-occurrence subgraph, return ranked theme groups with key entities + representative X-docs (+ optional on-demand summary).

Both modes are concurrent and exposed at **parity** on CLI + MCP. Graph storage/traversal is a **real graph engine (Apache AGE / openCypher)** so multi-hop traversal, larger graphs, and multi-tenant deployments are first-class.

---

## 2. Constraints

**Product (new in v5):**
1. **Real graph database** — openCypher graph engine, not hand-rolled traversal.
2. **One Postgres** — graph lives in the same Postgres via **Apache AGE** (no separate Neo4j in the default deployment).
3. **Multi-user / multi-tenant** — `tenant_id` isolation throughout; safe for shared/larger-scale use.
4. **Scale with guardrails** — explicit performance budgets + benchmark gates; a **swappable `GraphBackend`** so AGE can be replaced by Neo4j/Memgraph if it misses targets (kill-switch), without rewriting callers.

**Carried over (hard):**
5. **Both modes concurrent**; vector RAG untouched. 6. **CLI + MCP parity** for retrieval. 7. **Local models** (Ollama extraction/summaries; pluggable `Embedder`). 8. **Continuous ingest → correct, cheap incremental** (edits/deletes reconcile; aggregates recomputed from per-doc source-of-truth). 9. **Build on existing infra** (people pipeline, enricher, embedder, hybrid search). 10. **Repo conventions:** UUID app IDs, parameterized SQL (+ parameterized Cypher), additive migrations, no destructive prod-DB ops, `ruff` + `mypy --strict`, ≥85% coverage (95% pure logic), regression test per bug, **no PII in code/tests**.

---

## 3. What already exists (reuse map)
Unchanged from v4 except where noted. Reused: `directory_entries` + `_participant_keys` + `aggregate_people()`/`resolve_person_to_keys()` (person entities for free); `OllamaEnricher` (extend `extract_entities()`/`summarize_group()`); `hybrid_search()` + `SearchResult` (doc-hit portion of results); `Embedder`/`make_embedder` (embeddings in pgvector **side tables**, not AGE properties); `interactions` (G4 schema change); `eval/` (callable refactor); the dim-reconciliation generalization built in the paused `g0b` (**kept** — embeddings stay relational). The v4 `vault/graph.py` doc-link graph is unrelated and untouched.

---

## 4. Architecture decisions

### D1 — Storage/traversal: **Apache AGE inside Postgres** (openCypher), behind a swappable backend.
The graph (entity/document vertices, mention + co-occurrence edges) lives in an **AGE graph** in the same Postgres; traversal is **Cypher**. A `GraphBackend` Protocol abstracts it so AGE is the default impl and Neo4j/Memgraph remain a drop-in escape hatch.
- **Why:** Product now needs a *real* graph DB at multi-user scale in *one* Postgres. AGE is GA (PG16/17/18), gives openCypher, ACID, BTREE/GIN on properties, and mixed SQL+Cypher in one statement — satisfying "real graph DB + one DB" without a second datastore.
- **Rejected:** plain tables + Python BFS (v4 — not a real graph engine; keeps graph semantics in app code; was right only for single-user local); dedicated Neo4j/Memgraph (second store + cross-store consistency — kept as the abstraction's kill-switch, not the default).
- **Guardrail:** AGE perf is *not* assumed — hard depth/degree/frontier caps + property indexes + **benchmark gates before G2 acceptance** (a LightRAG report showed minutes-long retrieval on a few-thousand-node AGE graph; we measure, not hope).

### D2 — Indexing approach: **LazyGraphRAG-style** (unchanged). Cheap/incremental at ingest; expensive global work (communities) batched/lazy in G3. No full MS GraphRAG.

### D3 — Entities: **people for free; concepts via the gated, validated, versioned Ollama extractor** (unchanged from v4 D3). Default backend `OllamaExtractor` (`enrichment.extract_entities()`), zero new dep, default-OFF until an eval gate passes. (Note: AGE's `ai_extension extract()` is Azure-only — we do NOT depend on it; local Ollama only.)

### D4 — Edges: **window co-occurrence over raw text, raw counts in a relational source-of-truth, aggregated into AGE `CO_OCCURS`.**
- Raw per-doc co-occurrence (over raw text, chunker-independent, capped, normalized-**lift** weighted, generic-suppressed at derive time) → relational `graph_edge_contributions` (source of truth, +`tenant_id`).
- **Co-occurrence window (normative):** the window is an **inclusive radius** — a pair co-occurs iff `|pos_i − pos_j| ≤ window` — and is **unit-agnostic**: `pos` is whatever ordinal the aspect assigns (a token/word index for raw-text concepts; a single shared notional position `0` for participant-derived people, so any `window ≥ 1` yields the complete graph over a document's persons).
- Aggregate `(:Entity)-[:CO_OCCURS {weight, co_count, doc_count, tenant_id}]-(:Entity)` edges are **recomputed from contributions** and materialized into AGE (batch refresh). Per-doc contributions are **not** stored as graph edges (Codex r2/v5).
- **Edge-weight convention (normative):** single metric **normalized lift ∈ (0,1]** (stored on `CO_OCCURS.weight` + relational mirror), used for pruning, Cypher traversal scoring, and theme ranking. (PMI rejected — can be negative.) **Formula (normative):** `normalized_lift(A, B) = co_df(A, B) / min(df(A), df(B))` ∈ (0,1], where `df(X)` is X's document frequency and `co_df(A, B)` the count of documents in which A and B co-occur (the corpus size `N` cancels); it is `1.0` exactly when one entity's document set is a subset of the other's.

### D5 — Communities (G3, not v1): networkx Louvain over the entity graph + Jaccard stable-identity + delta-gated dirty + lazy embedded summaries. AGE does not remove this need.

### D6 — Brain is a **retriever, not an answerer** (unchanged). Returns `GraphContext` (themes/entities/docs/evidence); the LLM synthesizes. Optional `--synthesize` local-Ollama map-reduce for human CLI use.

### D7 — Both modes concurrent + **auto-router** (heuristic) + explicit `mode`. `hybrid_search` untouched. `fuse` deferred to G4.

### D8 — Distinct **`GraphContext`** wire shape (not fake `SearchResult` parity). `docs[]` may reuse `SearchResult`.

### D9 — **Multi-tenancy (new).** `tenant_id` on every relational source-of-truth row, every AGE vertex/edge property, and every query. One AGE graph with **tenant-scoped properties + indexes** is the default; **strict isolation uses a separate database/graph per tenant**. CLI/MCP **never** accept raw Cypher — only parameterized, tenant-scoped generated queries. Single-user local deployments use a fixed default `tenant_id` (e.g. `"default"`), so the local experience is unchanged.

### D10 — **`GraphBackend` abstraction + performance gates (new).** A narrow Protocol (`upsert_entities`, `upsert_mention_edges`, `refresh_cooccur_edges`, `traverse(seed, depth, caps)`, `scope_person`, `drop_graph`, `bootstrap`) with an **AGE implementation** (Cypher) as default. Hard caps (`BRAIN_GRAPH_DEPTH`, max degree, frontier cap), parameterized Cypher only, and a **benchmark fixture** (synthetic N-node/E-edge graph) that must meet P95 latency budgets before G2 is accepted. If AGE fails, implement a Neo4j/Memgraph backend behind the same Protocol — callers unchanged.

---

## 5. Schema & storage (migration `012_graphrag.sql` + AGE bootstrap)

**Two layers, one Postgres:**

**(a) Relational source-of-truth + embeddings (pgvector side tables)** — additive tables, all with `tenant_id TEXT NOT NULL DEFAULT 'default'`, UUID app IDs, `CHECK (src_id < dst_id)` canonicalization, FKs `ON DELETE CASCADE`. **`tenant_id` is part of every PK/unique and is indexed** (every key below is tenant-scoped). **Migration note:** the paused `g0a` created an uncommitted, pre-tenant/pre-AGE `migrations/012_graphrag.sql`; since it was never committed/shipped, v5.1 **rewrites `012_graphrag.sql` in place** to this tenantized schema (no separate migration). The AGE graph itself is created by an idempotent init bootstrap, not a frozen `.sql` migration (see below).
- `graph_entities(id UUID pk, tenant_id, entity_type CHECK, name, canonical_key, description, embedding vector(1024) NULL, doc_count, properties JSONB, ts…)` · `UNIQUE(tenant_id, entity_type, canonical_key)`; index `(tenant_id, entity_type)`. **Durable app identity lives here** (AGE vertex IDs are not the app identity).
- `graph_entity_mentions(tenant_id, entity_id, document_id, mention_count, source, PK(tenant_id, entity_id, document_id))` — source of truth; index `(tenant_id, document_id)`.
- `graph_edge_contributions(tenant_id, document_id, src_id, dst_id, cooccur_count, PK(tenant_id, document_id, src_id, dst_id), CHECK src_id<dst_id)` — raw, source of truth; indexes `(tenant_id, src_id)`, `(tenant_id, dst_id)`.
- `graph_relationships(tenant_id, src_id, dst_id, rel_type, weight, co_count, doc_count, updated_at, PK(tenant_id, src_id, dst_id, rel_type), CHECK src_id<dst_id)` — **derived aggregate mirror** (normalized lift), recomputed from contributions; this is the SQL counterpart of the AGE `CO_OCCURS` edges (kept in sync by the same refresh) and backs evidence/ranking; indexes `(tenant_id, src_id)`, `(tenant_id, dst_id)`.
- `graph_index_state(tenant_id, document_id, aspect, content_hash, inputs_hash, extractor_ver, suppress_ver, indexed_at, PK(tenant_id, document_id, aspect))` — per-aspect incremental watermark.
- (G3) `graph_communities` + `graph_community_members` (tenant-scoped); (G4) `interactions` change.
- Embeddings stay in these relational columns (pgvector), **not** AGE vertex properties — joined by `entity_id`/`document_id`. The `g0b` generalized dim-reconciliation (`ensure_embedding_column(table,column)` allowlisted via `psycopg.sql.Identifier`) applies to `graph_entities.embedding`; chunks semantics preserved (regression-tested); graph cols nullable, no HNSW initially.

**(b) AGE graph** (`brain_graph`), created idempotently at init:
- Vertices: `(:Entity {tenant_id, entity_uuid, entity_type, canonical_key, name})`, optional `(:Document {tenant_id, document_uuid, content_type, sent_at})`.
- Edges: `(:Entity)-[:MENTIONED_IN {tenant_id, mention_count, source}]->(:Document)`; `(:Entity)-[:CO_OCCURS {tenant_id, rel_type, weight, co_count, doc_count}]-(:Entity)` (aggregate).
- Property indexes (BTREE/GIN) on `tenant_id`, `entity_uuid`, `canonical_key`, `CO_OCCURS.weight`.
- AGE IDs are internal; all joins back to relational use `entity_uuid`/`document_uuid`.

**Migration/bootstrap (`db.py`):** `brain init` runs SQL migrations then bootstraps AGE idempotently, **in autocommit / with explicit commits** (AGE catalog DDL doesn't play well inside an open transaction with psycopg v3): `CREATE EXTENSION IF NOT EXISTS vector; … pgcrypto; … age CASCADE; LOAD 'age';` then **check `ag_catalog.ag_graph` and only `SELECT create_graph('brain_graph')` if absent**. **The init bootstrap creates the GRAPH only** — vertex/edge **labels + property indexes are owned by G0-4** (the `GraphBackend` creates them alongside its entity/edge upserts; an AGE property index targets a label's backing table, which cannot exist before the label is created, so they cannot be pre-created here). All AGE calls are **fully-qualified `ag_catalog.*`** (`ag_catalog.cypher(...) AS (… ag_catalog.agtype)` / `ag_catalog.create_graph` — proven on live AGE), so the bootstrap sets **no global `search_path`** and never leaks `ag_catalog` onto the session, keeping unqualified `public` DDL/queries correct (matches `conftest._reset_age_graph`). Bootstrap `psycopg.Error`s are wrapped in `AgeBootstrapError` (a `BrainError`). Migrations stay pure SQL where possible; AGE graph creation lives in this idempotent bootstrap step invoked by `init` (not a frozen `.sql` migration, since `create_graph` is a function call) — documented clearly.

---

## 6. Retrieval (Cypher + SQL; tenant-scoped; bounded)

All queries inject `tenant_id` and hard caps (`BRAIN_GRAPH_DEPTH` default 2, max degree, frontier cap). **No raw user Cypher** — only parameterized generated queries via the backend.

### 6a. Local (entity-centric) — AGE traversal, Python-side scoring
`scope_person`/seed resolves query → `Entity` vertices (by `canonical_key`/name or vector pre-match in the relational catalog). Then a **bounded variable-length AGE traversal that returns the path's relationships**, with affinity computed **in Python** (do NOT rely on Cypher `reduce()`/`all()` — not reliably supported in AGE):
```cypher
-- depth is validated then baked into a fixed template (e.g. *1..2); NOT a bound var.
-- tenant_id is enforced on the seed, EVERY intermediate node, AND every relationship.
MATCH p = (s:Entity)-[:CO_OCCURS*1..2]-(n:Entity)
WHERE s.entity_uuid = $seed
  AND s.tenant_id = $t AND n.tenant_id = $t
  AND all_same_tenant(nodes(p), $t)            -- enforced via per-element WHERE (generated)
RETURN n.entity_uuid AS eid,
       [r IN relationships(p) | r.weight] AS edge_weights,
       length(p) AS hops
LIMIT $frontier_cap;
```
The backend **generates** the concrete query: a fixed `*1..N` template per validated depth; an explicit per-element tenant predicate over `nodes(p)`/`relationships(p)` (since `all()` may be unavailable, generate `AND n1.tenant_id=$t AND ... AND r1.weight>=$min_w ...` or post-filter returned paths in Python); and AGE-correct parameter passing (AGE Cypher params go through the `cypher()` agtype parameter map / prepared statements, **not** ordinary psycopg `%s` placeholders inside the Cypher string). Python computes `path_affinity = ∏ edge_weights` (seed 1.0), keeps the best affinity per `eid`, applies the frontier cap, and records `hops` of the winning path. **This whole query shape is an AGE canary in G0** — validated against a live AGE instance before G2 depends on it; if AGE can't express bounded variable-length traversal acceptably, the `GraphBackend` kill-switch applies. Reached entities → docs via `graph_entity_mentions` (SQL) → snippets via existing search path.

### 6b. Themes with X — scope-first (headline)
1. **Cypher scope:** person X → `MENTIONED_IN` → Documents → co-mentioned `Entity` vertices (tenant-scoped, exclude X/owner/generic).
2. **SQL lift:** compute in-scope normalized lift from `graph_edge_contributions` restricted to X's documents (absolute `generic_df_cap = round(GENERIC_DF × tenant_corpus_N)`).
3. **Group** the small scoped subgraph (lift threshold + bridge guard + connected components; acceptance fixture; networkx pulled forward only if it fails).
4. Return ranked `ThemeGroup`s (key entities + representative X-docs + snippets; optional on-demand `summarize_group`).
Cypher provides scoping/expansion; SQL provides weighting/evidence; Python shapes `GraphContext` (not the engine).

### 6c. Global (G3): community summaries by vector+FTS (guarded on non-null embedding).
### 6d. Auto-router: pure heuristic (thematic keywords + resolvable person ⇒ themes; thematic alone ⇒ global; else local). `mode=` overrides.

---

## 7. Incremental reconcile (correct under AGE)

`reconcile_document(conn, tenant_id, doc_id)` (centralized in `graph_rag/reconcile.py`), called from **every** write path (post-ingest hook, `update_document` `ingest:1352`, gmail upsert, `cli rm` `cli:2588`, `watch:757`, `sync:1049/1281`, force-reingest `ingest:733`):
1. Per-aspect skip if `content_hash`+`inputs_hash`+`extractor_ver`+`suppress_ver` unchanged.
2. **Relational rewrite (source of truth):** `DELETE … WHERE document_id=D` then re-insert mentions + raw contributions (people always; concepts if `BRAIN_GRAPH_CONCEPTS`).
3. **AGE sync (split MERGE — AGE `MERGE` is all-or-nothing on a full pattern):** first `MERGE` each `Entity`/`Document` **vertex** on its own (by `tenant_id`+`entity_uuid`/`document_uuid`); then in a separate step `MATCH` the two endpoints and `MERGE`/delete-create the `MENTIONED_IN` **edge**. Never `MERGE` a whole `(a)-[e]->(b)` pattern in one clause (it would re-create vertices). Delete + recreate this doc's `MENTIONED_IN` edges.
4. **Aggregate refresh:** recompute `graph_relationships` mirror + `CO_OCCURS` edges from contributions (batched/debounced; full recompute is correct because aggregates derive from source-of-truth — cascade-safe). `DETACH DELETE` zero-mention `Entity` vertices; GC catalog rows with `doc_count=0`.
5. `UPSERT graph_index_state`.

`remove_document` (all delete paths): relational `ON DELETE CASCADE` clears source rows; AGE `MATCH (:Document {document_uuid:$d}) DETACH DELETE`; then aggregate refresh. `doctor` runs a drift check (relational vs AGE vs recomputed).

**Two-command rebuild model (implemented; G1).** The corpus has two distinct maintenance commands, *not* one:
- **`brain graphrag build --force`** = **authoritative full rebuild** from the relational source-of-truth. It bypasses the per-aspect `graph_index_state` watermark and re-reconciles **every** document — rebuilding entity vertices, `MENTIONED_IN` edges, `Document` vertices, **and** `CO_OCCURS` edges — then rewrites each watermark to current. This is the recovery path for a **dropped or corrupted AGE mirror** while documents + config are unchanged (a plain `--backfill` would watermark-skip every doc and recover nothing). A force build first restores all of the tenant's `Entity` vertices in one idempotent pre-pass so the per-doc full-tenant `CO_OCCURS` rematerialization always finds its endpoints.
- **`brain graphrag refresh`** = **corpus-wide weight/edge recompute** *without* re-resolving any document's persons: it rebuilds `graph_relationships` (normalized lift + generic suppression) from `graph_edge_contributions` and rematerializes `CO_OCCURS`. It is the response to a corpus-wide weighting/suppression change (a new `suppress_ver` / `BRAIN_GRAPH_GENERIC_DF`). Refresh **assumes the tenant's `Entity` vertices already exist** (it raises if one is missing), so for a dropped mirror use `build --force` instead.

**AGE session bootstrap:** every connection that touches the graph runs `LOAD 'age'` and then issues **fully-qualified `ag_catalog.cypher(...) AS (… ag_catalog.agtype)`** — it sets **no global `search_path`** (proven on live AGE; avoids leaking `ag_catalog` onto the session and breaking unqualified `public` queries), with psycopg-v3 autocommit handled in `db.connect`/`db.connect_age`; the helper wraps this (`db.load_age`) so callers don't repeat it.

---

## 8. Module layout
```
src/brain/graph_rag/
  __init__.py        — public API + EntityExtractor & GraphBackend Protocols
  schema.py          — dataclasses (GraphEntity, EntityMention, EdgeContribution, Edge,
                       ThemeGroup, GraphContext, GraphExplanation) + tenant_id
  backends/
    base.py          — GraphBackend Protocol (upsert_entities, upsert_mention_edges,
                       refresh_cooccur_edges, traverse, scope_person, drop_graph, bootstrap)
    age.py           — AGE backend: parameterized Cypher, session bootstrap, graph/label init
    (neo4j.py)       — escape-hatch stub (only if AGE misses perf gates)
  extract.py         — OllamaExtractor (validated/versioned/chunked) + canonicalization
  cooccur.py         — raw-text window co-occurrence (chunker-independent) + caps
  weighting.py       — normalized lift + generic suppression (derive-time, versioned)
  reconcile.py       — reconcile_document/remove_document/refresh_aggregates (relational + backend)
  retrieve.py        — graph_rag_search(): local/themes(+global G3) via backend + SQL
  grouping.py        — scoped-subgraph grouping (lift threshold + bridge guard + CC)
  router.py          — pure heuristic router
  tenancy.py         — tenant resolution/scoping helpers
# extended: enrichment.py, db.py (AGE bootstrap + dim refactor), config.py, cli.py,
#   mcp_server.py, ingest/__init__.py, interactions.py (G4), eval/runner.py (G4),
#   migrations/012_graphrag.sql, docker image + compose.
```

## 9. Surfaces — CLI + MCP parity (no raw Cypher)
New `brain graphrag` group (leave `brain graph` export intact). Retrieval = full CLI↔MCP parity; admin/index ops CLI-primary. **Never** expose raw Cypher; all tools take structured params and inject `tenant_id` + caps.

**Implemented in G1** (admin/index ops, CLI-only this wave — no graphrag MCP yet):
- `brain graphrag build [--backfill] [--force] [--tenant T] [--limit N]` — person-aspect backfill (`--backfill`) or authoritative clear-then-rebuild (`--force`; mutually exclusive with `--limit`). Concept indexing arrives in G2.
- `brain graphrag refresh [--tenant T]` — corpus-wide weight/edge recompute.

**Planned — by wave:**
- **G2:** `--concepts` flag on `build`; `brain graphrag search "<q>" [--mode auto|local|themes|global] [--person X] [--depth N] [--limit N] [--tenant T] [--json] [--synthesize]` ↔ `brain_graphrag_search(...)`; `brain graphrag themes --person "X" [--tenant T] …` ↔ `brain_graphrag_themes(...)`; `brain graphrag entity "<name>"` ↔ `brain_graphrag_entity(...)`; the `brain_graphrag_build` MCP; `brain doctor` graph drift/entity-edge-count checks (the G0 doctor already reports AGE version + graph presence + session bootstrap).
- **G3:** `communities`.

> Note: `reembed` is **not** a `graphrag` command. `brain reembed` is the existing embeddings-backfill command (`chunks.embedding`), unrelated to the graph mirror; an earlier draft listed it here in error.

## 10. Config (`config.py`, ConfigError-validated)

**Implemented in G1** (the only `BRAIN_GRAPH_*` knobs `config.py` currently parses + validates; documented in `.env.example` + `src/brain/templates/env.example`):
- `BRAIN_GRAPH_ENABLED` (=`false`) — opt-in people-aspect sync hook.
- `BRAIN_GRAPH_TENANT` (=`default`) — tenant id on every graph row/vertex/edge/query.
- `BRAIN_GRAPH_COOCCUR_WINDOW` (=`3`) — inclusive co-occurrence radius.
- `BRAIN_GRAPH_MAX_ENTITIES_PER_DOC` (=`40`) — per-doc entity cap (`none`/`unlimited` disables).
- `BRAIN_GRAPH_GENERIC_DF` (=`0.30`) — generic-entity document-frequency suppression ratio.

**Planned (not yet parsed by `config.py`) — by wave:**
- `BRAIN_GRAPH_CONCEPTS` (=`false`), `BRAIN_GRAPH_EXTRACT_MODEL` — **G2** (concept aspect + Ollama extractor).
- `BRAIN_GRAPH_DEPTH` (=`2`), `BRAIN_GRAPH_FRONTIER_CAP` (=`200`), `BRAIN_GRAPH_MAX_DEGREE` (=cap), `BRAIN_GRAPH_MIN_EDGE_WEIGHT`, `BRAIN_GRAPH_THEME_LIMIT` (=`5`) — **G2** (bounded traversal / scope-first themes; the caps are passed explicitly to the backend today, not yet env-configurable).
- `BRAIN_GRAPH_BACKEND` (=`age`) — **G4** (Neo4j/Memgraph kill-switch only; AGE is the hardcoded default backend through G1–G3).
- `BRAIN_GRAPH_NAME` (=`brain_graph`) — the AGE graph name is currently the `db.DEFAULT_GRAPH_NAME` constant, **not yet** an env knob; promote to config if/when a second graph is needed.

## 11. Ops & deployment
- **Custom Docker image** pinned: PG16 + `vector` + `pgcrypto` + `age` (stock `pgvector/pgvector` lacks AGE; `apache/age` lacks our pgvector setup → build a combined image; document the Dockerfile + `shared_preload_libraries=age` if required by the AGE build). Update `docker-compose` + templates + installer.
- `brain init` AGE bootstrap (idempotent) + `doctor` AGE checks.
- **Test harness:** `tests/conftest.py` reset must, **in autocommit**, `LOAD 'age'` and `SELECT drop_graph('brain_graph', true)` if the graph exists (then the init bootstrap re-creates it) — in addition to the existing `DROP SCHEMA public` dance, because `ag_catalog` + the graph namespace **survive** the public-schema drop. Any `ag_catalog` `search_path` it sets is scoped to those AGE statements and reset immediately (no global `ag_catalog` leak onto the session, so the subsequent `run_migrations` DDL still targets `public`). Add an AGE-aware reset fixture; keep the live-DB markers.

## 12. Phased roadmap (AGE-rewritten; dependency-ordered)
| Wave | Scope | New |
|---|---|---|
| **G0 — AGE foundation** | custom image + compose; AGE bootstrap in `db.py`/`brain init` (**graph only** — vertex/edge labels + property indexes are created by the GraphBackend, not the init bootstrap) + session bootstrap in `connect`; mig 012 relational source-of-truth tables (+tenant_id); `GraphBackend` Protocol + AGE backend (vertex/edge MERGE, **label/index init [owns labels + property indexes, deferred from G0-2]**, `drop_graph`); generalized dim reconciliation (g0b, kept); conftest AGE reset; `doctor` AGE line | custom image, AGE |
| **G1 — Person graph + reconcile** | `reconcile.py` (person aspect) relational + AGE sync; person import + person-person contributions; `cooccur.py`/`weighting.py`; wire into all write/delete paths; `brain graphrag build --backfill` (person-only) + `refresh`; integration tests (edit/delete/idempotency/batched≡full); **benchmark fixture** | tenancy, reconcile |
| **G2 — Concept extraction + retrieval + surfaces + eval (HEADLINE)** | `EntityExtractor`+`OllamaExtractor` (gated); concept aspect; Cypher local traversal + scope-first themes + grouping + router → `GraphContext`; CLI `brain graphrag …` + MCP `brain_graphrag_*` (parity, no raw Cypher); eval fixtures + extractor gate; **P95 latency gate must pass** | headline |
| **G3 — Global communities** | mig 013; networkx Louvain + stable identity + lazy embedded summaries; global mode + router; communities CLI/MCP | networkx |
| **G4 — Polish** | mig 014 interactions (nullable doc_id + target_type/target_id + graph_retrieved); logging; fuse mode; `run_eval` callable + graph eval category + baseline; docs/memory; (optional) Neo4j backend if perf gate failed | — |

Each wave: `ruff`+`mypy --strict`+`pytest` green, Codex review of the diff, CLAUDE.md review+audit loop. **G2 is gated on the AGE benchmark** meeting P95 budgets; if it fails, trigger the kill-switch (Neo4j backend) before proceeding.

## 13. Testing & benchmarks
v4 test plan carries over (router, co-occurrence raw-text + overlap regression, generic suppression, grouping acceptance fixture, dim reconciliation chunks-preserved, edit/delete/idempotency/batched≡full reconcile — now exercising the AGE backend on the test DB). **Add:** AGE session-bootstrap tests; tenant-isolation tests (queries never cross tenants); a **performance benchmark** (synthetic graph, P95 traversal/themes latency budget) that gates G2; conftest AGE-reset test.

## 14. Dependencies
- **Runtime:** Apache AGE (DB extension, via the custom image — not a pip dep); networkx (G3, pure-Python). pgvector/psycopg/tiktoken reused.
- **NOT added:** torch/transformers/GLiNER/spaCy; Azure `ai_extension` (Azure-only — we use local Ollama); Neo4j (escape-hatch only).

## 15. Out of scope (YAGNI)
Full MS GraphRAG; LLM relation-triple extraction; Azure ai_extension/DiskANN; hierarchical multi-level communities (G3 level 0); PPR; in-brain map-reduce by default; raw Cypher exposure; cross-tenant graphs; incremental affected-refresh (full refresh in v1); `fuse` before G4; Neo4j backend unless AGE fails the perf gate.

## 16. Risks & kill-switch
| Risk | Likelihood | Mitigation |
|---|---|---|
| **AGE traversal too slow at scale** (LightRAG saw minutes on ~3.5k nodes) | **Med-High** | hard depth/degree/frontier caps + property indexes + **P95 benchmark gate before G2** (synthetic graph: 50k entities / 500k `CO_OCCURS` / 1M mentions / 10 tenants; **P95 local traversal ≤ 750 ms, P95 themes-with-X ≤ 2 s**); `GraphBackend` kill-switch → Neo4j/Memgraph if missed |
| AGE ops friction (LOAD/search_path/psycopg autocommit) | Med | backend wraps session bootstrap; init/doctor verify; documented |
| Custom image maintenance | Med | pinned versions; CI builds the image; documented Dockerfile |
| Tenant data leakage | High-impact | tenant_id on every row/vertex/edge + every query; no raw Cypher; isolation tests; separate-DB option for strict isolation |
| AGE catalog survives test reset → flaky tests | Med | conftest drop_graph/extension reset fixture |
| Aggregate drift on edit/delete | Low (by design) | recompute from source-of-truth; cascade-safe; doctor drift check |
| Small-model extraction unreliability | Med | strict validation + versioning + default-OFF until eval gate; Protocol swap |

## 17. Resolved decisions (Codex v5 validation, 2026-05-21)
1. **Tenancy model:** **one tenant-scoped AGE graph** for G0/G2 (mandatory all-node/all-edge tenant filtering + property indexes + isolation tests + perf gate). Graph-per-tenant or separate DB **only** for strict isolation or if the tenant-filter perf gate fails.
2. **`CO_OCCURS`:** **eagerly materialize** the aggregate `CO_OCCURS` edges in AGE (for traversal/local expansion); compute **scoped theme lift from the relational `graph_edge_contributions`/`graph_relationships`** for evidence + ranking.
3. **Perf gate (G2):** synthetic graph of **50k entities / 500k `CO_OCCURS` / 1M mentions / 10 tenants**; **P95 local traversal ≤ 750 ms, P95 themes-with-X ≤ 2 s**. Must pass before G2 acceptance.
4. **Document vertices:** **include `(:Document)` vertices + `MENTIONED_IN` edges** in AGE (richer Cypher scoping); keep document text/evidence relational.
5. **Theme membership:** **exclude seed X and owner**; default theme groups are **topics/projects/orgs/tools**; other people appear only as supporting/bridge entities, not as themes themselves (configurable).

## 17b. G2 Resolved Decisions (Codex-ruled)

Codex-ruled answers (read-only design rulings, 2026-05-21) to the seven questions the G2 plan/spec left unspecified. **Additive** — these refine/extend §6–§10 + §17 without contradicting existing normative text. Where a ruling fixes a previously-ambiguous knob default (§10), it is recorded here as the **G2 decision** rather than editing the original §10 line.

1. **Grouping engine (refines §6b step 3).** G2 grouping is **pure-Python connected components**, no networkx. Edge threshold: keep a scoped edge iff `normalized_lift >= 0.20`. **Bridge guard:** after thresholding, drop a graph-theoretic bridge edge iff removing it splits into two sides each with `>= 2` theme-eligible entities **AND** the bridge `weight < 0.50`; **keep** bridges with `weight >= 0.50`, and **keep** leaf bridges (either side of size `1`). **Acceptance fixture** asserts exactly: two 3-node dense clusters (internal weights `0.70/0.75/0.80`) → two groups; a cross-cluster edge at `0.19` is dropped by threshold; a cross-cluster bridge at `0.30` is dropped by the bridge guard (clusters do not merge); a leaf attachment at `0.30` stays attached to its parent group; group membership + ranking are deterministic across repeated runs. **Failure** = any of those assertions fails. **networkx is pulled into G2 only if the pure implementation of the same threshold/bridge/CC rules fails that fixture**, and then only for `bridges()` / `connected_components()`. Louvain / community detection remains **G3** (§4 D5).

2. **Concept extractor eval gate (fills the §4 D3 gap).** Gate **metric** = document-level, **type-aware concept-set micro-F1** over unique `(entity_type, canonical_key)` pairs after canonicalization, **people excluded**. **Pass threshold** = `micro_f1 >= 0.80` **AND** `precision >= 0.85` **AND** `recall >= 0.70` **AND** `invalid_json_or_schema_rate == 0`. Passing this gate is necessary but not sufficient to flip the default: **`BRAIN_GRAPH_CONCEPTS` stays default-OFF for the G2 ship regardless** (§10) — flipping default-ON is a separate, later explicit decision.

3. **Auto-router (concretizes §6d).** Deterministic, pure/testable rule set: (1) if `mode != auto`, honor it (subject to Q3-fallback below for `global`). (2) **Thematic intent** via a **closed regex grammar** (not an open-ended synonym list): normalized query matches `\bthemes?\b|\btopics?\b|\bpatterns?\b|\btrends?\b|\brecurring\b` **OR** `\bhow\s+(has|have|did|does)\b.{0,80}\b(evolve|evolved|change|changed|shift|shifted)\b`. (3) **Person resolution precedence:** explicit `--person` / MCP `person` first; otherwise a token-boundary name / `canonical_key` scan of known person entities in the query. (4) **Query-match tie-break:** longest normalized matched span → highest `doc_count` → lexicographically smallest `canonical_key`. (5) **Branches:** `thematic AND resolved_person → themes`; `thematic AND no resolved_person → global` (degraded in G2, see decision 4); else → `local`.

4. **G2 global-branch fallback (G2-only; resolves the §6c/§12 "global lands in G3" gap).** Because global mode (community summaries) is **G3**, the G2 auto-router must never dispatch to it. **`mode=auto` thematic + no resolvable person degrades global → local**: run entity-centric local retrieval using the thematic query terms as entity-seed candidates, returning a normal **never-raise** `GraphContext`. The context **signals**: `mode='local'`, `requested_mode='auto'`, `degraded_from='global'`, `degradation_reason='global_unavailable_g2'`. By contrast, an **explicit** `--mode global` (CLI) / `mode='global'` (MCP) **REJECTS, never degrades**: core raises a new `GraphModeUnavailable(BrainError)` ("global mode lands in G3"); the CLI maps it to `typer.BadParameter` (exit 2) and MCP maps it to `McpError(INVALID_PARAMS, ...)`.

5. **Config defaults (fixes the §10 ambiguities).** **G2 decision:** `BRAIN_GRAPH_MAX_DEGREE = 50` (validate as a positive int — supersedes the ambiguous "=cap" placeholder in §10); `BRAIN_GRAPH_MIN_EDGE_WEIGHT = 0.20` (validate as a float in `[0.0, 1.0]` — the §10 line gave no default). The `0.20` floor is intentionally identical to decision 1's grouping threshold; both pair with `DEPTH=2` / `FRONTIER_CAP=200` to keep worst-case depth-2 expansion bounded while pruning weak normalized-lift edges.

6. **Benchmark CI policy (matches the eval-gate precedent).** The **G2 P95 benchmark gate runs in CI**, in a **dedicated workflow**, **excluded from the default `pytest`** exactly like the eval marker: the default invocation becomes `-m 'not eval and not benchmark'`, and the gate runs `pytest -m benchmark --no-cov` against the **pinned AGE Docker image**. **Local use is manual only.** Seed the synthetic corpus **once per pytest session** with a session-scoped fixture using `BENCHMARK_SPEC_FULL` and **seed `1234`**; corpus **generation time is excluded from the P95** measurement. Budgets: CI generation ≤ 20 min, benchmark job ≤ 30 min. (Gate thresholds unchanged from §16/§17.3: P95 local traversal ≤ 750 ms, P95 themes-with-X ≤ 2 s, over 50k entities / 500k `CO_OCCURS` / 1M mentions / 10 tenants.)

7. **`--synthesize` scope (confirms §6 D6 / §9 for G2).** **`--synthesize` ships IN G2** as an **opt-in flag, default-off**; it must **never be required for retrieval**. If Ollama is down / times out / returns invalid JSON or text / the model is missing, retrieval still **exits successfully with `summary=None` + a WARN** (stderr/log) — matching the existing **GraphSyncer / enrichment never-raise discipline** (§7; `graph_rag/sync.py`): optional LLM/graph side work is best-effort, warned, and recomputable, and must not turn a retrieval path into a hard live-Ollama dependency.

## 17c. G3 Resolved Decisions (Codex-ruled)

Codex-ruled answers (read-only design rulings, 2026-05-22) to the ten questions the G3 plan/spec left unspecified (global communities — tasks G3-a..g). **Additive** — these refine/extend §4 D5, §5, §6c/§6d, §8–§10, §16/§17.3 without contradicting existing normative text. Same status as §17b: where a ruling fixes a previously-ambiguous knob/shape it is recorded here as the **G3 decision**. No ruling was flagged low-confidence.

1. **Migration 013 schema (fixes the §5a `graph_communities` + `graph_community_members` gap).** **Single level only** — a `level INTEGER NOT NULL DEFAULT 0 CHECK (level = 0)` column is present but multi-level stays deferred (§15). **UUID** stable identity (not text slug). `graph_communities`: `tenant_id TEXT NOT NULL DEFAULT 'default'`, `community_key UUID NOT NULL DEFAULT gen_random_uuid()`, `level` (above), `build_version TEXT NOT NULL DEFAULT 'networkx-louvain-v1'`, `source_graph_hash TEXT NOT NULL`, `members_hash TEXT NOT NULL`, `member_count INTEGER NOT NULL DEFAULT 0`, `edge_count INTEGER NOT NULL DEFAULT 0`, `total_weight REAL NOT NULL DEFAULT 0`, `summary TEXT NULL`, `summary_model TEXT NULL`, `summary_at TIMESTAMPTZ NULL`, `summary_embedding vector(1024) NULL`, `summary_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(summary,''))) STORED`, `created_at`, `updated_at`; **PK** `(tenant_id, community_key)`, **UNIQUE** `(tenant_id, level, members_hash)`. `graph_community_members`: `tenant_id TEXT NOT NULL DEFAULT 'default'`, `community_key UUID NOT NULL`, `entity_id UUID NOT NULL`, `member_rank INTEGER NOT NULL DEFAULT 0`, `member_weight REAL NOT NULL DEFAULT 0`; **PK** `(tenant_id, community_key, entity_id)`, tenant-safe composite **FK** `(tenant_id, community_key) → graph_communities` and `(tenant_id, entity_id) → graph_entities`, both `ON DELETE CASCADE`. **Dirty fingerprint = `source_graph_hash`** (a tenant-graph edge hash over ordered `graph_relationships`), **not** a members-set hash; `members_hash` is per-community identity only. Matches 012's tenant-scoped-key + composite-FK conventions exactly.

2. **Communities are RELATIONAL-only (confirms §5a / §6c / §17b dec 2).** No AGE `Community` vertex label and no membership edges in AGE; the graph stays `Entity`/`Document` + `MENTIONED_IN`/`CO_OCCURS`. Community summaries, memberships, and embeddings live in SQL (same rule as embeddings + derived aggregates).

3. **Detection runs in a dedicated `brain graphrag communities build|refresh` subcommand** — *not* folded into `graphrag refresh`, *not* a `build --communities` flag. `communities build` computes the current tenant `source_graph_hash` and **skips** when the stored `(build_version, source_graph_hash)` already matches; `communities refresh` forces the rebuild regardless. **Reconcile sets no dirty flag** and **no per-query Louvain** (batched at build/refresh per LazyGraphRAG §4 D2). Jaccard stable-identity matching runs **only after** the dirty gate fires, to preserve `community_key`s across rebuilds.

4. **Global ranking = RRF over communities (concretizes §6c "vector+FTS").** The ranked unit is the **community** (`graph_communities`), not docs/chunks. Two signals: an **FTS rank** over `summary_tsv`/`summary` and a **vector cosine rank** over `summary_embedding` (rows with `summary_embedding IS NOT NULL`). Fusion `score = Σ 1/(60 + rank)` (RRF_K=60, matching `search.py`), ties broken by `community_key`. **Extract a tiny pure helper** (e.g. `brain.rank_fusion.rrf_contribution(rank, k=60)`) and have **both** `search.py` and the global path use it (DRY — the inline RRF in `hybrid_search` is refactored onto the shared helper).

5. **New `CommunityGroup` dataclass (do not reuse `ThemeGroup`).** Frozen, fields: `community_key: str`, `level: int = 0`, `member_count: int = 0`, `score: float = 0.0`, `summary: str | None = None`, `entities: list[GraphEntity] = field(default_factory=list)`, `doc_ids: list[str] = field(default_factory=list)`. Add `communities: list[CommunityGroup] = field(default_factory=list)` to `GraphContext` and a top-level `"communities"` key in `graph_context_json` / renderable. Existing keys stay additive + stable (Q1-C/Q1-D wire-shape discipline).

6. **G2 degradation machinery: KEEP-DORMANT** (not removed) for wire stability. `GraphContext.requested_mode`/`degraded_from`/`degradation_reason` (+ the `RoutingDecision` fields + the `format.py` degradation lines + the `DEGRADED_FROM_GLOBAL`/`DEGRADATION_REASON_G2` constants) **stay**, but G3 **stops populating** them. The router returns `GLOBAL_MODE` for both explicit `--mode global` **and** the auto thematic-without-person branch (the flip — task G3-e). The CLI/MCP global-**reject** mapping + its tests go away because explicit global now **works**; `GraphModeUnavailable` may remain defined for compatibility but **must no longer be raised** for global.

7. **Jaccard DRY: extract to a neutral module** — `src/brain/set_similarity.py` with `jaccard(a: AbstractSet[T], b: AbstractSet[T]) -> float`. **Both** `brain.eval.graph_retrieval` (replacing its private `_jaccard`) and the new communities matcher import from there — neither `eval/` nor `graph_rag/` becomes a dependency of the other.

8. **G3 perf budgets:** P95 **global query ≤ 1000 ms** (consistent with §16/§17.3's local ≤750 ms / themes ≤2 s); **community build ≤ 5 minutes** on the existing full benchmark corpus (50k entities / 500k `CO_OCCURS` / 1M mentions / 10 tenants). **Extend the `-m benchmark` gate** with one community-build wall-clock assertion + one global-query P95 assertion; CI corpus-generation budget stays ≤20 min and the benchmark job ≤30 min (§17b dec 6). The build assertion measures Louvain + relational writes; the global assertion measures retrieval over **prebuilt** communities.

9. **Embedder seam: global REQUIRES an embedder; local/themes stay embedder-free.** Add an optional `embedder_factory: Callable[[], Embedder] | None` to `graph_rag_search`; the CLI `_graphrag_search_or_exit` passes `lambda: make_embedder(cfg)`, the MCP `_graphrag_search_or_mcp_error` passes `lambda: state.embedder`, and `_retrieve_global` invokes it **only after** routing resolves to `global` (so local/themes never construct an embedder). If the factory is absent or the query-embedding call fails, **log a WARN and run FTS-only** rather than failing retrieval (never-raise).

10. **"Lazy embedded summaries" = EAGER at community build/refresh, not per-query.** ("Lazy" in §4 D5 means deferred to the batch build relative to *ingest* — **not** lazy relative to the query.) The batch writes memberships, then **best-effort** generates `summary` / `summary_model` / `summary_at` / `summary_embedding`: `summarize_group` returning `None` leaves the summary fields NULL; an embedding failure leaves `summary_embedding` NULL. **Query time never calls Ollama for summaries** — it only embeds the *query* for the vector signal and **degrades to FTS-only** when `summary_embedding IS NULL` or the query embedding is unavailable (matches §6c "guarded on non-null embedding").

## 17d. G4 Resolved Decisions (Codex-ruled)

Codex-ruled answers (read-only design rulings, 2026-05-22) to the five questions the G4 plan/spec left unspecified (final polish — tasks G4-a..g / #43–#49). **Additive** — these refine/extend §6, §7, §12 (G4 row), §13, §16/§17.3 without contradicting existing normative text. Same status as §17b/§17c. No ruling was flagged low-confidence.

1. **`fuse` mode = RRF over doc-id rank from both legs (fixes the §12/§15 "fuse before G4" gap).** The new mode fuses the **graph leg** with the **vector+FTS hybrid leg** via **`brain.rank_fusion.rrf_contribution(rank, k=60)`** over each leg's **document-id rank** — **not** a score-blend (same RRF_K=60 discipline as `search.py` / global / `build_related`). **Graph leg = `local` docs only**; `themes`/`global` stay separate modes (their ranked units — theme groups / communities — and scoping semantics differ, so they don't fuse cleanly into a doc list). Add a `FUSE_MODE = "fuse"` constant (router vocabulary, re-exported from `retrieve`). The fused `SearchResult`s land in **`GraphContext.docs`** (unchanged field/type — wire-stable); **`GraphContext.mode`** and **`GraphExplanation.mode`** are both stamped `"fuse"`; per-doc leg provenance goes in **`GraphExplanation.matched_filters["fuse_doc_provenance"]`** (no change to `SearchResult`). **Fallback (never-raise):** if embedder construction / the query embedding fails, WARN and degrade to **graph + FTS-only hybrid**; if the hybrid leg cannot run at all, return **graph-only + WARN**. (Mirrors the §17b dec 7 / §17c Q9 best-effort-optional-leg discipline.)

2. **Interactions generalization = migration 015 (concretizes the §12 G4 row "nullable doc_id + target_type/target_id + graph_retrieved").** Mig **015** (not 014 — see decision 5): make **`interactions.document_id` NULLABLE**; add **`target_type TEXT NULL`**, **`target_id TEXT NULL`**, **`graph_retrieved BOOLEAN NOT NULL DEFAULT FALSE`**. CHECK constraints: (a) `target_type IS NULL OR target_type IN ('entity','community','theme')`; (b) **exactly one target shape** — either `document_id IS NOT NULL AND target_type IS NULL AND target_id IS NULL` **XOR** `document_id IS NULL AND target_type IS NOT NULL AND target_id IS NOT NULL`. `target_type`/`target_id` make **entity/community/theme first-class rateable interaction targets**; `graph_retrieved` is a **provenance flag** (a graph surface produced this row), **not** part of the target model. **Logging hook = user action / open time only — never retrieval-time** (no auto-log on every graph result). Wire surface: **generalize `record_interaction`** (new optional `target_type`/`target_id`/`graph_retrieved` params) + add an **optional `graph_retrieved` param to `brain_show`** — **do NOT** add a new `InteractionSource` value and **do NOT** change the locked `{session_id, results}` `brain_search`/`brain_show` response shape. Keep Python `_VALID_ACTIONS`/`_VALID_SOURCES` (and a new `_VALID_TARGET_TYPES`) in lockstep with the SQL CHECKs.

3. **Eval expansion = parallel graph-eval runner + separate baseline (NOT a category inside `EvalReport`).** The G2-j graph scorers (`score_local_docs` / `score_themes` in `brain.eval.graph_retrieval`) report **set precision/recall/F1** shapes that don't fit `EvalReport`'s nDCG@5/MRR/Recall@20-only model, so G4 adds a **parallel graph eval runner + report with a separate graph-baseline path**, not a new `_VALID_CATEGORIES` entry on the hybrid runner. **Golden corpus = reuse `tests/eval/graph_retrieval_cases.py`** (the existing G2-j `LOCAL_CASES`/`THEMES_CASES` fixture), not a new committed corpus. **Do NOT add `--fail-below` or a committed `ci.json` in G4** (that flag+baseline precedent belongs to the separate Q1 audit roadmap and does not exist in this repo's eval CLI). Instead **record/diff a graph-specific baseline as a canary** and keep the **blocking** guarantees where they already live — the existing synthetic-graph tests + the `-m benchmark` gate.

4. **`fuse` P95 budget = ≤ 1500 ms (extends §16/§17.3, §17b dec 6, §17c Q8).** Concrete budget: **P95 `mode=fuse` ≤ 1500 ms** on the full benchmark corpus (50k entities / 500k `CO_OCCURS` / 1M mentions / 10 tenants) — local traversal's 750 ms plus a bounded hybrid-leg allowance, **measured end-to-end including the query embedding**. Add **exactly one new assertion** to the existing **`-m benchmark`** gate (dedicated CI workflow, excluded from the default `pytest` via `-m 'not eval and not benchmark'`), following the §17b dec 6 / §17c dec 8 pattern.

5. **Migration number = 015 (corrects the stale §12 G4 row + plan text).** The interactions migration **must be 015**: `014_graphrag_community_summary_hash.sql` is already taken (G3-c). The plan/spec text "mig 014 interactions" (plan §G4 ~L39, §12 G4 row ~L217) is **stale** and should read **015**.

6. **`fuse` multi-tenant isolation = fail-fast gate to `tenant='default'` (closes G4-review finding P1-1; Codex-ruled 2026-05-22, high-confidence).** **Verified leak:** `documents`/`chunks` carry **no `tenant_id`** column (only the graph_* tables — migrations 012/013/014 — are tenantized; the base document store predates v5.1 graph multi-tenancy and was not retrofitted). `mode=fuse`'s **hybrid leg** (`brain.search.hybrid_search`, `fuse.py::_run_hybrid_leg`) queries `documents`/`chunks` **un-tenant-filtered (corpus-wide)**, so `graph_rag_search(mode='fuse', tenant='other')` can surface documents the tenant's graph never reaches — a cross-tenant document leak in a multi-tenant deployment. **`mode=local` and `mode=global` are NOT affected:** their returned doc-id sets are derived **solely** through tenant-scoped graph rows (`local` via `graph_entity_mentions WHERE tenant_id=%s` in `retrieve.py::_rank_documents`; `global` via `graph_community_members` JOIN `graph_entity_mentions`, both tenant-predicated, in `global_.py`), so they are inherently tenant-scoped — fuse is the **only** concrete leak. **Ruling — Option C (fail-fast gate):** `fuse` stays available **only for `tenant_id == "default"`** until the document corpus is tenantized. `_retrieve_fuse` (`src/brain/graph_rag/fuse.py`) **raises a caller-facing `ValueError` BEFORE running either leg** (not empty, not graph-only, not hybrid-filtered) when `tenant != "default"`; suggested message: `"mode='fuse' is only available for tenant 'default' until documents/chunks are tenantized; use mode='local' or mode='global' for graph-scoped retrieval"`. `ValueError` maps cleanly through the existing graphrag surfaces (CLI `_graphrag_search_or_exit` → `typer.BadParameter` exit 2; MCP `_graphrag_search_or_mcp_error` → `INVALID_PARAMS`). A **regression test** must prove a non-default `fuse` rejects **before** any corpus-wide hybrid result can surface. **Rejected alternatives:** (A) document-only — a note cannot close a P1 leak on its own; (B) constrain fuse's hybrid leg to the tenant's `graph_entity_mentions` doc universe — rejected because it turns the mention mirror into an implicit document ACL with silent recall loss (tenant docs with no extracted entities silently vanish from the hybrid leg). **This §17d note alone is NOT sufficient for P1-1 — it is sufficient only once the code gate lands** (the gate is the required fix; this entry records the decision, the implementer wave lands the guard + test). **Tracked follow-up (document-level tenancy retrofit):** add `tenant_id` to `documents`/`chunks` + tenant-safe FKs/filters on every doc-returning path, then **remove the fuse gate** and revisit `local`/`global` scoping. Until then `local`/`global` remain as-is (inherently scoped via the mention mirror).

**Scope confirmations (Codex):** (i) **G4 does NOT build the Neo4j/Memgraph backend** — the AGE P95 perf gate **passed** in G2, so the §16 kill-switch is not triggered; Neo4j stays the §15/§16 escape-hatch (defined-but-unbuilt). (ii) The **`cli.py` (6673 lines) / `mcp_server.py` (1886 lines) >800-line split is correctly DEFERRED** — G4 is feature polish, not a large mechanical file-split refactor; it must not be folded into this wave. (iii) **D7 invariant clarification (G4-e DRY, closes review finding P3-1 — no revert):** D7's "`hybrid_search` untouched" means its **ranking BEHAVIOR is unchanged**, not "zero edits". The G4-e DRY consolidation refactored `hybrid_search`'s inline RRF onto the shared `brain.rank_fusion.rrf_contribution(rank, k=60)` helper (also used by `global` / `fuse` / `build_related`, per §6c dec 4 / §17d dec 1). This is a no-op on the formula — same `1/(60 + rank)` contribution, same fusion, same ordering — so the D7 invariant holds; only the duplicated implementation was removed. The `rrf_contribution` adoption in `search.py` is intentional and must NOT be reverted.

## 18. Historical Codex logs (v1–v4, pre-AGE)
The r1–r5 resolution logs pertain to the v4 *plain-tables + Python-BFS* design and remain valid for the **correctness model** v5 preserves: per-doc source-of-truth + full refresh; raw-text window co-occurrence (dedupe chunk overlap); generic suppression at derive time; normalized lift ∈ (0,1]; scope-first themes via graph person mentions; distinct `GraphContext`; dim reconciliation via `psycopg.sql.Identifier` preserving chunks semantics. They are **superseded for storage/traversal** (now AGE) and did not consider multi-tenancy/scale. Full r1–r5 text is in this file's git history (pre-pivot versions).
