"""Tests for the shared time-windowed activity reader (``brain.activity``).

Unit coverage of ``week_bounds`` (pure) + integration coverage of the three
windowed readers against the real Postgres test DB. Timestamps are set with
explicit UTC ``datetime`` values via direct UPDATE / INSERT so the window
boundaries are deterministic (the same pattern as ``test_search_recency_boost``).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import psycopg
import pytest

from brain.activity import (
    iter_activity_docs,
    iter_ingested_docs,
    recent_captures,
    week_bounds,
)

# A fixed window: ISO week 2026-W23 == Mon 2026-06-01 .. Sun 2026-06-07.
WIN_AFTER = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
WIN_BEFORE = datetime(2026, 6, 7, 23, 59, 59, tzinfo=UTC)
IN_WINDOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
BEFORE_WINDOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
AFTER_WINDOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)


def _set_ingested_at(conn: psycopg.Connection, doc_id: str, when: datetime) -> None:
    conn.execute(
        "UPDATE documents SET ingested_at = %s WHERE id = %s", (when, doc_id)
    )


def _add_interaction(conn: psycopg.Connection, doc_id: str, when: datetime) -> None:
    conn.execute(
        "INSERT INTO interactions (document_id, action, source, at) "
        "VALUES (%s, 'opened', 'cli', %s)",
        (doc_id, when),
    )


# ---------------------------------------------------------------------------
# week_bounds — pure unit
# ---------------------------------------------------------------------------


def test_week_bounds_midyear() -> None:
    start, end = week_bounds("2026-W23")
    assert start == datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 6, 7, 23, 59, 59, tzinfo=UTC)


def test_week_bounds_year_boundary() -> None:
    # ISO week 1 of 2026 starts on 2025-12-29 (the Monday of the week with the
    # first Thursday of 2026) and ends 2026-01-04.
    start, end = week_bounds("2026-W01")
    assert start == datetime(2025, 12, 29, 0, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 1, 4, 23, 59, 59, tzinfo=UTC)


def test_week_bounds_bad_format_raises() -> None:
    with pytest.raises(ValueError):
        week_bounds("not-a-week")


def test_week_bounds_out_of_range_week_raises() -> None:
    # Matches the regex but 2026 has no ISO week 99 → fromisocalendar raises.
    with pytest.raises(ValueError):
        week_bounds("2026-W99")


# ---------------------------------------------------------------------------
# iter_activity_docs — integration
# ---------------------------------------------------------------------------


def test_iter_activity_docs_window_and_order(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    busy = seed_doc(title="Busy doc", content="busy body", tags=["alpha"])
    quiet = seed_doc(title="Quiet doc", content="quiet body", tags=["beta"])
    medium = seed_doc(title="Medium doc", content="medium body")
    outside = seed_doc(title="Outside doc", content="outside body")

    # In-window interactions: busy=3, medium=2, quiet=1.
    for _ in range(3):
        _add_interaction(test_db, busy, IN_WINDOW)
    for _ in range(2):
        _add_interaction(test_db, medium, IN_WINDOW)
    _add_interaction(test_db, quiet, IN_WINDOW)
    # Two interactions outside the window (must be excluded).
    _add_interaction(test_db, outside, BEFORE_WINDOW)
    _add_interaction(test_db, outside, AFTER_WINDOW)

    docs = iter_activity_docs(test_db, after=WIN_AFTER, before=WIN_BEFORE)

    assert [d.document_id for d in docs] == [busy, medium, quiet]
    assert [d.interaction_count for d in docs] == [3, 2, 1]
    assert docs[0].title == "Busy doc"
    assert docs[0].tags == ["alpha"]


def test_iter_activity_docs_respects_limit(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    for i in range(4):
        doc = seed_doc(title=f"Doc {i}", content=f"body {i}")
        for _ in range(i + 1):
            _add_interaction(test_db, doc, IN_WINDOW)
    docs = iter_activity_docs(test_db, after=WIN_AFTER, before=WIN_BEFORE, limit=2)
    assert len(docs) == 2


def test_iter_activity_docs_empty_window(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    doc = seed_doc(title="Doc", content="solo body")
    _add_interaction(test_db, doc, BEFORE_WINDOW)
    assert iter_activity_docs(test_db, after=WIN_AFTER, before=WIN_BEFORE) == []


# ---------------------------------------------------------------------------
# iter_ingested_docs — integration
# ---------------------------------------------------------------------------


def test_iter_ingested_docs_window_and_order(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    in_window_ids = []
    for i in range(5):
        doc = seed_doc(title=f"In {i}", content=f"in body {i}")
        _set_ingested_at(test_db, doc, datetime(2026, 6, 2 + i, tzinfo=UTC))
        in_window_ids.append(doc)
    # Two outside the window.
    for i in range(2):
        doc = seed_doc(title=f"Out {i}", content=f"out body {i}")
        _set_ingested_at(test_db, doc, BEFORE_WINDOW)

    docs = iter_ingested_docs(test_db, after=WIN_AFTER, before=WIN_BEFORE)

    assert len(docs) == 5
    assert {d.document_id for d in docs} == set(in_window_ids)
    # Newest first.
    ingest_times = [d.ingested_at for d in docs]
    assert ingest_times == sorted(ingest_times, reverse=True)


def test_iter_ingested_docs_respects_limit(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    for i in range(4):
        doc = seed_doc(title=f"Doc {i}", content=f"body {i}")
        _set_ingested_at(test_db, doc, IN_WINDOW)
    docs = iter_ingested_docs(test_db, after=WIN_AFTER, before=WIN_BEFORE, limit=2)
    assert len(docs) == 2


def test_iter_ingested_docs_empty_window(test_db: psycopg.Connection) -> None:
    assert iter_ingested_docs(test_db, after=WIN_AFTER, before=WIN_BEFORE) == []


# ---------------------------------------------------------------------------
# recent_captures — integration
# ---------------------------------------------------------------------------


def test_recent_captures_returns_recent_docs(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    # Two recent (ingested_at defaults to NOW() at insert).
    seed_doc(title="Recent A", content="recent a body")
    seed_doc(title="Recent B", content="recent b body")
    # One ancient — far outside any sane since-hours window.
    old = seed_doc(title="Ancient", content="ancient body")
    _set_ingested_at(test_db, old, datetime(2020, 1, 1, tzinfo=UTC))

    rows = recent_captures(test_db, since_hours=24, limit=20)

    titles = {r.title for r in rows}
    assert "Recent A" in titles
    assert "Recent B" in titles
    assert "Ancient" not in titles


def test_recent_captures_respects_limit(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    for i in range(5):
        seed_doc(title=f"Doc {i}", content=f"body {i}")
    rows = recent_captures(test_db, since_hours=24, limit=2)
    assert len(rows) == 2
