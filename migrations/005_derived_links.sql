-- Metadata-aware linker — schema for derived edges + person directory.
--
-- Additive only: nothing in prior migrations is altered or dropped. Three
-- new tables back the metadata-aware linker pipeline:
--
--   * ``derived_links`` — sibling of ``links`` (which holds wiki-link edges
--     only). Each row is an edge inferred from metadata overlap between two
--     documents (e.g. shared Gmail thread, shared participant on the same
--     day). Carries the rule that produced it, JSONB evidence, and a weight.
--   * ``directory_entries`` — persistent name↔email directory used to bridge
--     Krisp speaker labels (often a name) with Gmail headers (always an
--     email). Built incrementally from already-stored Gmail headers, MCP
--     calendar events, MCP contacts, and an optional ``people.yml`` override.
--   * ``directory_refresh_state`` — high-water mark per source so calendar /
--     contacts refreshes are incremental: each refresh fetches
--     ``(last_refreshed_at, now]`` only.
--
-- Two CHECK constraints tighter than spec §5 (intentional, mirrors the
--   `link_kind` pattern from migrations/003_vault_model.sql:23):
--   - `rule` is enum-restricted; adding a 4th rule will require ALTER.
--   - `weight` is range-restricted to [0, 1.0]; emphasized overrides above
--      1.0 will require ALTER.

BEGIN;

-- Edges derived from metadata overlap; sibling of `links` (which holds
-- wiki-link edges only).
CREATE TABLE derived_links (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  src_document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  dst_document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  rule            TEXT NOT NULL CHECK (rule IN ('shared_thread', 'shared_participant', 'same_day_participant')),
  evidence        JSONB NOT NULL DEFAULT '{}'::jsonb,
  weight          REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0.0 AND weight <= 1.0),
  CHECK (src_document_id <> dst_document_id),
  UNIQUE (src_document_id, dst_document_id, rule)
);
CREATE INDEX derived_links_src_idx ON derived_links (src_document_id);
CREATE INDEX derived_links_dst_idx ON derived_links (dst_document_id);
CREATE INDEX derived_links_rule_idx ON derived_links (rule);

-- Persistent name↔email directory used to bridge Krisp speaker labels
-- (often a name) with Gmail headers (always an email). Built incrementally
-- from already-stored Gmail headers, MCP calendar events, MCP contacts,
-- and an optional override file.
CREATE TABLE directory_entries (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name    TEXT NOT NULL,             -- normalized lowercase
  email           TEXT NOT NULL,             -- normalized lowercase
  source          TEXT NOT NULL CHECK (source IN ('gmail', 'calendar', 'contacts', 'people_yml')),
  occurrence_count INT NOT NULL DEFAULT 1,
  first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (display_name, email, source)
);
CREATE INDEX directory_entries_name_idx ON directory_entries (display_name);
CREATE INDEX directory_entries_email_idx ON directory_entries (email);

-- High-water mark per source so calendar / contacts refreshes are
-- incremental: each refresh fetches `(last_refreshed_at, now]` only.
CREATE TABLE directory_refresh_state (
  source              TEXT PRIMARY KEY CHECK (source IN ('gmail', 'calendar', 'contacts')),
  last_refreshed_at   TIMESTAMPTZ NOT NULL,
  records_seen        INT NOT NULL DEFAULT 0
);

COMMIT;
