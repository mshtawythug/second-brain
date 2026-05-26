-- Migration 016: index hygiene — drop dead indexes, add two missing ones.
--
-- Background: ``brain doctor`` (wave perf/waves-0-2) added a stale-stats
-- probe that exposed several indexes that have never been used (``idx_scan = 0``
-- across the entire lifetime of the prod DB). All eight were verified read-only
-- on 2026-05-25 before inclusion here.
--
-- This migration is additive in the sense that it only REMOVES indexes
-- (never user data) and ADDS two new ones. All operations are idempotent:
-- ``DROP INDEX IF EXISTS`` is a no-op when the index is already gone;
-- ``CREATE INDEX IF NOT EXISTS`` is a no-op when it already exists.
--
-- 1. documents_tsv_idx — GIN on documents.tsv (dead since migration 009 moved
--    FTS to the weighted chunks.tsv; the documents.tsv column is still present
--    for schema compatibility but no query plan uses this index).
--
-- 2. Seven additional dead indexes (idx_scan = 0 on prod as of 2026-05-25):
--      derived_links_rule_idx          (on derived_links.rule)
--      documents_tags_idx              (GIN on documents.tags)
--      directory_entries_email_idx     (on directory_entries.email)
--      idx_documents_draft             (on documents.draft)
--      idx_documents_sent_at           (on documents.sent_at)
--      idx_documents_thread_id         (on documents.thread_id)
--      uq_documents_gmail_thread       (unique partial on documents.thread_id)
--
--    Note: uq_documents_gmail_thread is a UNIQUE constraint implemented as an
--    index (migration 008). Dropping it removes the uniqueness constraint too.
--    The Python ingest layer enforces this via ``ON CONFLICT`` logic, not
--    by relying on the constraint at write time, so dropping it is safe.
--
-- 3. NEW: GIN index on documents.participants — the ``&&`` array-overlap
--    operator used by the --person filter (Q1-C) currently forces a seq-scan.
--
-- 4. NEW: partial btree index on documents.source_path — the file-ingest
--    dedup path runs ``WHERE source_path = %s`` and currently seq-scans the
--    whole documents table. Scoped to IS NOT NULL rows to stay compact (stdin
--    ingests have source_path NULL and are deduped by content_hash).

BEGIN;

-- Drop dead indexes

DROP INDEX IF EXISTS documents_tsv_idx;

DROP INDEX IF EXISTS derived_links_rule_idx;
DROP INDEX IF EXISTS documents_tags_idx;
DROP INDEX IF EXISTS directory_entries_email_idx;
DROP INDEX IF EXISTS idx_documents_draft;
DROP INDEX IF EXISTS idx_documents_sent_at;
DROP INDEX IF EXISTS idx_documents_thread_id;
DROP INDEX IF EXISTS uq_documents_gmail_thread;

-- Add missing indexes

-- GIN for ``documents.participants && ARRAY[...]`` array-overlap queries
-- (the --person filter in Q1-C; currently a full seq-scan).
CREATE INDEX IF NOT EXISTS documents_participants_idx
    ON documents USING GIN (participants);

-- Partial btree for file-ingest dedup: ``WHERE source_path = %s``.
-- Restricts to IS NOT NULL so the index never covers stdin rows (which use
-- content_hash dedup instead), keeping the index small.
CREATE INDEX IF NOT EXISTS documents_source_path_idx
    ON documents (source_path)
    WHERE source_path IS NOT NULL;

COMMIT;
