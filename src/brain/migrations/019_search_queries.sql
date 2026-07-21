-- 019_search_queries.sql — lightweight log of every hybrid-search call.
-- Plan 08 (`brain gaps`): a repeated zero-result / no-click query is direct
-- evidence the brain is missing knowledge the user needs. ``interactions``
-- structurally cannot hold a zero-result search (migration 015's
-- document-XOR-graph-target CHECK requires a target, and a failed search has
-- none), so search-failure mining needs its own append-only table.
--
-- Additive + idempotent. Creates ONLY the ``search_queries`` table + indexes.
-- The ``elicitation_gaps.signal_kind`` CHECK already includes 'search_failure'
-- (shipped by migration 018) — this migration does NOT touch that constraint.
-- NEVER edit shipped migrations 001-018.

BEGIN;

CREATE TABLE IF NOT EXISTS search_queries (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    query        TEXT NOT NULL,
    result_count INT  NOT NULL DEFAULT 0,
    session_id   UUID,              -- nullable; MCP path mints one, CLI path NULL
    source       TEXT NOT NULL
                   CHECK (source IN ('cli', 'mcp', 'wiki')),
    at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Recency-ordered tenant scan backs both the read view and the detector window.
CREATE INDEX IF NOT EXISTS search_queries_tenant_at_idx
    ON search_queries (tenant_id, at DESC);

-- Partial index for the zero-result hot path (the headline gap signal).
CREATE INDEX IF NOT EXISTS search_queries_zero_result_idx
    ON search_queries (tenant_id, at DESC)
    WHERE result_count = 0;

COMMIT;
