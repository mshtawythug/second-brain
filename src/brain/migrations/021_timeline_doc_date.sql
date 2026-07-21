-- Migration 021 — optional `doc_date` generated column for `brain timeline`
-- (Plan 05, Phase 3). Slot 021 per the cross-plan assignment in
-- docs/plans/2026-06-03-brain-next-features/README.md (migrations 018/019/020
-- belong to Plans 03/08/07 respectively; 016/017 already shipped).
--
-- ADDITIVE ONLY. The timeline's temporal anchor is
-- COALESCE(documents.sent_at, documents.ingested_at) — event time when known
-- (emails, Krisp), else ingest time. Phases 1 and 2 compute that inline in
-- every query. This migration materializes it as a generated STORED column +
-- a B-tree index so the repeated COALESCE expression is replaced by a single
-- indexed read. `brain.timeline._doc_date_expr` auto-detects the column via
-- information_schema and falls back to the inline COALESCE when it is absent,
-- so applying this migration is purely an optimization — zero behavior change.
--
-- References only `documents` columns frozen by earlier shipped migrations
-- (sent_at + participants from 007; ingested_at from 001). It does NOT
-- reference any object from migrations 018–020 (which may not yet exist when
-- this file is applied — the runner tolerates name-order gaps).
--
-- ⚠ Generated-column caveat: `ADD COLUMN … GENERATED … STORED` rewrites the
-- `documents` table in Postgres. At personal-corpus scale (≤ 5 000 rows) this
-- completes in well under a second, but it is not zero-cost. Existing rows are
-- backfilled automatically by Postgres. Idempotent (`IF NOT EXISTS`).

BEGIN;

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS doc_date TIMESTAMPTZ
        GENERATED ALWAYS AS (COALESCE(sent_at, ingested_at)) STORED;

CREATE INDEX IF NOT EXISTS idx_documents_doc_date
    ON documents (doc_date DESC);

COMMIT;
