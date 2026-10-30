-- 029_saved_searches.sql — saved searches ("watches") and the documents each has matched (RM-15).
--
-- `brain watch add NAME QUERY` stores a search here; each check records the
-- documents that newly match it in `saved_search_hits`, and the daily brief
-- lists them. A hit is keyed (watch, document), so a document is reported at
-- most once per watch: a racing check's INSERT ... ON CONFLICT DO NOTHING
-- RETURNING reports nothing for a row another check already recorded.
--
-- `notified_at` is NULL until a published brief (`brain brief --wiki`) has
-- listed the hit; the partial index serves the "unnotified hits" read.
-- `last_checked_at` is the start time of the last recording check, and only
-- ever moves forward (the check writes GREATEST(last_checked_at, start)).
--
-- `filters` holds the search filters a watch saves (source, tag, person, kind,
-- without_tag), validated by the CLI and MCP before they are written.
--
-- Additive: two new tables and an index, no change to an existing table.
-- Re-runnable: every statement is IF NOT EXISTS.
--
-- NEVER edit shipped migrations 001-028.

BEGIN;

CREATE TABLE IF NOT EXISTS saved_searches (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name             TEXT NOT NULL UNIQUE,
  query            TEXT NOT NULL,
  filters          JSONB NOT NULL DEFAULT '{}',
  mode             TEXT NOT NULL DEFAULT 'lexical' CHECK (mode IN ('lexical', 'semantic')),
  enabled          BOOLEAN NOT NULL DEFAULT TRUE,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_checked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS saved_search_hits (
  saved_search_id  UUID NOT NULL REFERENCES saved_searches(id) ON DELETE CASCADE,
  document_id      UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  notified_at      TIMESTAMPTZ,
  PRIMARY KEY (saved_search_id, document_id)
);

CREATE INDEX IF NOT EXISTS idx_saved_search_hits_unnotified
  ON saved_search_hits (saved_search_id) WHERE notified_at IS NULL;

COMMIT;
