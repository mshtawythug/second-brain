"""Integration tests for the P2.2 gmail-thread upsert path.

Covers:

- New thread doc inserts (regression for the legacy per-message path).
- Re-ingest with the same body is a no-op (content_hash short-circuit).
- Re-ingest with a different body (extra message) UPDATEs in place — the
  document UUID stays stable, but title / body / metadata / chunks all
  reflect the new state.
- The migration-008 partial unique index ``uq_documents_gmail_thread``
  blocks a manual second INSERT under the same ``(thread_id,
  content_type='email_thread')`` pair, while leaving legacy per-message
  rows (``content_type='email'``) untouched so the migration window is
  safe.
- The legacy per-message ingest path keeps using ``message_id`` as the
  source's ``external_id`` (no behavior change for ``content_type='email'``
  rows).
- Migration 008 itself is idempotent (running it twice is a no-op).

These exercise ``brain.ingest.ingest_document`` against a real Postgres
test DB so the partial unique index, the column-promotion path, and the
hooks all execute exactly as they will in production.
"""
import base64
import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.db import run_migrations
from brain.ingest import ExtractedDoc, ingest_document
from brain.ingest.gmail import to_extracted_thread
from tests.conftest import FakeEmbedder

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


# ---------------------------------------------------------------------------
# Test fixtures — minimal Gmail ``users.messages.get`` response builder.
# Same shape as ``tests/test_gmail_thread.py`` so the assembly inside
# ``to_extracted_thread`` produces production-realistic ExtractedDocs.
# ---------------------------------------------------------------------------


def _b64url(text: str) -> str:
    """Encode ``text`` exactly as a Gmail API ``payload.body.data`` field would."""
    return (
        base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")
    )


