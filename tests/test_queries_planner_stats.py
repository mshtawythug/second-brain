"""Tests for :func:`brain.queries.planner_stats_state`.

Regression coverage for a ``brain doctor`` FALSE POSITIVE observed live: the
``chunks stats`` check read ``pg_stat_user_tables.last_analyze`` /
``n_live_tup`` to decide whether the table had ever been analyzed. Those live
in PostgreSQL's cumulative-statistics subsystem, which is discarded on crash
recovery, on an unclean shutdown, and on ``pg_stat_reset*()``. On the live
corpus that produced ``WARN - never analyzed (13 live rows, stats NULL)`` for a
table holding ~13,000 rows whose ``pg_statistic`` entries were fully populated
-- both the verdict and the row count were wrong.

``planner_stats_state`` reads the crash-durable catalogs (``pg_statistic`` /
``pg_class.reltuples``) instead, and treats the activity counters as recency
detail only.

All fixtures are synthetic; the real corpus is never touched.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import analyze_tables, planner_stats_state

# A schema name no production migration will ever create, so the shadowing test
# can only ever affect the object it made itself.
_SHADOW_SCHEMA = "brain_planner_stats_shadow_test"


def _seed_chunks(test_db: psycopg.Connection, fake_embedder: Any, *, body: str) -> None:
    """Ingest one synthetic doc so ``chunks`` is non-empty for ANALYZE."""
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="planner-stats seed",
            content=body,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    assert result.document_id is not None


def test_planner_stats_survive_activity_counter_reset(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Planner stats stay detected after the activity counters are wiped.

    THE regression. Reproduces the live false positive exactly: ANALYZE the
    table (planner statistics now exist), then reset the cumulative counters
    the way a crash-restart does, then re-probe.

    Setup: seed rows into ``chunks``, run a real ANALYZE.
    Exercise: reset ``chunks``'s counters, call ``planner_stats_state`` again.
    Verify: ``has_planner_stats`` is STILL True and ``estimated_rows`` is still
    positive (both crash-durable), while ``last_analyzed`` goes None (the
    counter really was wiped -- proving the reset took effect and that the old
    timestamp-only test would have reported "never analyzed" here).
    """
    test_db.autocommit = True
    _seed_chunks(test_db, fake_embedder, body="planner stats durable body")
    analyze_tables(test_db, ["chunks"])

    state = planner_stats_state(test_db, "chunks")
    assert state.exists is True
    assert state.has_rows is True
    assert state.has_planner_stats is True
    assert state.last_analyzed is not None
    assert state.estimated_rows > 0

    # Exactly what an unclean postmaster restart does to the stats subsystem.
    test_db.execute(
        "SELECT pg_stat_reset_single_table_counters('public.chunks'::regclass)"
    )

    after = planner_stats_state(test_db, "chunks")
    assert after.last_analyzed is None, (
        "counter reset did not take effect; test is vacuous"
    )
    assert after.has_planner_stats is True, (
        "regression: planner statistics reported absent after an activity-counter "
        "reset -- this is the live 'never analyzed' false positive"
    )
    assert after.estimated_rows > 0, (
        "regression: row estimate collapsed with the counters; reltuples is "
        "crash-durable and must survive"
    )
    assert after.has_rows is True, (
        "regression: the EXISTS probe must not follow the wiped counters -- "
        "reporting an empty table here is what silently skips the check"
    )


