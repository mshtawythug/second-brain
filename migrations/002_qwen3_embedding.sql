-- Phase 2 of Voyage→Qwen3 swap. The user has explicitly approved discarding
-- existing Voyage embeddings; chunks.content is preserved so Phase 3's
-- `brain reembed` can repopulate the new column from the original chunk text.
--
-- The HNSW index is NOT recreated here. Building HNSW on an all-NULL column
-- would be wasted work; `brain reembed` rebuilds the index after backfill.
-- The NOT NULL constraint is also deferred to post-backfill — also handled by
-- `brain reembed`.

BEGIN;

DROP INDEX IF EXISTS chunks_embedding_idx;
ALTER TABLE chunks DROP COLUMN embedding;
ALTER TABLE chunks ADD COLUMN embedding vector(4096);

COMMIT;
