-- Plan 07 — `brain connect` proactive auto-link suggestions.
--
-- Additive only: nothing in prior migrations is altered or dropped. One new
-- table backs the proactive suggestion queue. Each row is a candidate
-- (source_doc → target_doc) wikilink the user has NOT yet drawn, scored by a
-- RRF blend of an entity-graph affinity leg and an embedding affinity leg
-- (see ``src/brain/connect.py``). The accept/reject state machine is a
-- DEDICATED concern, separate from the ``interactions`` log (migration 010):
-- a suggestion is ``pending`` until the user accepts (optionally writing the
-- wikilink into the source vault file) or rejects it.
--
-- The ``UNIQUE (source_doc_id, target_doc_id)`` constraint makes
-- ``brain connect refresh`` safely idempotent: re-running upserts the score on
-- ``pending`` rows only (``ON CONFLICT ... DO UPDATE ... WHERE status =
-- 'pending'``), so accepted/rejected rows are frozen and never resurface.
-- Directed pairs are distinct rows: A→B and B→A may both be suggested.

BEGIN;

CREATE TABLE IF NOT EXISTS link_suggestions (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_doc_id   UUID        NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    target_doc_id   UUID        NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    score           FLOAT       NOT NULL,
    graph_score     FLOAT,
    embed_score     FLOAT,
    suggested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'accepted', 'rejected')),
    actioned_at     TIMESTAMPTZ,
    CHECK (source_doc_id <> target_doc_id),
    UNIQUE (source_doc_id, target_doc_id)
);

CREATE INDEX IF NOT EXISTS idx_link_suggestions_status
    ON link_suggestions (status);
CREATE INDEX IF NOT EXISTS idx_link_suggestions_source
    ON link_suggestions (source_doc_id);

COMMIT;
