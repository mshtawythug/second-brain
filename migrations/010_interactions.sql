-- Wave Q1-C — Interactions table.
-- Append-only feedback log. One row per user event (open / rate / pin / click).
-- Q1-C populates from CLI (brain rate) and MCP (brain_show with
-- originating_query). The wiki click surface lands in a later wave
-- (source='wiki' is accepted at the SQL layer so the future wave is
-- purely additive).
-- Additive-only: nothing in prior migrations is altered.

BEGIN;

CREATE TABLE interactions (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id  UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  query        TEXT,
  action       TEXT NOT NULL
                 CHECK (action IN ('clicked', 'opened',
                                   'rated_useful', 'rated_irrelevant',
                                   'pinned')),
  source       TEXT NOT NULL
                 CHECK (source IN ('cli', 'mcp', 'wiki')),
  session_id   UUID,   -- nullable: CLI ratings have no session
  at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX interactions_document_at_idx ON interactions (document_id, at DESC);
CREATE INDEX interactions_action_idx      ON interactions (action);
CREATE INDEX interactions_session_idx     ON interactions (session_id)
  WHERE session_id IS NOT NULL;

COMMIT;
