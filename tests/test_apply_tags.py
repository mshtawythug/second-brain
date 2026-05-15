"""Direct unit tests for ``brain.ingest.apply_tags``.

Covers the cases the CLI tag tests exercise indirectly: add-only, remove-only,
both add+remove, idempotent re-add, plus the unknown-document edge case.
The CLI/MCP wrappers each rely on this helper having stable semantics.
"""
from typing import Any

import psycopg
import pytest

from brain.ingest import ExtractedDoc, apply_tags, ingest_document


def _seed(
    conn: psycopg.Connection,
    fake_embedder: Any,
    *,
    tags: list[str] | None = None,
) -> str:
    result = ingest_document(
        conn,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="t",
            content="alpha bravo body",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=tags or [],
    )
    assert result.document_id is not None
    return result.document_id


def test_apply_tags_add_only(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    doc_id = _seed(test_db, fake_embedder, tags=["one"])
    final = apply_tags(test_db, doc_id, add=["two"])
    assert sorted(final) == ["one", "two"]


def test_apply_tags_remove_only(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    doc_id = _seed(test_db, fake_embedder, tags=["one", "two"])
    final = apply_tags(test_db, doc_id, remove=["one"])
    assert final == ["two"]


def test_apply_tags_add_and_remove_together(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    doc_id = _seed(test_db, fake_embedder, tags=["old"])
    final = apply_tags(test_db, doc_id, add=["new"], remove=["old"])
    assert final == ["new"]


def test_apply_tags_idempotent_re_add(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Re-adding an existing tag must not produce a duplicate."""
    doc_id = _seed(test_db, fake_embedder, tags=["one"])
    final = apply_tags(test_db, doc_id, add=["one"])
    assert final == ["one"]


def test_apply_tags_unknown_document_raises(
    test_db: psycopg.Connection,
) -> None:
    """A non-existent document id raises ValueError so callers can surface it."""
    with pytest.raises(ValueError, match="document not found"):
        apply_tags(
            test_db,
            "00000000-0000-0000-0000-000000000000",
            add=["x"],
        )


def test_apply_tags_no_op_returns_current_tags(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Calling with empty add+remove returns the current tag list unchanged."""
    doc_id = _seed(test_db, fake_embedder, tags=["one", "two"])
    final = apply_tags(test_db, doc_id)
    assert sorted(final) == ["one", "two"]


def test_apply_tags_normalizes_added_tag_casing(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Phase 3: brand-cased input is silently lowercased before the DB write.

    Effect: ``brain tag <id> +BrandName`` ends up storing ``brandname`` in
    ``documents.tags`` regardless of what the caller typed. We verify by
    SELECTing the column directly so we don't trust the function's return
    value alone.
    """
    doc_id = _seed(test_db, fake_embedder, tags=[])
    apply_tags(test_db, doc_id, add=["BrandName"])
    row = test_db.execute(
        "SELECT tags FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    assert list(row[0]) == ["brandname"]


def test_apply_tags_remove_matches_canonical_form_case_insensitive(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """A remove of ``BrandName`` matches a row stored as ``brandname``.

    Without normalization the SQL ``<> ALL`` comparator is case-sensitive
    and would silently no-op. The boundary normalization is what makes
    case-insensitive removal work.
    """
    doc_id = _seed(test_db, fake_embedder, tags=["brandname"])
    final = apply_tags(test_db, doc_id, remove=["BrandName"])
    assert final == []


def test_apply_tags_dedupes_case_variants_in_add_list(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Adding ``["A", "a"]`` results in a single canonical ``["a"]`` entry."""
    doc_id = _seed(test_db, fake_embedder, tags=[])
    final = apply_tags(test_db, doc_id, add=["A", "a"])
    assert final == ["a"]
    row = test_db.execute(
        "SELECT tags FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    assert list(row[0]) == ["a"]
