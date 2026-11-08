-- 031_document_embeddings.sql — one vector per document, for the document-leg stage (RM-20).
--
-- `brain docembed backfill` embeds each document's `title + "\n" + summary`, or its title plus the
-- head of its content when it has no summary, and stores the vector here. The document-leg
-- search stage (default OFF, `BRAIN_DOC_LEG`) reads it to re-score the rows a search already
-- returned; it adds no candidates.
--
-- `basis` says which text was embedded and `basis_hash` is its SHA-256, so `--stale` can find a
-- vector whose text changed. `embedder` is the backend's identity (class, model, dim); the stage
-- ignores a row written by another embedder. `embedding` starts at the house dimension, 1024;
-- `brain init` resizes it for the active embedder while the table holds no vectors, as it does
-- `chunks.embedding`. No index beyond the primary key: the stage reads at most 50 rows by key.
-- Deleting a document deletes its vector (ON DELETE CASCADE).
--
-- Additive: one new table, no change to an existing one.
-- Re-runnable: IF NOT EXISTS.
--
-- NEVER edit shipped migrations 001-030.

BEGIN;

CREATE TABLE IF NOT EXISTS document_embeddings (
  document_id  UUID PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  embedding    vector(1024),
  basis        TEXT NOT NULL CHECK (basis IN ('title_summary', 'title_head')),
  basis_hash   TEXT NOT NULL,
  embedder     TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMIT;
