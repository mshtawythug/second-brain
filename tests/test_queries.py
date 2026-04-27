"""Direct unit tests for ``brain.queries`` shared helpers.

The CLI and MCP server both call into these — they're covered indirectly by
the existing test suites, but these tests exercise the module's exception
hierarchy and the defensive ``None`` branch in :func:`fetch_document`
directly so the contract stays pinned.
"""
from typing import Any

import psycopg
import pytest

from brain.errors import (
    IdPrefixAmbiguous,
    IdPrefixNotFound,
    IdPrefixNotHex,
    IdPrefixTooShort,
)
from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import (
    fetch_document,
    list_documents,
    resolve_document_prefix,
)


def _seed(
    conn: psycopg.Connection, fake_embedder: Any, *, title: str = "t"
) -> str:
    result = ingest_document(
        conn,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title=title,
            content="alpha bravo body",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=[],
    )
    assert result.document_id is not None
    return result.document_id


def test_resolve_document_prefix_returns_full_id(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    doc_id = _seed(test_db, fake_embedder)
    assert resolve_document_prefix(test_db, doc_id[:8]) == doc_id


def test_resolve_document_prefix_too_short(
    test_db: psycopg.Connection,
) -> None:
    with pytest.raises(IdPrefixTooShort):
        resolve_document_prefix(test_db, "abc")


def test_resolve_document_prefix_non_hex(
    test_db: psycopg.Connection,
) -> None:
    with pytest.raises(IdPrefixNotHex):
        resolve_document_prefix(test_db, "abc_de%")


def test_resolve_document_prefix_not_found(
    test_db: psycopg.Connection,
) -> None:
    with pytest.raises(IdPrefixNotFound):
        resolve_document_prefix(test_db, "ffffff")


def test_resolve_document_prefix_ambiguous(
    test_db: psycopg.Connection,
) -> None:
    for new_id in (
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "aaaaaabb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ):
        test_db.execute(
            "INSERT INTO documents (id, title, content, content_hash, "
            "content_type) VALUES (%s, %s, %s, %s, %s)",
            (new_id, "t", "body", new_id + "_h", "note"),
        )
    with pytest.raises(IdPrefixAmbiguous):
        resolve_document_prefix(test_db, "aaaaaa")


def test_fetch_document_returns_none_for_missing_id(
    test_db: psycopg.Connection,
) -> None:
    """Defensive: caller may have raced; fetch returns ``None`` rather than crashing."""
    assert (
        fetch_document(test_db, "00000000-0000-0000-0000-000000000000") is None
    )


def test_list_documents_filters_round_trip(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Smoke test that the projection populates the expected DocumentRow fields."""
    doc_id = _seed(test_db, fake_embedder, title="Doc")
    rows = list_documents(test_db, limit=5)
    assert len(rows) == 1
    only = rows[0]
    assert only.id == doc_id
    assert only.title == "Doc"
    assert only.content_type == "note"
    assert only.tags == []
    # list projection omits the body + source_path.
    assert only.content is None
    assert only.source_path is None
