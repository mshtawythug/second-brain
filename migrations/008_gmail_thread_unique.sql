-- Migration 008: enforce one merged-thread row per gmail thread_id.
-- Additive only. Existing rows untouched (no thread_id collision today since
-- the legacy ingest path keys on message_id; once P2.3 + P2.4 land, the
-- collapsed corpus will have exactly one content_type='email_thread' row
-- per thread and this index protects against accidental re-creation).

CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_gmail_thread
    ON documents (thread_id)
    WHERE kind = 'ingested'
      AND thread_id IS NOT NULL
      AND content_type = 'email_thread';
