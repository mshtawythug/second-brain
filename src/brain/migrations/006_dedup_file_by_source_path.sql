-- File-based ingests dedup by ``source_path`` (in code), not ``content_hash``.
--
-- Why: when a source file's bytes change on disk and we re-ingest, the new
-- content_hash differs from the prior row's, so the prior content_hash lookup
-- missed and a NEW row was inserted — leaving the corpus with two documents
-- for the same path. Triggering case: the markdown extractor fix in commit
-- 85a6394 changed extracted-content bytes for every existing markdown doc, so
-- every re-ingest would have silently duplicated.
--
-- Migration 004 introduced a partial UNIQUE index on ``content_hash`` scoped
-- to ``kind='ingested'``. That worked while file ingests deduped by hash, but
-- it conflicts with the new "two files with byte-identical content at
-- different paths are separate documents" semantic — the second INSERT would
-- fail. Tighten the partial predicate so the constraint only applies to
-- stdin ingests (``source_path IS NULL``: krisp / slack / gmail), where we
-- still want belt-and-suspenders protection against duplicate transcripts.

BEGIN;

DROP INDEX IF EXISTS documents_content_hash_ingested_idx;
CREATE UNIQUE INDEX IF NOT EXISTS documents_content_hash_stdin_idx
  ON documents (content_hash)
  WHERE kind = 'ingested' AND source_path IS NULL;

COMMIT;
