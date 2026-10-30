-- 030_near_duplicates.sql — MinHash signatures and near-duplicate pairs (RM-19).
--
-- `brain dedup scan` signs every document whose `document_minhash.content_hash` is missing or
-- differs from `documents.content_hash` (128 MinHash values plus 32 LSH band hashes, all signed
-- 64-bit), finds candidates through the GIN index on `bands`, and records each verified pair
-- (estimated Jaccard >= 0.85) in `near_duplicates`. A document under the 50-word floor is stored
-- with empty arrays, so it is not revisited and never matches a band.
--
-- A pair is stored once, `doc_a < doc_b`. `status` is the owner's label: a scan writes new pairs
-- as `open`, never changes a status, and deletes only `open` pairs that no longer verify, so a
-- dismissed pair is never re-opened. Deleting either document deletes the pair and the
-- signature (ON DELETE CASCADE). Nothing here refuses, merges or deletes a document.
--
-- Additive: two new tables and an index, no change to an existing table.
-- Re-runnable: every statement is IF NOT EXISTS.
--
-- NEVER edit shipped migrations 001-029.

BEGIN;

CREATE TABLE IF NOT EXISTS document_minhash (
  document_id   UUID PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  content_hash  TEXT NOT NULL,          -- recompute when it differs from documents.content_hash
  signature     BIGINT[] NOT NULL,      -- 128 values
  bands         BIGINT[] NOT NULL,      -- 32 band hashes
  computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_document_minhash_bands ON document_minhash USING GIN (bands);

CREATE TABLE IF NOT EXISTS near_duplicates (
  doc_a       UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  doc_b       UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  similarity  REAL NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'dismissed', 'confirmed')),
  detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (doc_a, doc_b),
  CHECK (doc_a < doc_b)
);

COMMIT;
