-- Migration 016: index hygiene — drop one provably dead index, add two missing.
--
-- All operations are idempotent:
-- ``DROP INDEX IF EXISTS`` is a no-op when already gone;
-- ``CREATE INDEX IF NOT EXISTS`` is a no-op when already present.
--
-- 1. DROP documents_tsv_idx — the only PROVABLY dead index.
--    Code-level evidence: migration 009 moved FTS to the weighted
--    chunks.tsv (multi-field tsvector). The documents.tsv column is still
--    present (generated, no migration cost to keep), but no search query
--    plan uses this index. It is a ~12 MB GIN that carries unnecessary
--    write-amplification on every document INSERT / UPDATE.
--
-- 2. DEFERRED — the following six filter indexes also showed idx_scan=0 at
--    the 2026-05-25 audit, but the prod stats were reset by the 2026-05-22
--    AGE cutover (pg_restore). Three days of observations is too short a
--    window to call filter indexes "dead":
--
--      derived_links_rule_idx        (on derived_links.rule)
--      documents_tags_idx            (GIN on documents.tags)
--      directory_entries_email_idx   (on directory_entries.email)
--      idx_documents_draft           (on documents.draft)
--      idx_documents_sent_at         (on documents.sent_at)
--      idx_documents_thread_id       (on documents.thread_id)
--
--    Revisit after ~30 days of post-cutover prod traffic. Drop in a
--    migration 017 if idx_scan remains 0 at that point.
--
-- 3. NEVER DROP uq_documents_gmail_thread — it is a UNIQUE constraint
--    (indisunique=True, migration 008) that enforces the invariant
--    "one email_thread row per thread_id". UNIQUE indexes legitimately
--    show idx_scan=0 because uniqueness enforcement does not increment
--    idx_scan. Dropping it would silently remove a data-integrity guard.
--
-- 4. NEW: GIN index on documents.participants — the ``&&`` array-overlap
--    operator used by the --person filter (Q1-C) forces a seq-scan without
--    this index.
--
-- 5. NEW: partial btree index on documents.source_path — the file-ingest
--    dedup path (``WHERE source_path = %s``) forces a seq-scan without it.
--    Scoped to IS NOT NULL rows (stdin ingests use content_hash dedup and
--    have source_path NULL), keeping the index small.

BEGIN;

-- Drop the single provably-dead index (FTS moved to chunks.tsv in 009).
DROP INDEX IF EXISTS documents_tsv_idx;

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
