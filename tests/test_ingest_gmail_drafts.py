"""Tests for the Q1-A email-drafts inclusion behaviour.

Wave Q1-A (2026-05-11) changed Gmail ingest to include draft messages rather
than skip them. Draft documents are stamped with ``documents.draft = TRUE``
so the P1.6 wiki quarantine hides them from Quartz while ``brain search``
still surfaces them.

Tests:
1. ``to_extracted_doc`` no longer raises ``DraftSkipped``; sets ``_is_draft=True``.
2. ``to_extracted_thread`` marks an all-draft thread with ``_is_draft=True``,
   includes all messages in the body.
3. ``to_extracted_thread`` keeps ``_is_draft=False`` on a mixed thread and
   drops drafts from the rendered body.
4. ``ingest_document(draft=True)`` stamps the ``documents.draft`` column.
5. Upsert path flips ``draft=False`` when a sent reply arrives on an
   all-draft thread.
6. All-draft thread body is updated when a new draft is added (draft+draft
   update must still work; guard only blocks published-with-draft-incoming).
7. Auto-flip TRUE→FALSE correctly updates the body to the full mixed-thread
   view (sent messages only; draft bodies dropped per mixed-thread rule).
8. Partial re-ingest with ``draft=True`` on a published thread is a no-op:
   ``draft`` stays FALSE, body not overwritten, ``body_changed=False``.
"""
import base64
from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, IngestResult, ingest_document
from brain.ingest.gmail import to_extracted_doc, to_extracted_thread

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64url(text: str) -> str:
    """Encode text as urlsafe base64 without padding (Gmail API format)."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _make_msg(
    *,
    msg_id: str = "m1",
    thread_id: str = "t1",
    internal_date: str = "1000",
    subject: str = "Test Subject",
    from_addr: str = "alice@example.com",
    date: str | None = "Mon, 01 Jan 2026 12:00:00 +0000",
    body: str = "body text",
    label_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal Gmail Message resource."""
    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": from_addr},
    ]
    if date is not None:
        headers.append({"name": "Date", "value": date})
    msg: dict[str, Any] = {
        "id": msg_id,
        "threadId": thread_id,
        "labelIds": list(label_ids) if label_ids is not None else ["INBOX"],
        "internalDate": internal_date,
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": _b64url(body), "size": len(body)},
        },
    }
    return msg


# ---------------------------------------------------------------------------
# Test 1: to_extracted_doc — draft no longer raises DraftSkipped
# ---------------------------------------------------------------------------


def test_to_extracted_doc_returns_draft_with_flag() -> None:
    """A DRAFT-labelled message returns an ExtractedDoc with ``_is_draft=True``.

    Prior to Q1-A this raised ``DraftSkipped``. The exception is no longer
    raised; instead the metadata key ``_is_draft`` signals to the pipeline
    that the document should be stamped ``draft=TRUE``.
    """
    msg = _make_msg(
        msg_id="draft-1",
        label_ids=["DRAFT"],
        date=None,  # drafts often have no Date header
    )
    doc = to_extracted_doc(msg)
    assert doc.metadata["_is_draft"] is True
    # No Date header → sent_at should be absent.
    assert "sent_at" not in doc.metadata
    # Title, content, and content_type should be populated normally.
    assert doc.title == "Test Subject"
    assert doc.content_type == "email"


def test_to_extracted_doc_non_draft_has_is_draft_false() -> None:
    """A regular (sent) message has ``_is_draft=False`` in metadata."""
    msg = _make_msg(label_ids=["INBOX", "SENT"])
    doc = to_extracted_doc(msg)
    assert doc.metadata["_is_draft"] is False


# ---------------------------------------------------------------------------
# Test 2: to_extracted_thread — all-draft thread gets _is_draft=True
# ---------------------------------------------------------------------------


def test_to_extracted_thread_marks_all_draft_thread() -> None:
    """Three draft messages → ``_is_draft=True``; all three render in the body.

    The all-draft thread is assembled intact: body, participants,
    message_count, and label_ids all reflect all three messages.
    """
    msgs = [
        _make_msg(
            msg_id="d1",
            thread_id="td",
            internal_date="1000",
            subject="Draft",
            from_addr="alice@example.com",
            body="DRAFT BODY ONE",
            label_ids=["DRAFT"],
        ),
        _make_msg(
            msg_id="d2",
            thread_id="td",
            internal_date="2000",
            subject="Re: Draft",
            from_addr="alice@example.com",
            body="DRAFT BODY TWO",
            label_ids=["DRAFT"],
        ),
        _make_msg(
            msg_id="d3",
            thread_id="td",
            internal_date="3000",
            subject="Re: Draft",
            from_addr="alice@example.com",
            body="DRAFT BODY THREE",
            label_ids=["DRAFT"],
        ),
    ]
    doc = to_extracted_thread(msgs)
    assert doc.metadata["_is_draft"] is True
    assert doc.metadata["message_count"] == 3
    # All three bodies must appear in the assembled content.
    assert "DRAFT BODY ONE" in doc.content
    assert "DRAFT BODY TWO" in doc.content
    assert "DRAFT BODY THREE" in doc.content


