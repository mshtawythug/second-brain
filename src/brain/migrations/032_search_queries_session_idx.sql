-- 032_search_queries_session_idx.sql — an index for "does this session name a logged search?"
--
-- RM-22b's engagement stage (brain/search_engagement.py) counts a read that
-- carries a session only when that session names a logged search:
--
--     EXISTS (SELECT 1 FROM search_queries s WHERE s.session_id = i.session_id)
--
-- That is how it tells a person's read after a search from the `opened` row
-- `brain ask` writes for each citation under the ask's own session (plan D4,
-- RM-22a's rule in brain/engagement.py). `search_queries.session_id` had no
-- index (019 indexes `(tenant_id, at)` only), and `search_queries` is never
-- pruned, so the probe was a sequential scan per read row. Plan D5's benchmark
-- (1,000,000 interactions, 300,000 searches, 50 candidates x 200 runs) measured
-- p95 11.7 s, against a 20 ms budget; the EXPLAIN showed the SubPlan seq-scanning
-- search_queries 649 times. The stage ships off, so nothing was slow on any
-- install; the index has to exist before anyone turns it on.
--
-- Partial on `session_id IS NOT NULL`: the CLI path logs searches with a NULL
-- session, and the probe never looks one up, so those rows stay out of it.
--
-- Additive and idempotent (IF NOT EXISTS). Not CONCURRENTLY: `brain init` runs
-- each migration inside a transaction, where CONCURRENTLY is refused. A plain
-- CREATE INDEX takes a SHARE lock on search_queries for the build, which blocks
-- writers (a search's log row) but not readers, for well under a second at
-- personal scale. The 0.4.0 upgrade notes stop the writers first anyway.
-- NEVER edit shipped migrations 001-031.

BEGIN;

-- Fail fast rather than queue every later search behind this build when a
-- long transaction already holds a conflicting lock (028's reasoning). An
-- aborted `brain init` records nothing (the schema_migrations INSERT comes
-- after this file), and re-running it once the writer is stopped applies it.
SET LOCAL lock_timeout = '3s';

CREATE INDEX IF NOT EXISTS search_queries_session_idx
    ON search_queries (session_id)
    WHERE session_id IS NOT NULL;

COMMIT;
