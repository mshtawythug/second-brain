"""RM-42 wiring: both ingest chunk sites route ``content_type="code"`` through the code chunker.

Real Postgres. ``ingest_document`` (insert) reads the path hint from the
extracted document's metadata; ``update_document`` (``brain edit``) reads it
from the post-patch metadata, so an edit that sets ``path`` in the same call is
chunked with it.
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document, update_document

SOURCE = '''\
"""Synthetic module."""
import os


def first():
    return os.sep


def second():
    return 2
'''


def _chunk_heads(conn: psycopg.Connection[Any], document_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT content FROM chunks WHERE document_id=%s ORDER BY chunk_index",
        (document_id,),
    ).fetchall()
    return [str(r[0]).split("\n", 1)[0] for r in rows]


def _ingest(conn: psycopg.Connection[Any], embedder: Any, *, content_type: str) -> str:
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title="mod.py",
            content=SOURCE,
            content_type=content_type,
            source_path=None,
            metadata={"path": "pkg/mod.py"},
        ),
        source_kind="manual",
    )
    assert result.document_id is not None
    return result.document_id


def test_ingest_document_chunks_code_by_definition(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    document_id = _ingest(test_db, fake_embedder, content_type="code")

    assert _chunk_heads(test_db, document_id) == [
        "pkg/mod.py:1-2 <module>",
        "pkg/mod.py:5-6 first",
        "pkg/mod.py:9-10 second",
    ]


def test_ingest_document_keeps_prose_on_the_prose_chunker(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    document_id = _ingest(test_db, fake_embedder, content_type="txt")

    assert _chunk_heads(test_db, document_id) == ['"""Synthetic module."""']


def test_update_document_rechunks_code_with_the_post_patch_path(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    document_id = _ingest(test_db, fake_embedder, content_type="code")

    update_document(
        test_db,
        embedder=fake_embedder,
        document_id=document_id,
        new_content=SOURCE.replace("return 2", "return 3"),
        metadata_patch={"path": "pkg/renamed.py"},
    )

    assert _chunk_heads(test_db, document_id) == [
        "pkg/renamed.py:1-2 <module>",
        "pkg/renamed.py:5-6 first",
        "pkg/renamed.py:9-10 second",
    ]
