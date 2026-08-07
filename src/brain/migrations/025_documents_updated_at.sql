-- 025_documents_updated_at.sql — last content/metadata mutation of a document.
--
-- Additive, following the 007 pattern. Backfilled to ingested_at so no
-- pre-025 row reads a fabricated "now" and no --updated-after filter silently
-- drops history. NOT NULL is applied only after the backfill.
--
-- SEMANTICS (spec: plan "Decisions locked" F9-2): updated_at means "the user's
-- knowledge in this document changed". Maintenance jobs -- enrichment summary
-- backfills, tag normalization, participant-key repair, vault_path bookkeeping
-- -- deliberately do NOT bump it. `ingested_at` keeps its existing
-- edit-bumping behaviour untouched (see update_document in ingest/__init__.py).
--
-- Distinct from the date `--after` / `--before` already filter on: those bind
-- coalesce(sent_at, ingested_at), which for an email or a transcript prefers
-- sent_at and so cannot express the edit dimension at all.
--
-- Re-runnable: ADD COLUMN IF NOT EXISTS is a no-op; the
-- `UPDATE ... WHERE updated_at IS NULL` matches nothing on a second run --
-- which is what stops a re-apply from clobbering a genuine edit timestamp
-- back to ingested_at; SET DEFAULT / SET NOT NULL are idempotent;
-- CREATE INDEX IF NOT EXISTS is a no-op.
--
-- NEVER edit shipped migrations 001-024.

BEGIN;

ALTER TABLE documents ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
UPDATE documents SET updated_at = ingested_at WHERE updated_at IS NULL;
ALTER TABLE documents ALTER COLUMN updated_at SET DEFAULT NOW();
ALTER TABLE documents ALTER COLUMN updated_at SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_documents_updated_at
    ON documents (updated_at DESC);

COMMIT;
