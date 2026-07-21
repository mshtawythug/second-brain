-- Phase 2 follow-up: vault-tier notes can legitimately share body bytes.
--
-- Examples that hit the prior UNIQUE constraint and crashed sync:
--   - Two empty notes (e.g. drafts the user just created from a template)
--   - Two notes whose only body is "TBD" or a placeholder string
--   - Daily notes for two days that happen to start with the same scaffold
--
-- Ingested-tier dedup is still desirable: re-ingesting the same Krisp call
-- should idempotently no-op rather than producing two rows. Keep the unique
-- constraint scoped to ``kind='ingested'`` via a partial index.

BEGIN;

ALTER TABLE documents DROP CONSTRAINT documents_content_hash_key;
CREATE UNIQUE INDEX documents_content_hash_ingested_idx
  ON documents (content_hash) WHERE kind = 'ingested';

COMMIT;
