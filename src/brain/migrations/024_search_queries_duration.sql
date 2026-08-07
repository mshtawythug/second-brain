-- 024_search_queries_duration.sql — retrieval latency + the 'ui' surface.
--
-- PAYLOAD 1: search_queries.duration_ms. F5 instruments the search path with
-- perf_counter phases; persisting the total lets `brain usage` (F7) answer
-- "is retrieval getting slower" without re-instrumenting anything. Nullable
-- INT: every pre-024 row keeps NULL, which every consumer must read as
-- "not measured", NEVER as 0 ms.
--
-- PAYLOAD 2: widen the surface enum on BOTH telemetry tables to admit 'ui'.
-- F14 (`brain ui`) is a fourth surface and has no migration allocated to it;
-- 024 is already the migration touching search_queries, and the release
-- spec's allocation table forbids sections inventing numbers.
--
-- This is a CORRECTNESS fix, not a nicety. brain.gaps.record_search_query
-- swallows ONLY OperationalError, UndefinedTable, and an UndefinedColumn
-- narrowed to the known additive columns; its docstring states that any other
-- schema error propagates. A CheckViolation is an IntegrityError, matches no
-- handler, and escapes -- so without this widening EVERY search issued through
-- the UI returns a 500. The interactions side fails even earlier: the Python
-- gate in brain.interactions raises InteractionError before the INSERT is
-- attempted, so the Python mirror (_VALID_SOURCES) must widen in lockstep.
--
-- Migrations 010 and 019 declare their CHECKs inline and unnamed, so
-- PostgreSQL auto-generated `interactions_source_check` and
-- `search_queries_source_check`. PostgreSQL 16 has no
-- ADD CONSTRAINT IF NOT EXISTS (see the note in migration 015), so each
-- constraint is dropped by its auto-generated name and re-added under an
-- explicit name -- making this file re-runnable and every future widening a
-- named-constraint swap in a LATER migration.
--
-- Additive in effect: the new value set is a strict superset of the old, so
-- no existing row can violate it and no table is rewritten.
-- NEVER edit shipped migrations 001-023.

BEGIN;

ALTER TABLE search_queries ADD COLUMN IF NOT EXISTS duration_ms INT;

ALTER TABLE interactions   DROP CONSTRAINT IF EXISTS interactions_source_check;
ALTER TABLE interactions   DROP CONSTRAINT IF EXISTS interactions_source_allowed;
ALTER TABLE interactions   ADD  CONSTRAINT interactions_source_allowed
    CHECK (source IN ('cli', 'mcp', 'wiki', 'ui'));

ALTER TABLE search_queries DROP CONSTRAINT IF EXISTS search_queries_source_check;
ALTER TABLE search_queries DROP CONSTRAINT IF EXISTS search_queries_source_allowed;
ALTER TABLE search_queries ADD  CONSTRAINT search_queries_source_allowed
    CHECK (source IN ('cli', 'mcp', 'wiki', 'ui'));

COMMIT;
