-- Vault model phase 1 — schema additions for the headless-Obsidian vault.
--
-- Additive only: every existing row gets ``kind='ingested'`` via the column
-- default (matches the legacy semantics where everything in the DB came from
-- an upstream source). ``links`` and ``unresolved_links`` are new; nothing in
-- prior migrations is altered or dropped.

BEGIN;

ALTER TABLE documents ADD COLUMN kind TEXT NOT NULL DEFAULT 'ingested'
  CHECK (kind IN ('vault', 'ingested'));

ALTER TABLE documents ADD COLUMN vault_path TEXT;
CREATE UNIQUE INDEX documents_vault_path_idx
  ON documents (vault_path) WHERE vault_path IS NOT NULL;
CREATE INDEX documents_kind_idx ON documents (kind);

CREATE TABLE links (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  src_document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  dst_document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  link_text       TEXT NOT NULL,
  link_kind       TEXT NOT NULL CHECK (link_kind IN ('wiki', 'embed')),
  display_text    TEXT,
  UNIQUE (src_document_id, dst_document_id, link_text, link_kind)
);
CREATE INDEX links_src_idx ON links (src_document_id);
CREATE INDEX links_dst_idx ON links (dst_document_id);

CREATE TABLE unresolved_links (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  src_document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  link_text       TEXT NOT NULL,
  link_kind       TEXT NOT NULL CHECK (link_kind IN ('wiki', 'embed')),
  display_text    TEXT,
  UNIQUE (src_document_id, link_text, link_kind)
);
CREATE INDEX unresolved_links_src_idx ON unresolved_links (src_document_id);
CREATE INDEX unresolved_links_text_idx ON unresolved_links (link_text);

COMMIT;
