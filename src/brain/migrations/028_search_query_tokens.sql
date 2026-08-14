-- 028_search_query_tokens.sql — what a retrieval call actually COST the caller.
--
-- Nothing token-shaped existed in the schema before this (verified: zero hits
-- for "token" across migrations 001-027). `brain recall` computed
-- ``used_tokens`` and threw it away, so the one token-measured surface in the
-- system could not report a trend.
--
-- TWO columns, not one, because the distinction is the whole point:
--
--   payload_tokens  — MEASURED. The exact ``cl100k_base`` count of the
--                     CANONICAL serialization —
--                     ``json.dumps(payload, ensure_ascii=False)`` — of the
--                     artifact the surface produced. NOT a per-surface
--                     delivery measurement: no agent-facing surface emits
--                     exactly these bytes (Rich's ``console.print_json`` and
--                     the MCP text block both re-serialize with ``indent=2``,
--                     adding roughly 10% of indentation on a 5-result list).
--                     Counting the canonical form on purpose, so CLI and MCP
--                     rows stay comparable with each other and with the
--                     Wave-0 harness — see the note in
--                     ``scripts/token_payload_report.py`` ("What the number
--                     is"). One exception, and it is a real one: the default
--                     ``brain recall`` output is plain text via
--                     ``typer.echo``, so there the count IS delivered-exact.
--                     NULL when not measured (e.g. a human terminal search,
--                     which delivers a Rich table and not a payload).
--   baseline_tokens — COUNTERFACTUAL. What the SAME call would have cost in
--                     the default (non-brief) mode. NULL unless a cheaper mode
--                     was in effect, so a savings figure can only ever be
--                     computed over rows where BOTH are present. A flat
--                     "we save N%" over rows that never had an alternative is
--                     marketing, not measurement.
--
-- Both nullable INT with no default and no CHECK: every pre-028 row is
-- honestly NULL, and a range CHECK mirroring a Python bound is exactly the
-- drift migration 024 exists to remember (see 027's note). One gate, at the
-- Python boundary (``brain.gaps._validate_token_columns``), tested there --
-- it rejects a negative count AND a baseline offered without a payload, the
-- latter being the invariant that keeps the counterfactual from being
-- fabricated.
--
-- No index: the `brain usage` rollups already scan the
-- (tenant_id, at) window these columns ride along in. NEVER edit shipped
-- migrations 001-027.
--
-- Nullable-INT-no-default is a catalog-only change on PostgreSQL 11+, so
-- neither ALTER rewrites the table.

BEGIN;

ALTER TABLE search_queries ADD COLUMN IF NOT EXISTS payload_tokens  INT;
ALTER TABLE search_queries ADD COLUMN IF NOT EXISTS baseline_tokens INT;

COMMIT;
