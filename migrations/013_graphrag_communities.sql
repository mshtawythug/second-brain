-- Migration 013 — GraphRAG global communities (wave G3, AGE pivot v5).
--
-- Additive only. This migration owns the RELATIONAL community tables for the
-- LazyGraphRAG-style global mode per spec docs/specs/2026-05-20-graphrag-design.md
-- §17c Q1. Communities are RELATIONAL-ONLY (§17c Q2): there is NO AGE
-- ``Community`` vertex label and no membership edges in AGE — the graph stays
-- ``Entity``/``Document`` + ``MENTIONED_IN``/``CO_OCCURS``. Community summaries,
-- memberships, and embeddings live here in SQL exactly like the embeddings +
-- derived aggregates. Keep this file pure SQL with no AGE/Cypher DDL.
--
--   graph_communities         — one row per detected community (networkx Louvain
--                               over the tenant entity graph). Carries the
--                               stable ``community_key`` identity, the dirty
--                               fingerprint (``source_graph_hash``), the
--                               per-community identity hash (``members_hash``),
--                               aggregate stats, and the lazily-built (eager at
--                               community build/refresh — §17c Q10) summary +
--                               summary embedding/tsv for the global retrieval
--                               RRF (§17c Q4).
--   graph_community_members   — community ↔ entity membership (per-community
--                               rank + weight). Tenant-safe composite FKs to
--                               BOTH graph_communities and graph_entities.
--
-- SINGLE LEVEL ONLY (§17c Q1 / §15): ``level`` is present but pinned to 0 via a
-- CHECK; hierarchical multi-level communities stay deferred (YAGNI).
--
-- MULTI-TENANCY (spec §4 D9, §5): every table carries
-- ``tenant_id TEXT NOT NULL DEFAULT 'default'`` and ``tenant_id`` is part of
-- every PK / UNIQUE and every lookup index — every key below is tenant-scoped.
-- The membership table carries TENANT-SAFE composite FKs
-- ``(tenant_id, community_key) -> graph_communities(tenant_id, community_key)``
-- and ``(tenant_id, entity_id) -> graph_entities(tenant_id, id)`` so a member
-- row can only ever reference a community / entity in its OWN tenant. Mirrors
-- the 012 conventions exactly.
--
-- DIRTY FINGERPRINT (§17c Q1/Q3): ``source_graph_hash`` is a tenant-graph edge
-- hash over the ordered ``graph_relationships``; ``communities build`` skips
-- when the stored ``(build_version, source_graph_hash)`` already matches.
-- ``members_hash`` is per-community identity only (Jaccard stable-identity).
--
-- EMBEDDINGS: ``summary_embedding`` ships as ``vector(1024)`` NULLABLE. The
-- generalized dim reconciliation (G0b — allowlisted in
-- ``brain.embedding_targets`` as ``graph_communities.summary_embedding``)
-- resizes it to the active embedder's dim, and the conditional HNSW is owned by
-- the finalize-time machinery (``brain.queries.finalize_embedding_index``), NOT
-- hardcoded here — exactly as 012 defers ``graph_entities.embedding`` (small row
-- counts → sequential cosine scan until finalize). The ``summary_tsv`` GENERATED
-- column backs the FTS leg of the global RRF (§17c Q4); it uses the immutable
-- 2-arg ``to_tsvector('english', …)`` form so it is valid in a STORED column
-- (matches 009's chunks.tsv style).

BEGIN;

-- One row per detected community. ``community_key`` is the durable, stable
-- identity preserved across rebuilds by Jaccard matching (§17c Q3/Q7).
-- ``source_graph_hash`` + ``members_hash`` have NO default — the detector always
-- computes them. Aggregate counts default to 0 and are CHECK-enforced
-- non-negative (mirrors 012). PK ``(tenant_id, community_key)``; UNIQUE
-- ``(tenant_id, level, members_hash)`` is the per-community identity guard.
CREATE TABLE IF NOT EXISTS graph_communities (
    tenant_id         TEXT NOT NULL DEFAULT 'default',
    community_key     UUID NOT NULL DEFAULT gen_random_uuid(),
    level             INTEGER NOT NULL DEFAULT 0,   -- single-level only (§15)
    build_version     TEXT NOT NULL DEFAULT 'networkx-louvain-v1',
    source_graph_hash TEXT NOT NULL,                -- dirty fingerprint (§17c Q3)
    members_hash      TEXT NOT NULL,                -- per-community identity hash
    member_count      INTEGER NOT NULL DEFAULT 0,
    edge_count        INTEGER NOT NULL DEFAULT 0,
    total_weight      REAL NOT NULL DEFAULT 0,
    summary           TEXT,                          -- lazy/eager at build (§17c Q10)
    summary_model     TEXT,
    summary_at        TIMESTAMPTZ,
    summary_embedding vector(1024),                  -- dim-aware; nullable; HNSW deferred
    summary_tsv       tsvector GENERATED ALWAYS AS (
                          to_tsvector('english', coalesce(summary, ''))
                      ) STORED,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, community_key),
    CONSTRAINT gc_level_chk CHECK (level = 0),
    CONSTRAINT gc_member_count_chk CHECK (member_count >= 0),
    CONSTRAINT gc_edge_count_chk CHECK (edge_count >= 0),
    CONSTRAINT gc_total_weight_chk CHECK (total_weight >= 0),
    -- Per-community identity is tenant-scoped: the same members_hash may exist
    -- independently per tenant (and, once multi-level lands, per level).
    CONSTRAINT uq_graph_communities UNIQUE (tenant_id, level, members_hash)
);
-- Global retrieval scans a tenant's level-0 communities; the PK leads with
-- tenant_id, but the explicit (tenant_id, level) index documents the
-- candidate-set access pattern and survives the multi-level deferral.
CREATE INDEX IF NOT EXISTS idx_graph_communities_tenant_level
    ON graph_communities (tenant_id, level);
-- FTS leg of the global RRF (§17c Q4).
CREATE INDEX IF NOT EXISTS idx_graph_communities_summary_tsv
    ON graph_communities USING GIN (summary_tsv);

-- Community ↔ entity membership. Re-detection deletes+reinserts a tenant's
-- rows. Tenant-safe composite FKs guarantee both the community and the entity
-- live in the SAME tenant. ``member_rank`` / ``member_weight`` order entities
-- within a community (e.g. by degree / centrality); both CHECK-enforced
-- non-negative.
CREATE TABLE IF NOT EXISTS graph_community_members (
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    community_key UUID NOT NULL,
    entity_id     UUID NOT NULL,
    member_rank   INTEGER NOT NULL DEFAULT 0,
    member_weight REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, community_key, entity_id),
    CONSTRAINT gcm_member_rank_chk CHECK (member_rank >= 0),
    CONSTRAINT gcm_member_weight_chk CHECK (member_weight >= 0),
    CONSTRAINT fk_gcm_community
        FOREIGN KEY (tenant_id, community_key)
        REFERENCES graph_communities (tenant_id, community_key) ON DELETE CASCADE,
    CONSTRAINT fk_gcm_entity
        FOREIGN KEY (tenant_id, entity_id)
        REFERENCES graph_entities (tenant_id, id) ON DELETE CASCADE
);
-- Reverse lookup (which communities an entity belongs to) + makes the
-- graph_entities-delete cascade over this FK fast (Postgres does not
-- auto-index FK referencing columns). The (tenant_id, community_key) FK is
-- already covered by the PK's leading columns.
CREATE INDEX IF NOT EXISTS idx_gcm_entity
    ON graph_community_members (tenant_id, entity_id);

COMMIT;
