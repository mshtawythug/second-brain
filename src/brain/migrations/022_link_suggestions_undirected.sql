-- Plan 07 follow-up — make `link_suggestions` pairs UNDIRECTED.
--
-- Additive/idempotent: alters the migration-020 table in place. No data is
-- destroyed beyond redundant mirror rows (see the dedup step below).
--
-- THE BUG THIS FIXES (verified in live QA): migration 020 keyed uniqueness on
-- the DIRECTED pair ``UNIQUE (source_doc_id, target_doc_id)``, so A→B and B→A
-- were stored as two separate rows. ``brain connect refresh`` scores every
-- source doc independently, so it wrote BOTH orientations of the same unordered
-- pair — ~52% of the live pending queue was mirror duplicates. Worse, accepting
-- (or rejecting) one orientation left its mirror ``pending``, forcing the
-- reviewer to action the same pair twice.
--
-- THE FIX: a suggested link pair is undirected for review purposes. We keep
-- ``source_doc_id`` / ``target_doc_id`` as the BEST-SCORING orientation (so the
-- ``accept --write`` vault writeback still appends the ``## See Also`` wikilink
-- into a sensible "source" doc), but enforce ONE row per UNORDERED pair via a
-- functional unique index on ``(LEAST(source, target), GREATEST(source,
-- target))``. The application upsert (``connect._upsert_suggestion``) targets
-- this index and only flips orientation when a strictly-better score arrives;
-- the mirror of an already accepted/rejected pair can no longer be inserted.
--
-- This supersedes migration 020's comment ("Directed pairs are distinct rows").
-- Migration 020 is frozen and left untouched, per the repo migration rules.

BEGIN;

-- Step 1 — collapse existing mirror rows to one canonical row per UNORDERED
-- pair BEFORE the unique index is added (otherwise index creation would fail on
-- the live corpus's 501 mirror pairs). For each unordered pair keep the single
-- highest-priority row and delete the rest:
--   1. status priority: accepted (3) > rejected (2) > pending (1)
--   2. then higher blended ``score``  (keeps the better-scoring orientation)
--   3. then lower ``id`` (stable, deterministic tie-break)
--
-- Consequences of this ordering, by case:
--   * pending + pending  → keep the better-scoring orientation, drop the mirror
--                          (the common ~52% case). Accepted/rejected untouched.
--   * pending + decided  → keep the decided (accepted/rejected) row, drop the
--                          stale pending mirror. NO accepted/rejected row is
--                          deleted here.
--   * decided + decided  → only reachable if the SAME pair was both accepted in
--                          one orientation and rejected/accepted in the other
--                          (contradictory historical decisions). The unique
--                          index cannot hold both, so exactly one must go; we
--                          keep accepted-over-rejected, then higher score. This
--                          is the ONLY path that can drop a decided row, and it
--                          only triggers on self-contradictory legacy data.
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY LEAST(source_doc_id, target_doc_id),
                            GREATEST(source_doc_id, target_doc_id)
               ORDER BY CASE status
                            WHEN 'accepted' THEN 3
                            WHEN 'rejected' THEN 2
                            ELSE 1
                        END DESC,
                        score DESC,
                        id ASC
           ) AS rn
    FROM link_suggestions
)
DELETE FROM link_suggestions
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

-- Step 2 — drop the directed unique constraint from migration 020. The new
-- functional index (Step 3) is strictly stronger, so this would otherwise be a
-- redundant, confusing second uniqueness rule and an ambiguous ON CONFLICT
-- inference target.
ALTER TABLE link_suggestions
    DROP CONSTRAINT IF EXISTS link_suggestions_source_doc_id_target_doc_id_key;

-- Step 3 — enforce ONE row per UNORDERED pair regardless of which doc is stored
-- as source vs target. ``LEAST`` / ``GREATEST`` over the two UUIDs canonicalize
-- the pair; the index value is identical for A→B and B→A. This is the inference
-- target for ``connect._upsert_suggestion``'s ON CONFLICT.
CREATE UNIQUE INDEX IF NOT EXISTS uq_link_suggestions_unordered_pair
    ON link_suggestions (
        LEAST(source_doc_id, target_doc_id),
        GREATEST(source_doc_id, target_doc_id)
    );

COMMIT;