# ---------------------------------------------------------------------------
# Test 3: to_extracted_thread — mixed thread drops drafts, publishes
# ---------------------------------------------------------------------------


def test_to_extracted_thread_mixed_drops_drafts_and_publishes() -> None:
    """Two sent + one draft → ``_is_draft=False``; body has only the two sent msgs.

    The draft's body is excluded from the rendered document. message_count
    reflects only the sent messages.
    """
    msgs = [
        _make_msg(
            msg_id="s1",
            thread_id="tm",
            internal_date="1000",
            body="SENT BODY ONE",
            label_ids=["INBOX", "SENT"],
        ),
        _make_msg(
            msg_id="d1",
            thread_id="tm",
            internal_date="2000",
            body="DRAFT BODY SHOULD NOT APPEAR",
            label_ids=["DRAFT"],
        ),
        _make_msg(
            msg_id="s2",
            thread_id="tm",
            internal_date="3000",
            body="SENT BODY TWO",
            label_ids=["INBOX", "SENT"],
        ),
    ]
    doc = to_extracted_thread(msgs)
    assert doc.metadata["_is_draft"] is False
    assert doc.metadata["message_count"] == 2
    assert "SENT BODY ONE" in doc.content
    assert "SENT BODY TWO" in doc.content
    assert "DRAFT BODY SHOULD NOT APPEAR" not in doc.content


# ---------------------------------------------------------------------------
# Test 4: ingest_document stamps documents.draft = TRUE
# ---------------------------------------------------------------------------


def test_ingest_document_stamps_draft_column(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """``ingest_document(draft=True)`` writes ``draft=TRUE`` to documents."""
    doc = ExtractedDoc(
        title="My Draft Email",
        content="This is a draft I never sent.",
        content_type="email",
        source_path=None,
        metadata={"_is_draft": True},
    )
    result: IngestResult = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="gmail",
        source_external_id="draft-ext-1",
        draft=True,
    )
    assert result.created is True
    row = test_db.execute(
        "SELECT draft FROM documents WHERE id = %s",
        (result.document_id,),
    ).fetchone()
    assert row is not None
    assert row[0] is True


def test_ingest_document_draft_false_by_default(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """``ingest_document()`` without ``draft=`` writes ``draft=FALSE`` (default)."""
    doc = ExtractedDoc(
        title="A Normal Email",
        content="This is a regular sent email.",
        content_type="email",
        source_path=None,
        metadata={},
    )
    result: IngestResult = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="gmail",
        source_external_id="sent-ext-1",
    )
    assert result.created is True
    row = test_db.execute(
        "SELECT draft FROM documents WHERE id = %s",
        (result.document_id,),
    ).fetchone()
    assert row is not None
    assert row[0] is False


# ---------------------------------------------------------------------------
# Test 5: upsert path flips draft=False when a sent reply arrives
# ---------------------------------------------------------------------------


def _make_thread_doc(*, all_draft: bool, thread_id: str = "t-upsert") -> ExtractedDoc:
    """Build a minimal email_thread ExtractedDoc for upsert tests."""
    flag_char = "D" if all_draft else "S"
    return ExtractedDoc(
        title="Thread Subject",
        content=f"[{flag_char}] Thread body content.",
        content_type="email_thread",
        source_path=None,
        metadata={
            "thread_id": thread_id,
            "from": "alice@example.com",
            "to": "bob@example.com",
            "date": "Mon, 01 Jan 2026 12:00:00 +0000",
            "label_ids": ["DRAFT"] if all_draft else ["INBOX", "SENT"],
            "participants": ["alice@example.com", "bob@example.com"],
            "message_count": 1,
            "_is_draft": all_draft,
        },
    )


