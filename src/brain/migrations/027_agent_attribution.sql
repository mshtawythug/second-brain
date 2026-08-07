-- 027_agent_attribution.sql — record WHICH agent produced an event.
-- ``source`` (cli|mcp|wiki) already records the SURFACE; agent_id records the
-- ACTOR. Free-form TEXT with no CHECK: the set of agents is open-ended and
-- user-defined; the shape gate lives at the Python boundary
-- (brain.agent.normalize_agent_id). NULL = unattributed, which is what every
-- pre-027 row is and what any un-configured surface keeps writing.
--
-- Deliberately NO CHECK constraint. A CHECK mirroring a Python regex is
-- exactly the drift that migration 024 exists to remember: the Python mirror
-- rejected earlier than the INSERT, so fixing only the SQL looked correct and
-- was not. One gate, at the Python boundary, tested there.
--
-- All three ALTERs are nullable-TEXT-no-default, which PostgreSQL 11+ applies
-- as a catalog-only change — no table rewrite even on ``documents`` with its
-- STORED generated ``tsv`` column (contrast migration 021, which added a
-- GENERATED column and DID rewrite). NEVER edit shipped migrations 001-026.

BEGIN;

ALTER TABLE documents      ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE interactions   ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE search_queries ADD COLUMN IF NOT EXISTS agent_id TEXT;

-- Partial indexes: the ``brain usage`` by-agent rollups scan only attributed
-- rows, and on a brain that never sets BRAIN_AGENT_ID both indexes stay empty
-- and cost nothing to maintain.
CREATE INDEX IF NOT EXISTS search_queries_agent_at_idx
    ON search_queries (tenant_id, agent_id, at DESC)
    WHERE agent_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS interactions_agent_at_idx
    ON interactions (agent_id, at DESC)
    WHERE agent_id IS NOT NULL;

COMMIT;
