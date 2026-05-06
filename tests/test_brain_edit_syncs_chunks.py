"""Phase B regression — ``brain edit --title`` propagates the new title to
every chunk's ``title_text`` column.

Title-only edits do NOT re-chunk (the body is unchanged), so the only path
that keeps chunks consistent with the parent doc is the
``sync_chunk_search_metadata`` call wired into :func:`update_document`. This
test exercises the full CLI invocation so a regression in the wiring (e.g.
removing the call, or guarding it on the wrong field) trips the assertion.
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


def _chunk_titles(doc_id: str) -> list[str]:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT title_text FROM chunks WHERE document_id=%s ORDER BY chunk_index",
            (doc_id,),
        ).fetchall()
    return [str(r[0]) for r in rows]


def test_brain_edit_title_flips_chunk_title_text(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    patch_embedder(fake_embedder)
    doc_id = seed_doc(title="Old Title", content="paragraph body here")
    pre = _chunk_titles(doc_id)
    assert pre, "seed should produce at least one chunk"
    assert all(t == "Old Title" for t in pre), pre

    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--title", "Brand New Title"]
    )
    assert result.exit_code == 0, result.output

    post = _chunk_titles(doc_id)
    assert post, "post-edit chunks unexpectedly empty"
    assert all(t == "Brand New Title" for t in post), post


def test_brain_edit_title_only_does_not_clobber_tags_text(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    """Title-only edit must leave existing chunk.tags_text alone."""
    patch_embedder(fake_embedder)
    doc_id = seed_doc(
        title="Before",
        content="some body content",
        tags=["alpha", "beta"],
    )
    result = CliRunner().invoke(app, ["edit", doc_id[:8], "--title", "After"])
    assert result.exit_code == 0, result.output

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT title_text, tags_text FROM chunks WHERE document_id=%s",
            (doc_id,),
        ).fetchall()
    assert rows
    for title_text, tags_text in rows:
        assert title_text == "After"
        assert tags_text == "alpha beta"
