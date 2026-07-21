-- Migration 012 — GraphRAG relational source-of-truth (wave G0, AGE pivot v5).
--
-- Additive only. This migration owns the RELATIONAL source-of-truth +
-- pgvector side tables for the GraphRAG layer per spec
-- docs/specs/2026-05-20-graphrag-design.md §5(a). The Apache AGE graph itself
-- (graph `brain_graph`, vertex/edge labels, property indexes) is NOT created
-- here — it is bootstrapped idempotently by ``brain init`` (a ``create_graph``
-- function call, not frozen DDL) and its labels/indexes are owned by the G0-4
-- GraphBackend. Keep this file pure SQL with no AGE/Cypher DDL.
--
--   graph_entities            — entity nodes (durable app identity; AGE vertex
--                               IDs are internal and not the app identity).
--                               People imported from the people pipeline;
--                               concepts from the gated extractor.
--   graph_entity_mentions     — SOURCE OF TRUTH: per-doc entity mentions
--   graph_edge_contributions  — SOURCE OF TRUTH: per-doc raw window co-occurrence
--   graph_relationships       — DERIVED aggregate edges (recomputed, never
--                               blind-incremented); the SQL mirror of the AGE
--                               CO_OCCURS edges (kept in sync by the same refresh)
--   graph_index_state         — per-aspect incremental watermark (people vs
--                               concepts re-index independently)
--
-- MULTI-TENANCY (spec §4 D9, §5): every table carries
-- ``tenant_id TEXT NOT NULL DEFAULT 'default'`` and ``tenant_id`` is part of
-- every PK / UNIQUE and every lookup index — every key below is tenant-scoped.
-- ``graph_entities`` is keyed on the COMPOSITE ``(tenant_id, id)`` so the child
-- tables can carry TENANT-SAFE composite FKs ``(tenant_id, <entity_col>) ->
-- graph_entities(tenant_id, id)`` — a child row can only ever reference an
-- entity in its OWN tenant. ``id`` remains a globally-unique UUID (it is the AGE
-- ``Entity.entity_uuid`` vertex property); the composite key just makes the
-- relational key tenant-inclusive. Single-user local deployments use the fixed
-- default tenant ``'default'`` so the local experience is unchanged.
--
-- Per-document rows are the only source of truth; aggregates are derived by a
-- full refresh (spec §7). Undirected edges are canonicalized ``src_id < dst_id``
-- and CHECK-enforced. ``graph_relationships.weight`` is normalized lift in
-- (0, 1] (CHECK-enforced, NOT NULL with NO default — callers always compute it).
-- Count columns are CHECK-enforced non-negative. Embedding columns ship as
-- ``vector(1024)`` NULLABLE; the generalized dim reconciliation (G0b) resizes
-- ``graph_entities.embedding`` to the active embedder's dim and a later wave
-- embeds it. HNSW is intentionally skipped here (small row counts →
-- sequential scan; spec §5 note).
--
-- v5.1 note: the paused g0a created an uncommitted, pre-tenant/pre-AGE version
-- of this file; since it was never committed/shipped, v5.1 rewrites it in place
-- to this tenantized schema (no separate migration).
--
-- Communities tables arrive in 013 (G3); the interaction-logging change in
-- 014 (G4).

BEGIN;

-- Entity nodes. Person rows imported from the people pipeline; concepts from
-- the extractor. ``doc_count`` is DERIVED from mentions (refreshed, never
-- authoritative on write). Durable app identity lives in ``id``; the AGE
-- ``Entity`` vertex carries ``entity_uuid = id`` as a property for joins back.
-- COMPOSITE PK ``(tenant_id, id)`` makes the key tenant-inclusive and is the
-- target of the child tables' tenant-safe FKs.
CREATE TABLE IF NOT EXISTS graph_entities (
    id            UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    entity_type   TEXT NOT NULL,            -- 'person'|'org'|'project'|'topic'|'tool'
    name          TEXT NOT NULL,
    canonical_key TEXT NOT NULL,            -- dedup key (resolved person-key | lower(name))
    description   TEXT,
    embedding     vector(1024),             -- dim-aware; nullable until reconciled
    doc_count     INTEGER NOT NULL DEFAULT 0,   -- DERIVED from mentions (refreshed)
    properties    JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, id),
    CONSTRAINT graph_entities_type_chk
        CHECK (entity_type IN ('person', 'org', 'project', 'topic', 'tool')),
    CONSTRAINT graph_entities_doc_count_chk CHECK (doc_count >= 0),
    -- Tenant-scoped dedup: the same canonical_key may exist independently per tenant.
    CONSTRAINT uq_graph_entities UNIQUE (tenant_id, entity_type, canonical_key)
);
CREATE INDEX IF NOT EXISTS idx_graph_entities_type
    ON graph_entities (tenant_id, entity_type);