def test_planner_stats_absent_on_never_analyzed_table(
    test_db: psycopg.Connection,
) -> None:
    """POSITIVE CONTROL: the detector can still return False.

    Without this, ``has_planner_stats is True`` above could be passing
    vacuously -- a check that cannot fail is worse than no check. A freshly
    created table has no ``pg_statistic`` rows, so the probe must report
    absence.

    Uses a TEMP table: dropped automatically when the session ends, so it can
    never outlive the test.
    """
    test_db.autocommit = True
    test_db.execute("CREATE TEMP TABLE never_analyzed_probe (id int, payload text)")
    test_db.execute(
        "INSERT INTO never_analyzed_probe SELECT g, 'x' FROM generate_series(1, 50) g"
    )

    state = planner_stats_state(test_db, "never_analyzed_probe")

    assert state.exists is True
    assert state.has_planner_stats is False
    assert state.last_analyzed is None
    # reltuples is -1 ("unknown") on a never-analyzed table in PG14+; the helper
    # normalizes that to 0 rather than leaking a negative row count to callers.
    assert state.estimated_rows == 0
    # ...and yet the table demonstrably HAS rows. This pair is the whole point:
    # a caller that inferred emptiness from `estimated_rows == 0` would skip the
    # warning on precisely the table that needs it.
    assert state.has_rows is True

    # It flips to True once ANALYZE actually runs -- proving the False above is a
    # real observation about this table, not a quirk of temp tables.
    test_db.execute("ANALYZE never_analyzed_probe")
    after = planner_stats_state(test_db, "never_analyzed_probe")
    assert after.has_planner_stats is True
    assert after.estimated_rows > 0


def test_planner_stats_missing_table_returns_empty_state(
    test_db: psycopg.Connection,
) -> None:
    """A nonexistent relation reports absence instead of raising.

    ``to_regclass`` returns NULL rather than erroring, so doctor degrades to a
    warn line instead of crashing on a partially-migrated database.
    """
    test_db.autocommit = True

    state = planner_stats_state(test_db, "no_such_table_at_all")

    assert state.exists is False
    assert state.has_rows is False
    assert state.has_planner_stats is False
    assert state.last_analyzed is None
    assert state.estimated_rows == 0


def test_planner_stats_ignores_same_named_table_in_another_schema(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """A ``chunks`` in a non-search_path schema cannot shadow the real one.

    The replaced implementation matched ``WHERE relname = 'chunks'`` with no
    schema qualification, so any same-named relation elsewhere in the database
    (an Apache AGE label table, a scratch schema) could be picked up by
    ``fetchone()`` at random. ``to_regclass`` resolves through ``search_path``
    instead, which is how every other query in brain resolves its tables.

    Setup: a real analyzed ``public.chunks`` plus a decoy ``chunks`` in a
    dedicated schema that is NOT on the search_path.
    Exercise: probe ``"chunks"``.
    Verify: the answer describes public.chunks (has stats, rows > 0), not the
    never-analyzed decoy.
    """
    test_db.autocommit = True
    _seed_chunks(test_db, fake_embedder, body="planner stats shadow body")
    analyze_tables(test_db, ["chunks"])

    test_db.execute(f'CREATE SCHEMA IF NOT EXISTS "{_SHADOW_SCHEMA}"')
    try:
        test_db.execute(f'CREATE TABLE "{_SHADOW_SCHEMA}".chunks (id int)')

        state = planner_stats_state(test_db, "chunks")

        assert state.has_planner_stats is True
        assert state.last_analyzed is not None
        assert state.estimated_rows > 0
    finally:
        # Scoped to the uniquely-named schema this test created; touches nothing
        # else in the test database.
        test_db.execute(f'DROP SCHEMA IF EXISTS "{_SHADOW_SCHEMA}" CASCADE')


@pytest.mark.parametrize("table", ["chunks", "documents"])
def test_planner_stats_reports_both_doctor_tables(
    test_db: psycopg.Connection, fake_embedder: Any, table: str
) -> None:
    """Both tables auto-ANALYZEd after a bulk write are probeable by name."""
    test_db.autocommit = True
    _seed_chunks(test_db, fake_embedder, body=f"planner stats {table} body")
    analyze_tables(test_db, [table])

    state = planner_stats_state(test_db, table)

    assert state.exists is True
    assert state.has_rows is True
    assert state.has_planner_stats is True
    assert state.last_analyzed is not None
    assert state.estimated_rows > 0
