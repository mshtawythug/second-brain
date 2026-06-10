-- 018_review_gap_signal_kinds.sql
-- Additive + idempotent. Extends elicitation_gaps.signal_kind CHECK to the full
-- superset of all known signal kinds so no later migration needs to rewrite it.
-- Plan 03 adds 'stale'; Plan 08 (migration 019) depends on 'search_failure'
-- already being valid here, so this migration ships the complete six-value set.
-- Uses the same DO-block guard pattern as migration 015.
-- NEVER edit shipped migrations 001-017.

BEGIN;

DO $$
BEGIN
    -- Drop the four-value constraint from 017 if it still exists.
    -- (The inline CHECK from CREATE TABLE gets the auto-generated name below.)
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'elicitation_gaps_signal_kind_check'
          AND conrelid = 'elicitation_gaps'::regclass
    ) THEN
        ALTER TABLE elicitation_gaps
            DROP CONSTRAINT elicitation_gaps_signal_kind_check;
    END IF;

    -- Add the six-value superset constraint under a versioned name.
    -- Idempotent: no-op if already present from a prior run of this migration.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'elicitation_gaps_signal_kind_v2_check'
          AND conrelid = 'elicitation_gaps'::regclass
    ) THEN
        ALTER TABLE elicitation_gaps
            ADD CONSTRAINT elicitation_gaps_signal_kind_v2_check
            CHECK (signal_kind IN (
                'delta', 'orphan', 'contradiction', 'user_flagged',
                'stale', 'search_failure'
            ));
    END IF;
END$$;

COMMIT;