-- SOURCE OF TRUTH: per-doc entity mentions. Re-ingest deletes+reinserts this
-- doc's rows. Tenant-safe composite FK ``(tenant_id, entity_id)`` guarantees the
-- referenced entity is in the SAME tenant. The document_id FK stays single-col
-- (documents is not tenantized in G0).
CREATE TABLE IF NOT EXISTS graph_entity_mentions (
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    entity_id     UUID NOT NULL,
    document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    mention_count INTEGER NOT NULL DEFAULT 1,
    source        TEXT NOT NULL,            -- 'people' | 'extractor:<model>@<ver>'
    PRIMARY KEY (tenant_id, entity_id, document_id),
    CONSTRAINT gem_mention_count_chk CHECK (mention_count >= 0),
    CONSTRAINT fk_gem_entity
        FOREIGN KEY (tenant_id, entity_id)
        REFERENCES graph_entities (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_gem_document
    ON graph_entity_mentions (tenant_id, document_id);

-- SOURCE OF TRUTH: per-doc edge contributions (raw window co-occurrence over
-- raw text). RAW counts only — no generic suppression applied here (suppression
-- is derive/query-time). Canonical src<dst. Both endpoints carry tenant-safe
-- composite FKs.
CREATE TABLE IF NOT EXISTS graph_edge_contributions (
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    src_id        UUID NOT NULL,
    dst_id        UUID NOT NULL,
    cooccur_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, document_id, src_id, dst_id),
    CONSTRAINT gec_canonical CHECK (src_id < dst_id),
    CONSTRAINT gec_cooccur_count_chk CHECK (cooccur_count >= 0),
    CONSTRAINT fk_gec_src
        FOREIGN KEY (tenant_id, src_id)
        REFERENCES graph_entities (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_gec_dst
        FOREIGN KEY (tenant_id, dst_id)
        REFERENCES graph_entities (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_gec_src ON graph_edge_contributions (tenant_id, src_id);
CREATE INDEX IF NOT EXISTS idx_gec_dst ON graph_edge_contributions (tenant_id, dst_id);

-- DERIVED aggregate edges (recomputed from contributions; never
-- blind-incremented). SQL counterpart of the AGE CO_OCCURS edges (kept in sync
-- by the same refresh) and backs evidence/ranking. ``weight`` is normalized
-- lift in (0, 1] — NOT NULL with NO default (callers always compute it; 0 would
-- violate the range). Both endpoints carry tenant-safe composite FKs.
CREATE TABLE IF NOT EXISTS graph_relationships (
    tenant_id  TEXT NOT NULL DEFAULT 'default',
    src_id     UUID NOT NULL,
    dst_id     UUID NOT NULL,
    rel_type   TEXT NOT NULL DEFAULT 'co_occurs',
    weight     REAL NOT NULL,               -- normalized lift in (0,1] (derived/recomputed)
    co_count   INTEGER NOT NULL DEFAULT 0,  -- SUM(contributions.cooccur_count)
    doc_count  INTEGER NOT NULL DEFAULT 0,  -- COUNT(DISTINCT document_id)
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, src_id, dst_id, rel_type),
    CONSTRAINT grel_canonical CHECK (src_id < dst_id),
    CONSTRAINT grel_weight_chk CHECK (weight > 0 AND weight <= 1),
    CONSTRAINT grel_co_count_chk CHECK (co_count >= 0),
    CONSTRAINT grel_doc_count_chk CHECK (doc_count >= 0),
    CONSTRAINT fk_grel_src
        FOREIGN KEY (tenant_id, src_id)
        REFERENCES graph_entities (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_grel_dst
        FOREIGN KEY (tenant_id, dst_id)
        REFERENCES graph_entities (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_grel_src ON graph_relationships (tenant_id, src_id);
CREATE INDEX IF NOT EXISTS idx_grel_dst ON graph_relationships (tenant_id, dst_id);

-- Incremental watermark, PER ASPECT (people vs concepts re-index
-- independently). An aspect is re-indexed when ANY of its tracked input
-- fingerprints changes (content_hash + inputs_hash + extractor_ver +
-- suppress_ver; spec §7). ``aspect`` is constrained to the two known aspects:
-- 'people' (G1) and 'concepts' (G2).
CREATE TABLE IF NOT EXISTS graph_index_state (
    tenant_id      TEXT NOT NULL DEFAULT 'default',
    document_id    UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    aspect         TEXT NOT NULL,           -- 'people' | 'concepts'
    content_hash   TEXT NOT NULL,           -- body text fingerprint
    inputs_hash    TEXT NOT NULL,           -- aspect inputs: people→participants+directory+owner-key
                                            --   versions; concepts→content+window/cap config
    extractor_ver  TEXT NOT NULL,           -- extractor model@version; bump forces re-extract
    suppress_ver   TEXT NOT NULL DEFAULT '',-- suppression/weighting config version (derive-time)
    indexed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, document_id, aspect),
    CONSTRAINT gis_aspect_chk CHECK (aspect IN ('people', 'concepts'))
);

COMMIT;
