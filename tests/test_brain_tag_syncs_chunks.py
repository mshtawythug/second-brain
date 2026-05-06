"""Phase B regression — ``brain tag`` propagates tag changes to chunks.

``brain tag`` runs through :func:`brain.ingest.apply_tags`, which UPDATEs
``documents.tags`` and then calls
:func:`brain.queries.sync_chunk_search_metadata` to keep
``chunks.tags_text`` in lockstep. Both add and remove paths must propagate.

Manual-source docs (no vault file on disk) are sufficient for this test —
the file-writeback logic exercised by ``test_cli_tag.py`` is orthogonal to
the chunk-sync invariant we're checking here.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import psycopg
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _chunk_tags_text(doc_id: str) -> list[str]:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT tags_text FROM chunks "
            "WHERE document_id=%s ORDER BY chunk_index",
            (doc_id,),
        ).fetchall()
    return [str(r[0]) for r in rows]


def _doc_tags(doc_id: str) -> list[str]:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT tags FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
    assert row is not None
    return list(row[0] or [])


def test_brain_tag_add_flips_chunk_tags_text(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    patch_embedder(fake_embedder)
    doc_id = seed_doc(content="some body that produces a chunk", tags=["existing"])
    result = CliRunner().invoke(app, ["tag", doc_id[:8], "+foo"])
    assert result.exit_code == 0, result.output

    # ``foo`` should appear in every chunk's tags_text — order matches the
    # documents.tags array order, not insertion order, so use a set check.
    db_tags = _doc_tags(doc_id)
    assert "foo" in db_tags
    for tags_text in _chunk_tags_text(doc_id):
        chunk_tags = set(tags_text.split())
        assert "foo" in chunk_tags
        assert "existing" in chunk_tags


def test_brain_tag_remove_flips_chunk_tags_text(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    patch_embedder(fake_embedder)
    doc_id = seed_doc(
        content="another body that yields chunks",
        tags=["foo", "bar"],
    )
    # Sanity: pre-condition. Both tags present on each chunk.
    pre = _chunk_tags_text(doc_id)
    assert pre
    for tags_text in pre:
        chunk_tags = set(tags_text.split())
        assert "foo" in chunk_tags
        assert "bar" in chunk_tags

    result = CliRunner().invoke(app, ["tag", doc_id[:8], "-foo"])
    assert result.exit_code == 0, result.output

    assert "foo" not in _doc_tags(doc_id)
    for tags_text in _chunk_tags_text(doc_id):
        chunk_tags = set(tags_text.split())
        assert "foo" not in chunk_tags
        assert "bar" in chunk_tags


def test_brain_tag_add_then_remove_converges(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    """Add+remove on the same invocation: chunks.tags_text reflects net result."""
    patch_embedder(fake_embedder)
    doc_id = seed_doc(content="body content", tags=["old"])
    result = CliRunner().invoke(app, ["tag", doc_id[:8], "+new", "-old"])
    assert result.exit_code == 0, result.output

    for tags_text in _chunk_tags_text(doc_id):
        chunk_tags = set(tags_text.split())
        assert "new" in chunk_tags
        assert "old" not in chunk_tags
