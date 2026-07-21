-- Migration 015 — generalize ``interactions`` for graph-target feedback
-- (wave G4-a; spec docs/specs/2026-05-20-graphrag-design.md §17d Q2/Q5).
--
-- Additive + idempotent. Migration 010 created ``interactions`` as a
-- DOCUMENT-only feedback log (``document_id`` NOT NULL). G4 makes the graph
-- surfaces — entity / community / theme — FIRST-CLASS rateable targets while
-- keeping every existing document-only caller (``brain rate``, MCP
-- ``brain_show``) working unchanged. This migration:
--
--   1. ``document_id`` → NULLABLE (a graph-target row has no document).
--   2. ADD ``target_type TEXT NULL`` — one of 'entity' | 'community' | 'theme'.
--   3. ADD ``target_id   TEXT NULL`` — the durable id of the graph target
--      (entity UUID / community_key / theme key). TEXT (not UUID) because the
--      three target kinds do not share a single id type; the target_type names
--      which space the id lives in.
--   4. ADD ``graph_retrieved BOOLEAN NOT NULL DEFAULT FALSE`` — PROVENANCE only
--      (a graph surface produced this row). It is NOT part of the target model;
--      a document row surfaced via a graph path is still a document row with
--      ``graph_retrieved = TRUE``. Existing rows backfill to FALSE.
--
-- Two CHECK constraints (the authoritative gate; the Python writer mirrors them
-- in ``brain.interactions``):
--
--   * target_type domain — NULL, or one of the three known kinds.
--   * EXACTLY ONE target shape (XOR) — a row is EITHER a document row
--     (``document_id`` set, both target_* NULL) OR a graph-target row
--     (``document_id`` NULL, both target_* set). The two disjuncts are mutually
--     exclusive (one demands ``document_id`` NOT NULL, the other NULL), so their
--     OR is a true XOR. Every pre-015 row satisfies the first disjunct, so the
--     constraint validates cleanly against existing data.
--
-- An additive partial index on (target_type, target_id) supports graph-target
-- lookups; it excludes the document-only rows (target_type NULL) to stay small.
--
-- NEVER edit a shipped migration — this is a NEW numbered file. Migration 010
-- and all prior migrations are untouched.

BEGIN;

-- 1. document_id becomes nullable. Idempotent: DROP NOT NULL on an already
--    nullable column is a successful no-op.
ALTER TABLE interactions
    ALTER COLUMN document_id DROP NOT NULL;

-- 2-4. Additive columns. graph_retrieved backfills FALSE on existing rows.
ALTER TABLE interactions
    ADD COLUMN IF NOT EXISTS target_type     TEXT,
    ADD COLUMN IF NOT EXISTS target_id       TEXT,
    ADD COLUMN IF NOT EXISTS graph_retrieved BOOLEAN NOT NULL DEFAULT FALSE;

-- 5. target_type domain CHECK. Postgres has no ADD CONSTRAINT IF NOT EXISTS,
--    so guard on pg_constraint by name to keep re-runs safe.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'interactions_target_type_chk'
    ) THEN
        ALTER TABLE interactions
            ADD CONSTRAINT interactions_target_type_chk
            CHECK (target_type IS NULL
                   OR target_type IN ('entity', 'community', 'theme'));
    END IF;
END$$;

-- 6. XOR target-shape CHECK: document-only OR graph-target-only, never both,
--    never neither.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'interactions_target_xor_chk'
    ) THEN
        ALTER TABLE interactions
            ADD CONSTRAINT interactions_target_xor_chk
            CHECK (
                (document_id IS NOT NULL
                    AND target_type IS NULL AND target_id IS NULL)
                OR
                (document_id IS NULL
                    AND target_type IS NOT NULL AND target_id IS NOT NULL)
            );
    END IF;
END$$;

-- 7. Partial index for graph-target lookups (entity/community/theme rows only).
CREATE INDEX IF NOT EXISTS interactions_target_idx
    ON interactions (target_type, target_id)
    WHERE target_type IS NOT NULL;

COMMIT;
