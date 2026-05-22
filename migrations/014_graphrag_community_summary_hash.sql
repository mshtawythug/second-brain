-- Migration 014 — GraphRAG community summary staleness tracker (wave G3-c).
--
-- Additive + idempotent. Adds ONE nullable column to ``graph_communities``:
--
--   summary_members_hash TEXT NULL
--       The ``members_hash`` value that the community's CURRENT ``summary`` was
--       generated from. The eager summary pass (§17c Q10,
--       ``brain.graph_rag.communities_summary.summarize_communities``) treats a
--       community as NEEDING a (re)summary when
--           ``summary IS NULL OR summary_members_hash IS DISTINCT FROM members_hash``
--       i.e. a never-summarized community OR one whose membership changed since
--       its last summary. The G3-b delta-gate
--       (``brain.graph_rag.communities._update_reused_community``) MOVES
--       ``members_hash`` on a membership change while PRESERVING the old summary;
--       this column lets that staleness be detected WITHOUT ever blanking the
--       live summary — the existing summary stays queryable until a fresh one
--       replaces it.
--
-- ``IS DISTINCT FROM`` (not ``<>``) is what makes the predicate NULL-safe: a row
-- whose ``summary_members_hash`` is NULL (every row right after this migration,
-- and every freshly-minted community) is correctly seen as stale relative to its
-- non-NULL ``members_hash`` and gets summarized on the next pass.
--
-- No new index: the staleness scan is tenant-scoped over the (small) community
-- set, already covered by the ``(tenant_id, level)`` index from migration 013;
-- a dedicated index would not pay for itself at personal-corpus community counts.

BEGIN;

ALTER TABLE graph_communities
    ADD COLUMN IF NOT EXISTS summary_members_hash TEXT NULL;

COMMIT;