def test_thread_upsert_flips_draft_false_when_sent_reply_arrives(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """First ingest of an all-draft thread sets ``draft=True``; adding a sent
    reply on the second ingest flips it to ``draft=False``.

    This guards the critical use-case where the user starts typing a reply
    (the thread is all-draft) and then sends it (the thread becomes mixed /
    all-sent). The wiki must re-surface the thread after the flip.
    """
    # Round 1: all-draft thread → draft=True in DB.
    draft_doc = _make_thread_doc(all_draft=True)
    r1: IngestResult = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=draft_doc,
        source_kind="gmail",
        source_external_id="t-upsert",
        draft=True,
    )
    assert r1.created is True
    row1 = test_db.execute(
        "SELECT draft FROM documents WHERE id = %s", (r1.document_id,)
    ).fetchone()
    assert row1 is not None and row1[0] is True

    # Round 2: same thread_id but now a sent reply exists → draft=False.
    sent_doc = _make_thread_doc(all_draft=False)
    r2: IngestResult = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=sent_doc,
        source_kind="gmail",
        source_external_id="t-upsert",
        draft=False,
        force=True,  # force an update even if content hash matches
    )
    # In-place update path: created=False, body_changed=True.
    assert r2.created is False
    assert r2.body_changed is True
    # Document UUID preserved.
    assert r2.document_id == r1.document_id

    row2 = test_db.execute(
        "SELECT draft FROM documents WHERE id = %s", (r2.document_id,)
    ).fetchone()
    assert row2 is not None and row2[0] is False


# ---------------------------------------------------------------------------
# Test 6: re-ingest with draft=True must NOT hide a published thread
# ---------------------------------------------------------------------------


def test_published_thread_not_hidden_by_partial_reingest(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """A published thread (draft=FALSE) is fully preserved after a partial
    re-ingest that passes ``draft=True``.

    Scenario: Thread T has 2 sent messages (ingested, draft=FALSE). Later,
    ``brain ingest-gmail --since 7d`` fetches only the draft reply (the sent
    messages are outside the time window). The extractor sees only the draft
    message, sets ``_is_draft=True``, and the pipeline re-ingests with
    ``draft=True``. Three things must hold:

    1. ``draft`` column stays FALSE (the thread must remain visible in the wiki).
    2. The body is NOT overwritten with the draft-only content
       (publishing draft-only content must be blocked even when force=True).
    3. ``body_changed=False`` — the pipeline reports a no-op, not an update.

    This guards both Codex-flagged Q1-A bugs:
    (a) hiding a published thread (``draft`` flip)
    (b) replacing a published body with a draft-only view (body overwrite)
    """
    # Round 1: published thread (sent messages) → draft=FALSE.
    published_doc = _make_thread_doc(all_draft=False, thread_id="t-partial-reingest")
    r1: IngestResult = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=published_doc,
        source_kind="gmail",
        source_external_id="t-partial-reingest",
        draft=False,
    )
    assert r1.created is True
    row1 = test_db.execute(
        "SELECT draft, content FROM documents WHERE id = %s", (r1.document_id,)
    ).fetchone()
    assert row1 is not None and row1[0] is False, "initial ingest must produce draft=FALSE"
    original_content = row1[1]

    # Round 2: re-ingest with draft=True (partial window, only saw draft reply).
    # Content differs from round 1 to test that body overwrite is also blocked
    # even when force=True bypasses the hash check.
    partial_doc = ExtractedDoc(
        title="Thread Subject",
        content="[partial window — draft only] Updated body after draft reply.",
        content_type="email_thread",
        source_path=None,
        metadata={
            "thread_id": "t-partial-reingest",
            "from": "alice@example.com",
            "to": "bob@example.com",
            "date": "Mon, 01 Jan 2026 12:00:00 +0000",
            "label_ids": ["DRAFT"],
            "participants": ["alice@example.com"],
            "message_count": 1,
            "_is_draft": True,
        },
    )
    r2: IngestResult = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=partial_doc,
        source_kind="gmail",
        source_external_id="t-partial-reingest",
        draft=True,
        force=True,  # even force=True must not overwrite a published thread with draft content
    )
    # UUID preserved — no new row created.
    assert r2.document_id == r1.document_id, "UUID must be preserved across update"
    # Pipeline must report a no-op (body NOT overwritten).
    assert r2.body_changed is False, (
        "partial draft re-ingest must be a no-op for a published thread "
        "(body_changed must be False)"
    )

    row2 = test_db.execute(
        "SELECT draft, content FROM documents WHERE id = %s", (r2.document_id,)
    ).fetchone()
    assert row2 is not None
    # (a) draft column must stay FALSE.
    assert row2[0] is False, (
        "re-ingest with draft=True must NOT flip a published thread to "
        "draft=TRUE (the ingest window may be a subset of the full thread)"
    )
    # (b) body must NOT have been replaced with the draft-only content.
    assert row2[1] == original_content, (
        "partial draft re-ingest must NOT overwrite the published body "
        "with draft-only content"
    )


# ---------------------------------------------------------------------------
# Test 7: all-draft + new draft added → body updates (guard must NOT block)
# ---------------------------------------------------------------------------


