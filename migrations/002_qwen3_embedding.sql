-- Phase 2 of Voyage→Qwen3 swap. The user has explicitly approved discarding
-- existing Voyage embeddings; chunks.content is preserved so Phase 3's
-- `brain reembed` can repopulate the new column from the original chunk text.
--
-- The NOT NULL constraint is deferred to post-backfill — `brain reembed --finalize`
-- applies it once 0 NULL rows remain. No vector index is created: pgvector 0.8.2
-- caps both HNSW and IVFFlat at 2000 dims for `vector` and 4000 for `halfvec`,
-- neither of which fits our native 4096-dim Qwen3 output. Sequential scan is
-- acceptable at personal scale (~150ms @ 10K chunks).

BEGIN;

DROP INDEX IF EXISTS chunks_embedding_idx;
ALTER TABLE chunks DROP COLUMN embedding;
ALTER TABLE chunks ADD COLUMN embedding vector(4096);

COMMIT;
