-- engagement_explain.sql — time the engagement stage's one statement on YOUR data (RM-22b).
--
-- An adoption condition for the engagement stage (plan
-- docs/plans/2026-11-12-rm-22b-engagement-stage.md, ruling D2): the statement
-- brain/search_engagement.py runs once per search must stay at or under 20 ms
-- at p95 on the owner's own data before BRAIN_ENGAGEMENT_ALPHA / _BETA are
-- turned on. Above that, the spec's materialized table (RM-22b.D2) is owed first.
--
-- READ ONLY. It runs inside `BEGIN READ ONLY ... ROLLBACK`, so Postgres refuses
-- any write, and it creates nothing (no table, no prepared statement). It prints
-- NOTICE lines only: the timing over 200 runs, then one EXPLAIN (ANALYZE, BUFFERS).
-- No title, query or content is printed; document ids appear in the plan's
-- Index Cond only.
--
-- Run it yourself, with psql, against the database you want measured:
--
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/engagement_explain.sql
--
-- Each run looks up 50 documents (one search's candidates), drawn at random from
-- those with any interaction, so it measures the costly case; the session is NULL,
-- as in `brain eval`, so no read is excluded by session. Read the "p95" line.
--
-- The statement text below must equal _ENGAGEMENT_SQL in
-- src/brain/search_engagement.py, with %(ids)s, %(reads)s, %(session)s,
-- %(half_life_s)s and %(window_days)s as $1..$5;
-- tests/test_engagement_explain_script.py fails when the two drift.

BEGIN READ ONLY;

DO $engagement$
DECLARE
    stmt CONSTANT text := $q$
    SELECT i.document_id::text,
           COALESCE(SUM(
               power(0.5::float8,
                     GREATEST(EXTRACT(EPOCH FROM NOW() - i.at)::float8, 0) / $4)
           ) FILTER (
               WHERE i.action = ANY($2)
                 AND i.at >= NOW() - make_interval(days => $5)
                 AND (i.session_id IS NULL
                      OR (i.session_id IS DISTINCT FROM $3::uuid
                          AND EXISTS (SELECT 1 FROM search_queries s
                                      WHERE s.session_id = i.session_id)))
           ), 0),
           COUNT(*) FILTER (WHERE i.action = 'rated_useful'),
           COUNT(*) FILTER (WHERE i.action = 'rated_irrelevant')
    FROM interactions i
    WHERE i.document_id = ANY($1::uuid[])
    GROUP BY i.document_id
$q$;
    reads CONSTANT text[] := ARRAY['opened', 'clicked'];
    half_life_s CONSTANT float8 := 30 * 86400;
    window_days CONSTANT int := 300;
    runs CONSTANT int := 200;
    candidates CONSTANT int := 50;
    engaged uuid[];
    ids uuid[];
    started timestamptz;
    ms float8[] := '{}';
    sorted float8[];
    line text;
BEGIN
    SELECT array_agg(DISTINCT document_id) INTO engaged
    FROM interactions WHERE document_id IS NOT NULL;
    IF engaged IS NULL THEN
        RAISE NOTICE 'engagement: no document interactions yet; nothing to time';
        RETURN;
    END IF;
    FOR run IN 1..runs LOOP
        SELECT array_agg(d) INTO ids
        FROM (SELECT d FROM unnest(engaged) d ORDER BY random() LIMIT candidates) s;
        started := clock_timestamp();
        EXECUTE stmt USING ids, reads, NULL::uuid, half_life_s, window_days;
        ms := ms || (EXTRACT(EPOCH FROM clock_timestamp() - started) * 1000);
    END LOOP;
    SELECT array_agg(x ORDER BY x) INTO sorted FROM unnest(ms) x;
    RAISE NOTICE 'engagement: documents with interactions=%, runs=%, candidates=%',
        cardinality(engaged), runs, least(candidates, cardinality(engaged));
    RAISE NOTICE 'engagement: p50=% ms p95=% ms max=% ms (adoption needs p95 <= 20 ms)',
        round(sorted[ceil(runs * 0.50)::int]::numeric, 2),
        round(sorted[ceil(runs * 0.95)::int]::numeric, 2),
        round(sorted[runs]::numeric, 2);
    FOR line IN EXECUTE 'EXPLAIN (ANALYZE, BUFFERS) ' || stmt
        USING ids, reads, NULL::uuid, half_life_s, window_days
    LOOP
        RAISE NOTICE '%', line;
    END LOOP;
END
$engagement$;

ROLLBACK;
