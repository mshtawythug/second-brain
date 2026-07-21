-- 023_search_queries_fts_count.sql — record the FTS (lexical) leg hit count.
-- Plan 08 (`brain gaps`) design gap, found in live QA: hybrid search's VECTOR
-- leg always returns nearest neighbours, so an off-corpus query logs
-- result_count > 0 (filler) and the result_count = 0 detector never fires. The
-- FTS (lexical) leg matching ZERO chunks is the real "knowledge gap" signal:
-- the corpus has no lexical trace of the query. This column records that leg's
-- hit count so `brain gaps` can key off `fts_count = 0`.
--
-- Additive + idempotent. Adds ONLY the nullable ``fts_count`` column + one
-- partial index. Old rows keep ``fts_count IS NULL`` and the detector falls
-- back to the historical ``result_count = 0`` semantics for them. NULL (not 0)
-- is the right default: a 0 default would mislabel every legacy row as a
-- lexical miss. NEVER edit shipped migrations 001-021.
--
-- Numbered 023 to avoid colliding with the concurrently-shipping 022
-- (link_suggestions); this migration references NO 022 objects.

BEGIN;

ALTER TABLE search_queries ADD COLUMN IF NOT EXISTS fts_count INT;

-- Partial index for the lexical-miss hot path (the new headline gap signal),
-- mirroring the existing result_count = 0 partial index from migration 019.
CREATE INDEX IF NOT EXISTS search_queries_fts_zero_idx
    ON search_queries (tenant_id, at DESC)
    WHERE fts_count = 0;

COMMIT;