def _make_message(
    *,
    msg_id: str,
    thread_id: str = "thread-XYZ",
    internal_date: str,
    headers: dict[str, str],
    body_text: str,
    label_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal Gmail ``users.messages.get`` response."""
    h_list = [{"name": k, "value": v} for k, v in headers.items()]
    payload: dict[str, Any] = {
        "mimeType": "text/plain",
        "headers": h_list,
        "body": {"data": _b64url(body_text)},
    }
    return {
        "id": msg_id,
        "threadId": thread_id,
        "labelIds": list(label_ids) if label_ids is not None else [],
        "payload": payload,
        "internalDate": internal_date,
    }


def _make_thread(
    *, thread_id: str = "thread-XYZ", message_count: int
) -> list[dict[str, Any]]:
    """Build a synthetic gmail thread of ``message_count`` messages.

    Senders alternate between alice@example.com and bob@example.com.
    Bodies are unique per message so chunk content shifts when more
    messages are added (so the upsert path can detect a body change).
    """
    msgs: list[dict[str, Any]] = []
    for i in range(message_count):
        sender = "alice@example.com" if i % 2 == 0 else "bob@example.com"
        recipient = "bob@example.com" if i % 2 == 0 else "alice@example.com"
        # Hours 09 / 10 / 11 / 12 / 13 — strictly ascending so chronological
        # ordering is unambiguous regardless of internalDate.
        date_header = (
            f"Tue, 28 Apr 2026 {9 + i:02d}:00:00 -0400"
        )
        msgs.append(
            _make_message(
                msg_id=f"msg-{i + 1}",
                thread_id=thread_id,
                internal_date=str(1_000_000 + i * 1000),
                headers={
                    "Subject": "Quarterly review thread"
                    if i == 0
                    else "Re: Quarterly review thread",
                    "From": sender,
                    "To": recipient,
                    "Date": date_header,
                    "Message-ID": f"<msg-{i + 1}@example.com>",
                },
                body_text=f"Message body number {i + 1} — unique content here.",
                label_ids=["INBOX"],
            )
        )
    return msgs


# ---------------------------------------------------------------------------
# 1. New-thread INSERT.
# ---------------------------------------------------------------------------


def test_thread_doc_inserts_on_first_ingest(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    """First ingest of a 4-message thread → exactly 1 row, marker fields set."""
    thread_doc = to_extracted_thread(_make_thread(message_count=4))
    assert thread_doc.content_type == "email_thread"
    assert thread_doc.metadata["thread_id"] == "thread-XYZ"

    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=thread_doc,
        source_kind="gmail",
    )
    assert result.created is True
    assert result.body_changed is True

    rows = test_db.execute(
        "SELECT id, content_type, thread_id "
        "FROM documents WHERE thread_id = %s",
        ("thread-XYZ",),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "email_thread"
    assert rows[0][2] == "thread-XYZ"

    # The source row's external_id is the thread_id, not any single
    # message_id — that's the P2.2 contract for thread docs.
    src = test_db.execute(
        "SELECT kind, external_id FROM sources WHERE id = "
        "(SELECT source_id FROM documents WHERE id = %s)",
        (result.document_id,),
    ).fetchone()
    assert src is not None
    assert src[0] == "gmail"
    assert src[1] == "thread-XYZ"

    chunks = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id = %s",
        (result.document_id,),
    ).fetchone()
    assert chunks is not None
    assert chunks[0] >= 1


# ---------------------------------------------------------------------------
# 2. Re-ingest with the same thread + same body → idempotent no-op.
# ---------------------------------------------------------------------------


def test_thread_doc_upserts_on_re_ingest_same_body(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    """Re-ingest the same 4-message thread → 1 row total, no body rewrite."""
    msgs = _make_thread(message_count=4)
    thread_doc = to_extracted_thread(msgs)

    first = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=thread_doc,
        source_kind="gmail",
    )
    first_hash = test_db.execute(
        "SELECT content_hash FROM documents WHERE id = %s",
        (first.document_id,),
    ).fetchone()
    assert first_hash is not None
    first_chunk_ids = [
        r[0]
        for r in test_db.execute(
            "SELECT id FROM chunks WHERE document_id = %s ORDER BY chunk_index",
            (first.document_id,),
        ).fetchall()
    ]

    # Re-build the SAME thread doc and ingest again — content_hash must be
    # identical, so the upsert short-circuits.
    second_doc = to_extracted_thread(msgs)
    second = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=second_doc,
        source_kind="gmail",
    )
    assert second.created is False
    assert second.body_changed is False
    assert second.document_id == first.document_id

    # Still exactly one row with this thread_id.
    count = test_db.execute(
        "SELECT count(*) FROM documents WHERE thread_id = %s",
        ("thread-XYZ",),
    ).fetchone()
    assert count is not None
    assert count[0] == 1

    # content_hash unchanged.
    second_hash = test_db.execute(
        "SELECT content_hash FROM documents WHERE id = %s",
        (first.document_id,),
    ).fetchone()
    assert second_hash == first_hash

    # Chunk row identities preserved — no DELETE-then-INSERT cycle ran.
    second_chunk_ids = [
        r[0]
        for r in test_db.execute(
            "SELECT id FROM chunks WHERE document_id = %s ORDER BY chunk_index",
            (first.document_id,),
        ).fetchall()
    ]
    assert second_chunk_ids == first_chunk_ids


# ---------------------------------------------------------------------------
# 3. Re-ingest with an extra message → in-place UPDATE (UUID stable).
# ---------------------------------------------------------------------------


def test_thread_doc_upserts_on_re_ingest_extra_message(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    """Re-ingest with a 5th message → same row, body / chunks rebuilt."""
    msgs_4 = _make_thread(message_count=4)
    first_doc = to_extracted_thread(msgs_4)
    first = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=first_doc,
        source_kind="gmail",
    )
    first_hash = test_db.execute(
        "SELECT content_hash FROM documents WHERE id = %s",
        (first.document_id,),
    ).fetchone()
    assert first_hash is not None
    first_chunk_ids = {
        r[0]
        for r in test_db.execute(
            "SELECT id FROM chunks WHERE document_id = %s",
            (first.document_id,),
        ).fetchall()
    }

    msgs_5 = _make_thread(message_count=5)
    second_doc = to_extracted_thread(msgs_5)
    # Sanity: the rebuilt thread doc actually has different body bytes
    # (otherwise the test would tautologically pass via the same-body
    # short-circuit).
    assert second_doc.content != first_doc.content
    assert second_doc.metadata["message_count"] == 5

    second = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=second_doc,
        source_kind="gmail",
    )
    assert second.created is False
    assert second.body_changed is True
    assert second.document_id == first.document_id  # UUID stable

    # Still exactly one row.
    count = test_db.execute(
        "SELECT count(*) FROM documents WHERE thread_id = %s",
        ("thread-XYZ",),
    ).fetchone()
    assert count is not None
    assert count[0] == 1

    # content_hash, content, message_count metadata all reflect 5 messages.
    row = test_db.execute(
        "SELECT content_hash, content, metadata FROM documents WHERE id = %s",
        (first.document_id,),
    ).fetchone()
    assert row is not None
    new_hash, new_content, new_meta = row
    assert new_hash != first_hash[0]
    assert new_content == second_doc.content
    assert new_meta["message_count"] == 5

    # Chunks rebuilt — at least one new chunk id (the prior set is gone).
    second_chunk_ids = {
        r[0]
        for r in test_db.execute(
            "SELECT id FROM chunks WHERE document_id = %s",
            (first.document_id,),
        ).fetchall()
    }
    assert second_chunk_ids.isdisjoint(first_chunk_ids)
    assert len(second_chunk_ids) >= 1


# ---------------------------------------------------------------------------
# 4. Partial unique index blocks a concurrent dual-insert.
# ---------------------------------------------------------------------------


def test_partial_unique_index_blocks_concurrent_dual_insert(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    """A direct second INSERT with same (thread_id, content_type='email_thread')
    raises IntegrityError — the partial unique index catches it.
    """
    thread_doc = to_extracted_thread(_make_thread(message_count=3))
    first = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=thread_doc,
        source_kind="gmail",
    )
    assert first.document_id is not None

    # Bypass ingest_document's upsert path — go straight to SQL to simulate
    # a racing second writer that won the dedup-SELECT but lost the INSERT.
    with pytest.raises(psycopg.errors.UniqueViolation):
        test_db.execute(
            "INSERT INTO documents "
            "(title, content, content_hash, content_type, "
            " thread_id, metadata, kind) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                "duplicate-thread-doc",
                "different body content for collision test",
                "deadbeef" * 8,  # 64-char dummy hash, won't collide on hash
                "email_thread",
                "thread-XYZ",
                json.dumps({"thread_id": "thread-XYZ"}),
                "ingested",
            ),
        )


# ---------------------------------------------------------------------------
# 5. Partial unique index does NOT block legacy per-message rows.
# ---------------------------------------------------------------------------


def test_partial_unique_index_does_not_block_legacy_email_rows(
    test_db: psycopg.Connection,
) -> None:
    """Two rows with the same thread_id but content_type='email' (legacy
    per-message) coexist — the partial WHERE excludes them. Critical for
    the migration window where collapsed-thread rows live alongside
    not-yet-collapsed per-message rows.
    """
    # Two distinct per-message rows, same thread_id, content_type='email'.
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, "
        " thread_id, metadata, kind) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            "legacy msg 1",
            "msg 1 body",
            "a" * 64,
            "email",
            "thread-LEGACY",
            json.dumps({"thread_id": "thread-LEGACY", "message_id": "m1"}),
            "ingested",
        ),
    )
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, "
        " thread_id, metadata, kind) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            "legacy msg 2",
            "msg 2 body",
            "b" * 64,
            "email",
            "thread-LEGACY",
            json.dumps({"thread_id": "thread-LEGACY", "message_id": "m2"}),
            "ingested",
        ),
    )

    rows = test_db.execute(
        "SELECT count(*) FROM documents WHERE thread_id = %s",
        ("thread-LEGACY",),
    ).fetchone()
    assert rows is not None
    assert rows[0] == 2


# ---------------------------------------------------------------------------
# 6. Legacy per-message ingest still works.
# ---------------------------------------------------------------------------


def test_legacy_per_message_ingest_still_works(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    """Per-message gmail ingest (content_type='email') uses message_id as
    the source's ``external_id`` — no behavior change from P2.2.
    """
    per_message_doc = ExtractedDoc(
        title="Legacy single-message email",
        content="One-message body — no thread assembly here.",
        content_type="email",
        source_path=None,
        metadata={
            "message_id": "msg-legacy-1",
            "thread_id": "thread-LEGACY-2",
            "from": "alice@example.com",
        },
    )
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=per_message_doc,
        source_kind="gmail",
        source_external_id="msg-legacy-1",
    )
    assert result.created is True

    # Source row's external_id is the message_id, NOT the thread_id —
    # the P2.2 override only fires for ``content_type='email_thread'``.
    src = test_db.execute(
        "SELECT external_id FROM sources WHERE id = "
        "(SELECT source_id FROM documents WHERE id = %s)",
        (result.document_id,),
    ).fetchone()
    assert src is not None
    assert src[0] == "msg-legacy-1"

    # The row's content_type is 'email', not 'email_thread'.
    ct = test_db.execute(
        "SELECT content_type FROM documents WHERE id = %s",
        (result.document_id,),
    ).fetchone()
    assert ct is not None
    assert ct[0] == "email"


# ---------------------------------------------------------------------------
# 7. Migration 008 is idempotent.
# ---------------------------------------------------------------------------


def test_migration_008_is_idempotent(test_db: psycopg.Connection) -> None:
    """Running migration 008 twice is a no-op (CREATE INDEX IF NOT EXISTS)."""
    # The fixture already ran every migration once, including 008.
    # Running run_migrations again on the same connection should not
    # re-apply 008 (schema_migrations table guards it). Force a re-apply
    # by executing the SQL file body directly — it MUST succeed because
    # of the IF NOT EXISTS guard.
    sql_path = MIGRATIONS_DIR / "008_gmail_thread_unique.sql"
    sql_text = sql_path.read_text()
    test_db.execute(sql_text)  # First re-apply.
    test_db.execute(sql_text)  # Second re-apply.

    # The standard migration runner is also idempotent: schema_migrations
    # has the entry, so a second run_migrations applies nothing new.
    applied = run_migrations(test_db)
    assert "008_gmail_thread_unique.sql" not in applied


# ---------------------------------------------------------------------------
# 8. Bonus regression: re-ingest reuses the same source row.
# ---------------------------------------------------------------------------


def test_thread_re_ingest_reuses_source_row(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    """The thread-keyed ``sources`` row (kind='gmail', external_id=thread_id)
    is reused on every re-ingest — we never accumulate one source row per
    re-ingest call.
    """
    msgs_4 = _make_thread(message_count=4)
    first = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=to_extracted_thread(msgs_4),
        source_kind="gmail",
    )
    msgs_5 = _make_thread(message_count=5)
    second = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=to_extracted_thread(msgs_5),
        source_kind="gmail",
    )
    assert first.document_id == second.document_id

    sources = test_db.execute(
        "SELECT count(*) FROM sources "
        "WHERE kind = 'gmail' AND external_id = 'thread-XYZ'"
    ).fetchone()
    assert sources is not None
    assert sources[0] == 1
