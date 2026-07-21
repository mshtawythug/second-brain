-- Migration 009: title- and tag-weighted FTS for chunks.
-- See docs/plans/2026-05-06-search-ranking-fix.md.
--
-- Adds denormalized title/tags/extras columns on chunks and recomputes
-- chunks.tsv as a weighted multi-field tsvector so title/tag hits rank
-- ahead of body hits. Additive: title_text/tags_text/search_extras are
-- nullable; coalesce(...) inside the generated column keeps the tsv
-- well-defined for rows that haven't been backfilled yet.
--
-- The DROP COLUMN tsv is technically destructive but the column is
-- generated (no user data); it's rebuilt below from the new sources in
-- the same migration. The GIN index is recreated against the new tsv.

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS title_text     TEXT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tags_text      TEXT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS search_extras  TEXT;

DROP INDEX IF EXISTS chunks_tsv_idx;
ALTER TABLE chunks DROP COLUMN IF EXISTS tsv;

ALTER TABLE chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(title_text,    '')), 'A') ||
    setweight(to_tsvector('english', coalesce(tags_text,     '')), 'B') ||
    setweight(to_tsvector('english', coalesce(content,       '')), 'C') ||
    setweight(to_tsvector('english', coalesce(search_extras, '')), 'C')
) STORED;

CREATE INDEX chunks_tsv_idx ON chunks USING GIN (tsv);
