"""The one statement that reads a tag's documents for ``brain tags summarize`` (RM-55).

Pinned against the test DB (5434): the F6 lens, the draft filter, recency order, the limit, and
an exact tag match. Every row is synthetic.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from brain.tag_summary import fetch_tag_documents


def _doc(
    conn: psycopg.Connection[Any],
    title: str,
    *,
    tags: tuple[str, ...] = ("nebula",),
    summary: str | None = None,
    sensitivity: str = "normal",
    draft: bool = False,
    doc_date: datetime | None = None,
    ingested_at: datetime = datetime(2026, 1, 1, tzinfo=UTC),
) -> str:
    row = conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type, tags, summary,"
        " sensitivity, draft, doc_date, ingested_at)"
        " VALUES (%s, %s, %s, 'note', %s, %s, %s, %s, %s, %s) RETURNING id::text",
        (title, f"body {title}", f"hash-{title}", list(tags), summary, sensitivity, draft,
         doc_date, ingested_at),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _titles(conn: psycopg.Connection[Any], **kwargs: Any) -> list[str]:
    options: dict[str, Any] = {"limit": 20, "sensitivity": None, **kwargs}
    return [d.title for d in fetch_tag_documents(conn, "nebula", **options)]


def test_lens_normal_excludes_confidential(test_db: psycopg.Connection[Any]) -> None:
    _doc(test_db, "open note")
    _doc(test_db, "closed note", sensitivity="confidential")
    assert _titles(test_db, sensitivity="normal") == ["open note"]


def test_lens_none_includes_both_tiers(test_db: psycopg.Connection[Any]) -> None:
    _doc(test_db, "open note")
    _doc(test_db, "closed note", sensitivity="confidential")
    assert sorted(_titles(test_db)) == ["closed note", "open note"]


def test_drafts_are_excluded(test_db: psycopg.Connection[Any]) -> None:
    _doc(test_db, "unsent draft", draft=True)
    assert _titles(test_db) == []


def test_non_drafts_with_the_tag_are_included(test_db: psycopg.Connection[Any]) -> None:
    _doc(test_db, "unsent draft", draft=True)
    _doc(test_db, "sent note")
    assert _titles(test_db) == ["sent note"]


def test_order_is_discovery_date_desc_then_id(test_db: psycopg.Connection[Any]) -> None:
    _doc(test_db, "old event, new ingest", doc_date=datetime(2025, 1, 1, tzinfo=UTC),
         ingested_at=datetime(2026, 3, 1, tzinfo=UTC))
    _doc(test_db, "undated, mid ingest", ingested_at=datetime(2026, 2, 1, tzinfo=UTC))
    _doc(test_db, "newest event", doc_date=datetime(2026, 4, 1, tzinfo=UTC))
    a = _doc(test_db, "tie a", doc_date=datetime(2025, 6, 1, tzinfo=UTC))
    b = _doc(test_db, "tie b", doc_date=datetime(2025, 6, 1, tzinfo=UTC))
    tie = ["tie a", "tie b"] if a < b else ["tie b", "tie a"]
    assert _titles(test_db) == [
        "newest event", "undated, mid ingest", *tie, "old event, new ingest"
    ]


def test_limit_caps_the_fetch(test_db: psycopg.Connection[Any]) -> None:
    for day in range(1, 6):
        _doc(test_db, f"note {day}", doc_date=datetime(2026, 1, day, tzinfo=UTC))
    assert _titles(test_db, limit=2) == ["note 5", "note 4"]


def test_other_tags_are_not_matched(test_db: psycopg.Connection[Any]) -> None:
    _doc(test_db, "plural", tags=("nebulae",))
    _doc(test_db, "prefix", tags=("nebula-notes",))
    _doc(test_db, "exact among others", tags=("orbit", "nebula"))
    assert _titles(test_db) == ["exact among others"]


def test_rows_carry_the_summary_and_the_discovery_date(test_db: psycopg.Connection[Any]) -> None:
    when = datetime(2026, 2, 2, tzinfo=UTC)
    doc_id = _doc(test_db, "with summary", summary="zephyr routine", doc_date=when)
    (doc,) = fetch_tag_documents(test_db, "nebula", limit=5, sensitivity=None)
    assert (doc.document_id, doc.summary, doc.date) == (doc_id, "zephyr routine", when)


def test_a_tag_is_a_bound_parameter_not_sql(test_db: psycopg.Connection[Any]) -> None:
    _doc(test_db, "tagged", tags=("x' OR '1'='1",))
    _doc(test_db, "other", tags=("orbit",))
    docs = fetch_tag_documents(test_db, "x' OR '1'='1", limit=5, sensitivity=None)
    assert [d.title for d in docs] == ["tagged"]


@pytest.mark.parametrize("sensitivity", ["normal", None])
def test_an_unknown_tag_reads_nothing(
    test_db: psycopg.Connection[Any], sensitivity: str | None
) -> None:
    _doc(test_db, "tagged")
    assert fetch_tag_documents(test_db, "orbit", limit=5, sensitivity=sensitivity) == []
