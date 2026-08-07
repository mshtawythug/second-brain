-- 026_document_sensitivity.sql — per-document sensitivity tier (trust boundary).
--
-- Additive only. Every existing row becomes 'normal' via the column DEFAULT,
-- which is exactly the pre-migration behaviour (no boundary refuses anything),
-- so this migration cannot change the result of any existing query.
--
-- TWO LEVELS BY DESIGN (spec F4-F6 section 5.6): 'normal' | 'confidential'.
-- Three levels would imply a lattice, and a lattice needs comparison
-- operators, per-boundary thresholds, and a policy language -- none of which a
-- single-user local knowledge base has any use for. Each egress boundary asks
-- exactly one question ("may this body leave?"), so exactly one bit is needed.
--
-- TEXT + a NAMED CHECK rather than BOOLEAN: a future third level then becomes a
-- named-constraint swap in a LATER migration -- never an edit to this file --
-- instead of a column type change. 'normal' (not FALSE) is the default so the
-- value reads correctly in vault frontmatter and in JSON payloads.
--
-- The Python mirror of this constraint is brain.sensitivity.SensitivityLevel /
-- VALID_SENSITIVITY_LEVELS. The two are pinned together by
-- tests/test_migration_026_sensitivity.py, which reads pg_get_constraintdef off
-- documents_sensitivity_check and asserts it covers exactly the Python set.
-- That test exists because this exact shape drifted once before: migration 024
-- was needed only because interactions.py mirrored a SQL CHECK in a Python
-- frozenset and the Python side raised BEFORE the INSERT, so repairing the SQL
-- alone looked correct and was not.
--
-- The partial index mirrors idx_documents_draft from migration 007: the
-- confidential subset is expected to stay small, and every consumer
-- (the hosted-embedder veto, `brain list --sensitivity confidential`, the
-- export frontmatter writer) filters on equality to 'confidential'.
--
-- Does NOT touch documents.content or chunks.content, so the GENERATED ALWAYS
-- chunks.tsv is never rewritten by this migration.
--
-- Re-runnable: ADD COLUMN IF NOT EXISTS is a no-op on a second apply;
-- DROP CONSTRAINT IF EXISTS before ADD CONSTRAINT is what makes the constraint
-- re-appliable, because PostgreSQL 16 has no ADD CONSTRAINT IF NOT EXISTS form;
-- CREATE INDEX IF NOT EXISTS is a no-op.
--
-- NEVER edit shipped migrations 001-025.

BEGIN;

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS sensitivity TEXT NOT NULL DEFAULT 'normal';

ALTER TABLE documents
    DROP CONSTRAINT IF EXISTS documents_sensitivity_check;

ALTER TABLE documents
    ADD CONSTRAINT documents_sensitivity_check
    CHECK (sensitivity IN ('normal', 'confidential'));

CREATE INDEX IF NOT EXISTS idx_documents_sensitivity
    ON documents (sensitivity) WHERE sensitivity <> 'normal';

COMMIT;
