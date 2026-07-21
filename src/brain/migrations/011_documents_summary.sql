-- Migration 011 — Wave Q1-D per-document auto-summary columns.
--
-- Additive only. Existing rows untouched. Pre-Q1-D rows keep
-- ``summary IS NULL``; ``brain enrich --backfill`` populates them.
--
-- Three nullable columns + one partial index on ``summary IS NULL`` so the
-- backfill iterator (``brain enrich --backfill``) can keyset-walk only the
-- unenriched subset. The index stays tiny once enrichment has run over the
-- corpus — only rows that genuinely failed enrichment (short content,
-- Ollama unavailable, repeated parse failures) remain in it. Mirrors the
-- ``idx_documents_thread_id`` shape from migration 007.

BEGIN;

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS summary       TEXT,
    ADD COLUMN IF NOT EXISTS summary_model TEXT,
    ADD COLUMN IF NOT EXISTS summary_at    TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_documents_summary_null
    ON documents (id) WHERE summary IS NULL;

COMMIT;