def test_all_draft_thread_refresh_updates_body_when_new_draft_added(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Adding a new draft to an all-draft thread must update the stored body.

    The partial-window guard only fires when the EXISTING row is published
    (``draft=FALSE``) and the incoming is draft-only (``draft=True``). When
    both the existing row and the incoming doc are draft-only, the update
    must proceed so the thread body reflects the latest draft state.
    """
    tid = "t-draft-refresh"
    # Round 1: two-draft thread.
    doc1 = ExtractedDoc(
        title="Draft Thread",
        content="Draft one body.",
        content_type="email_thread",
        source_path=None,
        metadata={
            "thread_id": tid,
            "from": "alice@example.com",
            "to": "alice@example.com",
            "date": "Mon, 01 Jan 2026 12:00:00 +0000",
            "label_ids": ["DRAFT"],
            "participants": ["alice@example.com"],
            "message_count": 2,
            "_is_draft": True,
        },
    )
    r1: IngestResult = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc1,
        source_kind="gmail",
        source_external_id=tid,
        draft=True,
    )
    assert r1.created is True

    # Round 2: three-draft thread (new draft added) — must update body.
    doc2 = ExtractedDoc(
        title="Draft Thread",
        content="Draft one body.\n\nDraft two body.\n\nDraft three body.",
        content_type="email_thread",
        source_path=None,
        metadata={
            "thread_id": tid,
            "from": "alice@example.com",
            "to": "alice@example.com",
            "date": "Mon, 01 Jan 2026 13:00:00 +0000",
            "label_ids": ["DRAFT"],
            "participants": ["alice@example.com"],
            "message_count": 3,
            "_is_draft": True,
        },
    )
    r2: IngestResult = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc2,
        source_kind="gmail",
        source_external_id=tid,
        draft=True,
    )
    # Must be an in-place update — same UUID, body updated.
    assert r2.document_id == r1.document_id, "UUID must be stable across draft refresh"
    assert r2.created is False
    assert r2.body_changed is True, "body must be updated when a new draft is added"

    row = test_db.execute(
        "SELECT draft, content FROM documents WHERE id = %s", (r1.document_id,)
    ).fetchone()
    assert row is not None
    assert row[0] is True, "draft must still be TRUE after all-draft refresh"
    assert "Draft three body." in row[1], "new draft content must appear in body"


# ---------------------------------------------------------------------------
# Test 8: auto-flip TRUE→FALSE keeps full body + drops draft bodies
# ---------------------------------------------------------------------------


def test_draft_thread_flips_to_published_when_sent_reply_arrives_keeps_full_body(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Auto-flip TRUE→FALSE correctly updates body to the mixed-thread view.

    Round 1: all-draft thread → ``draft=True``.
    Round 2: a sent reply arrives, thread becomes mixed. ``to_extracted_thread``
    drops draft bodies per the mixed-thread rule (only sent messages render).
    The pipeline should: flip ``draft`` to FALSE (auto-flip works), update the
    body to the sent-only view, and preserve the UUID.
    """
    tid = "t-flip-sent"
    # Round 1: all-draft thread.
    all_draft_doc = _make_thread_doc(all_draft=True, thread_id=tid)
    r1: IngestResult = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=all_draft_doc,
        source_kind="gmail",
        source_external_id=tid,
        draft=True,
    )
    assert r1.created is True
    row1 = test_db.execute(
        "SELECT draft FROM documents WHERE id = %s", (r1.document_id,)
    ).fetchone()
    assert row1 is not None and row1[0] is True

    # Round 2: sent reply added → mixed thread per mixed-thread rule: body
    # contains only sent messages. _make_thread_doc(all_draft=False) renders
    # a doc where sent messages have been assembled from the non-draft subset.
    sent_reply_doc = ExtractedDoc(
        title="Thread Subject",
        content="SENT REPLY BODY — the draft bodies are dropped.",
        content_type="email_thread",
        source_path=None,
        metadata={
            "thread_id": tid,
            "from": "bob@example.com",
            "to": "alice@example.com",
            "date": "Tue, 02 Jan 2026 09:00:00 +0000",
            "label_ids": ["INBOX", "SENT"],
            "participants": ["alice@example.com", "bob@example.com"],
            "message_count": 1,
            "_is_draft": False,
        },
    )
    r2: IngestResult = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=sent_reply_doc,
        source_kind="gmail",
        source_external_id=tid,
        draft=False,
    )
    # UUID preserved across auto-flip.
    assert r2.document_id == r1.document_id, "UUID must be preserved across auto-flip"
    assert r2.created is False
    assert r2.body_changed is True, "body must be updated when sent reply arrives"

    row2 = test_db.execute(
        "SELECT draft, content FROM documents WHERE id = %s", (r2.document_id,)
    ).fetchone()
    assert row2 is not None
    # Auto-flip must have worked.
    assert row2[0] is False, "draft must be flipped to FALSE after sent reply"
    # Body must reflect the sent-reply content.
    assert "SENT REPLY BODY" in row2[1], "sent reply body must appear in content"
