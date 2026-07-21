CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE sources (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind         TEXT NOT NULL,
  external_id  TEXT,
  metadata     JSONB NOT NULL DEFAULT '{}',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (kind, external_id)
);

CREATE TABLE documents (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id     UUID REFERENCES sources(id) ON DELETE CASCADE,
  title         TEXT NOT NULL,
  content       TEXT NOT NULL,
  content_hash  TEXT NOT NULL UNIQUE,
  content_type  TEXT NOT NULL,
  source_path   TEXT,
  tags          TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
  metadata      JSONB NOT NULL DEFAULT '{}',
  ingested_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  tsv           tsvector GENERATED ALWAYS AS
                  (to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content,''))) STORED
);
CREATE INDEX documents_tsv_idx    ON documents USING GIN (tsv);
CREATE INDEX documents_tags_idx   ON documents USING GIN (tags);
CREATE INDEX documents_source_idx ON documents(source_id);

CREATE TABLE chunks (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id  UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_index  INT NOT NULL,
  content      TEXT NOT NULL,
  embedding    vector(1024) NOT NULL,
  metadata     JSONB NOT NULL DEFAULT '{}',
  tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
  UNIQUE (document_id, chunk_index)
);
CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_tsv_idx       ON chunks USING GIN (tsv);
CREATE INDEX chunks_document_idx  ON chunks(document_id);
