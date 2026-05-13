"""Phase B regression — every ``INSERT INTO chunks`` site populates the new
``title_text`` / ``tags_text`` / ``search_extras`` columns introduced by
migration 009.

Four insert paths are exercised:

1. :func:`brain.ingest.ingest_document` — new-doc create.
2. :func:`brain.ingest._update_doc_in_place` — gmail-thread upsert that
   replaces the body of an existing ``content_type='email_thread'`` row.
3. :func:`brain.ingest.update_document` — ``brain edit`` content rewrite.
4. :func:`brain.vault.sync._embed_and_insert_chunks` (via ``sync_vault``) —
   the vault-tier insert path.

Each test asserts the same denormalization invariant: ``title_text`` equals
``documents.title``, ``tags_text`` equals the space-joined tag list, and
``search_extras`` matches :func:`extract_sub_tokens` of the chunk body
(per-chunk, not per-doc).
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import psycopg

from brain.ingest import (
    ExtractedDoc,
    ingest_document,
    update_document,
)
from brain.ingest.sub_tokens import extract_sub_tokens
from brain.vault.frontmatter import dump_frontmatter
from brain.vault.sync import sync_vault


def _chunk_rows(
    conn: psycopg.Connection[Any], document_id: str
) -> list[tuple[str, str, str, str]]:
    """Return ``(content, title_text, tags_text, search_extras)`` per chunk."""
    rows = conn.execute(
        "SELECT content, title_text, tags_text, search_extras "
        "FROM chunks WHERE document_id=%s ORDER BY chunk_index",
        (document_id,),
    ).fetchall()
    return [
        (str(r[0]), str(r[1]), str(r[2]), str(r[3]) if r[3] is not None else "")
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 1. ingest_document — new-doc create path
# ---------------------------------------------------------------------------
def test_ingest_document_populates_new_chunk_columns(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """A fresh ingest writes title_text/tags_text/search_extras on every chunk."""
    body = "Reach out via person-b@example.com for the next event."
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="My Title",
            content=body,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=["a", "b"],
    )
    assert result.document_id is not None

    rows = _chunk_rows(test_db, result.document_id)
    assert rows, "expected at least one chunk for non-empty body"
    for content, title_text, tags_text, search_extras in rows:
        assert title_text == "My Title"
        assert tags_text == "a b"
        assert search_extras == extract_sub_tokens(content)


def test_ingest_document_empty_tags_emits_empty_string(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """No tags ⇒ ``tags_text=''`` (matches the `array_to_string` SQL pattern)."""
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="No Tags",
            content="body without tags",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=[],
    )
    assert result.document_id is not None
    rows = _chunk_rows(test_db, result.document_id)
    assert rows
    for _, title_text, tags_text, _ in rows:
        assert title_text == "No Tags"
        assert tags_text == ""


# ---------------------------------------------------------------------------
# 2. gmail-thread upsert — _update_doc_in_place
# ---------------------------------------------------------------------------
def test_thread_update_in_place_repopulates_chunk_columns(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """A re-ingest of the same gmail thread with new body rewrites chunk columns."""
    thread_id = "thread-abc"
    first = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Subject v1",
            content="Original body about example.com/groups",
            content_type="email_thread",
            source_path=None,
            metadata={"thread_id": thread_id},
        ),
        source_kind="gmail",
        source_external_id="msg-1",
        tags=["t1"],
    )
    assert first.document_id is not None
    first_id = first.document_id

    # Re-ingest with new body + new title + new tags. Same thread_id keys
    # the upsert path; document UUID stays stable.
    second = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Subject v2",
            content="New body mentioning bob@example.org and acme.test",
            content_type="email_thread",
            source_path=None,
            metadata={"thread_id": thread_id},
        ),
        source_kind="gmail",
        source_external_id="msg-2",
        tags=["t1", "t2"],
    )
    assert second.document_id == first_id
    assert second.created is False
    assert second.body_changed is True

    rows = _chunk_rows(test_db, first_id)
    assert rows, "thread upsert should re-insert chunks"
    for content, title_text, tags_text, search_extras in rows:
        assert title_text == "Subject v2"
        assert tags_text == "t1 t2"
        assert search_extras == extract_sub_tokens(content)


# ---------------------------------------------------------------------------
# 3. update_document — brain edit content rewrite
# ---------------------------------------------------------------------------
def test_update_document_rechunk_populates_new_columns(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """``brain edit --content`` re-inserts chunks with the new title/tags."""
    seed = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Original",
            content="alpha body",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=["one"],
    )
    assert seed.document_id is not None
    doc_id = seed.document_id

    update_document(
        test_db,
        embedder=fake_embedder,
        document_id=doc_id,
        new_title="Renamed",
        new_content="brand new content about https://acme.example.org/path",
        new_tags=["one", "two"],
    )

    rows = _chunk_rows(test_db, doc_id)
    assert rows
    for content, title_text, tags_text, search_extras in rows:
        assert title_text == "Renamed"
        assert tags_text == "one two"
        assert search_extras == extract_sub_tokens(content)


def test_update_document_rechunk_keeps_existing_title_when_not_overridden(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """When new_title is omitted, chunks pick up the row's pre-edit title."""
    seed = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Stays The Same",
            content="old body",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=["keep"],
    )
    assert seed.document_id is not None
    doc_id = seed.document_id

    update_document(
        test_db,
        embedder=fake_embedder,
        document_id=doc_id,
        new_content="completely new body here",
    )

    rows = _chunk_rows(test_db, doc_id)
    assert rows
    for _, title_text, tags_text, _ in rows:
        assert title_text == "Stays The Same"
        assert tags_text == "keep"


# ---------------------------------------------------------------------------
# 4. vault sync — _embed_and_insert_chunks
# ---------------------------------------------------------------------------
def test_vault_sync_chunk_insert_populates_new_columns(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """``brain vault sync`` insert path writes title/tags/search_extras."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note_id = str(uuid.uuid4())
    note_path = vault / "vault-note.md"
    note_path.write_text(
        dump_frontmatter(
            {"id": note_id, "title": "Vault Note", "tags": ["v", "w"]},
            "Body referencing person-b@example.com here.\n",
        )
    )

    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)

    rows = _chunk_rows(test_db, note_id)
    assert rows, "sync should chunk a non-empty body"
    for content, title_text, tags_text, search_extras in rows:
        assert title_text == "Vault Note"
        assert tags_text == "v w"
        assert search_extras == extract_sub_tokens(content)
