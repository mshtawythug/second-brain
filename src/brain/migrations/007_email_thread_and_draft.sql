-- Migration 007: email-thread metadata + draft flag
-- Additive only. Existing rows untouched.

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS thread_id        TEXT,
    ADD COLUMN IF NOT EXISTS rfc_message_id   TEXT,
    ADD COLUMN IF NOT EXISTS in_reply_to      TEXT,
    ADD COLUMN IF NOT EXISTS sent_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS participants     TEXT[],
    ADD COLUMN IF NOT EXISTS duration_min     INTEGER,
    ADD COLUMN IF NOT EXISTS draft            BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_documents_thread_id ON documents (thread_id) WHERE thread_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_documents_sent_at   ON documents (sent_at DESC) WHERE sent_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_documents_draft     ON documents (draft) WHERE draft = TRUE;
