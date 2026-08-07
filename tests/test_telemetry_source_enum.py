"""The ``'ui'`` telemetry surface must be admitted by SQL *and* Python.

``brain ui`` (F14) is a fourth surface alongside cli / mcp / wiki. It fails in
two independent places from one root cause, so both halves are asserted here:

* ``search_queries.source`` / ``interactions.source`` carry a SQL ``CHECK``.
  A ``CheckViolation`` is an ``IntegrityError``, which
  :func:`brain.gaps.record_search_query` deliberately does NOT swallow — so a
  too-narrow CHECK makes every UI search raise.
* :data:`brain.interactions._VALID_SOURCES` mirrors that CHECK in Python and
  raises :class:`InteractionError` *before* the INSERT is attempted — so a
  too-narrow frozenset silently rejects every UI document-open and rating.

The lockstep test at the bottom is what the "Keep both in lockstep" comment in
``brain/interactions.py`` has always asked for and nothing previously enforced.

All rows are synthetic.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

import psycopg
import pytest

from brain.errors import InteractionError
from brain.gaps import record_search_query
from brain.interactions import _VALID_SOURCES, record_interaction

_CONSTRAINT_VALUE_RE = re.compile(r"'([^']+)'")


def _constraint_values(conn: psycopg.Connection[Any], name: str) -> set[str]:
    """Read the quoted literals out of a live CHECK constraint definition."""
    row = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
        (name,),
    ).fetchone()
    assert row is not None, f"constraint {name!r} not found"
    return set(_CONSTRAINT_VALUE_RE.findall(str(row[0])))


def test_ui_source_is_accepted_by_both_tables(
    test_db: psycopg.Connection[Any], seed_doc: Any
) -> None:
    """RED-FIRST: the F14 blocker, reproduced on both telemetry tables."""
    # Arrange
    doc_id = seed_doc(title="Synthetic UI surface note")

    # Act — neither call may raise.
    record_search_query(
        test_db,
        query="synthetic ui query",
        result_count=1,
        fts_count=1,
        session_id=uuid.uuid4(),
        source="ui",
    )
    interaction_id = record_interaction(
        test_db, document_id=doc_id, action="opened", source="ui"
    )

    # Assert
    assert interaction_id
    row = test_db.execute(
        "SELECT count(*) FROM search_queries WHERE source = 'ui'"
    ).fetchone()
    assert row is not None and row[0] == 1
    row = test_db.execute(
        "SELECT count(*) FROM interactions WHERE source = 'ui'"
    ).fetchone()
    assert row is not None and row[0] == 1


def test_valid_sources_matches_the_live_sql_constraint(
    test_db: psycopg.Connection[Any]
) -> None:
    """THE LOCKSTEP INVARIANT the 'keep both in sync' comment always asked for.

    Widening the SQL ``CHECK`` without widening ``_VALID_SOURCES`` silently
    rejects the new surface in Python; widening ``_VALID_SOURCES`` without the
    migration lets a caller through only to trip a ``CheckViolation`` at
    INSERT. Either drift turns this red.
    """
    # Arrange / Act
    sql_values = _constraint_values(test_db, "search_queries_source_allowed")

    # Assert
    assert sql_values == set(_VALID_SOURCES)


def test_both_tables_admit_the_same_source_values(
    test_db: psycopg.Connection[Any]
) -> None:
    """The two telemetry tables must not diverge from each other either."""
    # Arrange / Act
    interactions = _constraint_values(test_db, "interactions_source_allowed")
    searches = _constraint_values(test_db, "search_queries_source_allowed")

    # Assert
    assert interactions == searches == set(_VALID_SOURCES)


def test_unknown_source_is_rejected_by_the_python_gate(
    test_db: psycopg.Connection[Any], seed_doc: Any
) -> None:
    """Widening for 'ui' must not have opened the gate to anything."""
    # Arrange
    doc_id = seed_doc(title="Synthetic gate note")

    # Act / Assert
    with pytest.raises(InteractionError, match="unknown source"):
        record_interaction(
            test_db,
            document_id=doc_id,
            action="opened",
            source="telepathy",  # type: ignore[arg-type]
        )


def test_unknown_source_is_rejected_by_the_sql_check(
    test_db: psycopg.Connection[Any]
) -> None:
    """The DB remains the authoritative gate, below the Python one."""
    # Act / Assert
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO search_queries (query, result_count, source) "
            "VALUES (%s, %s, %s)",
            ("synthetic bad-source query", 0, "telepathy"),
        )


def test_check_violation_is_not_swallowed_by_record_search_query(
    test_db: psycopg.Connection[Any]
) -> None:
    """An unknown surface is a code bug, not migration lag — it must surface.

    This is exactly why the too-narrow CHECK made every UI search 500 rather
    than degrade: ``record_search_query`` swallows only ``OperationalError``,
    ``UndefinedTable`` and a narrowed ``UndefinedColumn``. A ``CheckViolation``
    is an ``IntegrityError`` and matches none of them.
    """
    # Act / Assert
    with pytest.raises(psycopg.errors.CheckViolation):
        record_search_query(
            test_db,
            query="synthetic bad-source query",
            result_count=0,
            session_id=None,
            source="telepathy",
        )


def test_duration_ms_round_trips(test_db: psycopg.Connection[Any]) -> None:
    """``record_search_query`` persists the measured latency."""
    # Act
    record_search_query(
        test_db,
        query="synthetic timed query",
        result_count=2,
        fts_count=2,
        duration_ms=214,
        session_id=None,
        source="ui",
    )

    # Assert
    row = test_db.execute(
        "SELECT duration_ms FROM search_queries WHERE query = %s",
        ("synthetic timed query",),
    ).fetchone()
    assert row is not None and row[0] == 214


def test_duration_ms_defaults_to_null(test_db: psycopg.Connection[Any]) -> None:
    """Omitting the duration stores NULL ('not measured'), never 0."""
    # Act
    record_search_query(
        test_db,
        query="synthetic untimed query",
        result_count=1,
        session_id=None,
        source="cli",
    )

    # Assert
    row = test_db.execute(
        "SELECT duration_ms FROM search_queries WHERE query = %s",
        ("synthetic untimed query",),
    ).fetchone()
    assert row is not None and row[0] is None
