"""Tests for the Wave Q1-D ``list_existing_tags`` query helper."""
from __future__ import annotations

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import list_existing_tags


def _seed(
    test_db: psycopg.Connection,
    fake_embedder: object,
    *,
    title: str,
    tags: list[str],
) -> None:
    ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title=title,
            content=f"Body {title}",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=tags,
    )


def test_list_existing_tags_empty_corpus_returns_empty_list(
    test_db: psycopg.Connection,
) -> None:
    assert list_existing_tags(test_db) == []


def test_list_existing_tags_single_tag(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    _seed(test_db, fake_embedder, title="A", tags=["alpha"])
    assert list_existing_tags(test_db) == ["alpha"]


def test_list_existing_tags_dedupes_across_documents(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    _seed(test_db, fake_embedder, title="A", tags=["alpha", "beta"])
    _seed(test_db, fake_embedder, title="B", tags=["alpha", "gamma"])
    assert list_existing_tags(test_db) == ["alpha", "beta", "gamma"]


def test_list_existing_tags_alpha_sorted(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    _seed(test_db, fake_embedder, title="A", tags=["zulu", "alpha", "mike"])
    assert list_existing_tags(test_db) == ["alpha", "mike", "zulu"]


def test_list_existing_tags_min_doc_count_filters(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    _seed(test_db, fake_embedder, title="A", tags=["alpha", "rare"])
    _seed(test_db, fake_embedder, title="B", tags=["alpha"])
    # min_doc_count=2 → only ``alpha`` (used twice).
    assert list_existing_tags(test_db, min_doc_count=2) == ["alpha"]
