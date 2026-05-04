"""Tests for ``brain.ingest.gmail.to_extracted_thread`` — Phase 2.1.

Pure unit tests — no DB, no fixtures beyond the standard library. The
function is the foundation Phase 2 builds on: given N raw Gmail messages
that share a thread, produce ONE ``ExtractedDoc`` whose body is a single
Markdown document with chronological H2-per-message sections, and whose
metadata aggregates header fields across the thread (first-vs-latest
asymmetry pinned by the spec).
"""
import base64
from typing import Any

import pytest

from brain.ingest.gmail import to_extracted_thread


def _b64url(text: str) -> str:
    """Encode ``text`` exactly as a Gmail API ``payload.body.data`` field would."""
    return (
        base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")
    )


def _make_message(
    *,
    msg_id: str = "m1",
    thread_id: str = "abc123",
    internal_date: str | None = "1714312800000",  # 2026-04-28 14:00:00 UTC (approx)
    headers: dict[str, str] | None = None,
    body_text: str = "Hello",
    label_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal Gmail ``users.messages.get`` response for testing.

    ``headers`` is keyed by canonical header name (e.g. ``"Subject"``,
    ``"From"``); the function flattens it into Gmail's
    ``[{"name": ..., "value": ...}]`` shape. ``internal_date`` may be
    ``None`` to omit the field, or any string (unparseable values exercise
    the ``Date:`` header fallback path).
    """
    h_list = [{"name": k, "value": v} for k, v in (headers or {}).items()]
    payload: dict[str, Any] = {
        "mimeType": "text/plain",
        "headers": h_list,
        "body": {"data": _b64url(body_text)},
    }
    msg: dict[str, Any] = {
        "id": msg_id,
        "threadId": thread_id,
        "labelIds": list(label_ids) if label_ids is not None else [],
        "payload": payload,
    }
    if internal_date is not None:
        msg["internalDate"] = internal_date
    return msg


# --- 1. Single message -----------------------------------------------------


def test_single_message_thread() -> None:
    """A 1-message thread yields a single H2 + body, no <details> wrapper."""
    msg = _make_message(
        internal_date="1714312800000",
        headers={
            "Subject": "hello world",
            "From": "Alice <alice@example.com>",
            "To": "Bob <bob@example.com>",
            "Date": "Tue, 28 Apr 2026 10:00:00 -0400",
        },
        body_text="just one message body",
    )
    doc = to_extracted_thread([msg])
    assert doc.title == "hello world"
    assert doc.content_type == "email_thread"
    assert doc.metadata["message_count"] == 1
    # Single-message thread: latest-and-only → plain H2, no <details>.
    assert "<details>" not in doc.content
    assert doc.content.startswith("## ")
    assert "just one message body" in doc.content
    # sent_at echoes the one and only message's Date header in ISO UTC.
    assert doc.metadata["sent_at"] == "2026-04-28T14:00:00+00:00"
    # Participants = from + to (no cc).
    assert doc.metadata["participants"] == [
        "Alice <alice@example.com>",
        "Bob <bob@example.com>",
    ]


# --- 2. Chronological order from shuffled input ----------------------------


def test_four_message_thread_chronological() -> None:
    """Shuffled input ordering → output H2s appear in date-ascending order."""
    msgs = [
        _make_message(
            msg_id="m3",
            internal_date="3000",
            headers={
                "Subject": "Re: x",
                "From": "c@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:03 -0400",
            },
            body_text="third body",
        ),
        _make_message(
            msg_id="m1",
            internal_date="1000",
            headers={
                "Subject": "x",
                "From": "a@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:01 -0400",
            },
            body_text="first body",
        ),
        _make_message(
            msg_id="m4",
            internal_date="4000",
            headers={
                "Subject": "Re: Re: x",
                "From": "d@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:04 -0400",
            },
            body_text="fourth body",
        ),
        _make_message(
            msg_id="m2",
            internal_date="2000",
            headers={
                "Subject": "Re: x",
                "From": "b@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:02 -0400",
            },
            body_text="second body",
        ),
    ]
    doc = to_extracted_thread(msgs)
    assert doc.metadata["message_count"] == 4
    pos1 = doc.content.index("first body")
    pos2 = doc.content.index("second body")
    pos3 = doc.content.index("third body")
    pos4 = doc.content.index("fourth body")
    assert pos1 < pos2 < pos3 < pos4


# --- 3. Re/Fwd prefixes stripped from the title ----------------------------


def test_re_fwd_prefix_stripped() -> None:
    """Repeated ``Re:`` / ``Fwd:`` prefixes are stripped before title is set."""
    msg = _make_message(
        headers={
            "Subject": "Re: Re: Fwd: hello",
            "From": "a@x.com",
            "Date": "Tue, 28 Apr 2026 10:00:00 -0400",
        }
    )
    doc = to_extracted_thread([msg])
    assert doc.title == "hello"


# --- 4. Unicode preserved (only the slug strips it) ------------------------


def test_unicode_subject_preserved() -> None:
    """Unicode glyphs survive into ``documents.title`` — only the URL slug strips them."""
    msg = _make_message(
        headers={
            "Subject": "🔥 important",
            "From": "a@x.com",
            "Date": "Tue, 28 Apr 2026 10:00:00 -0400",
        }
    )
    doc = to_extracted_thread([msg])
    assert doc.title == "🔥 important"


# --- 5. Participants: union, sorted, case-insensitive dedupe ---------------


def test_participants_unique_sorted() -> None:
    """From/To/Cc unioned across messages, deduped case-insensitively, sorted."""
    msgs = [
        _make_message(
            msg_id="m1",
            internal_date="1000",
            headers={
                "Subject": "x",
                "From": "Alice <alice@example.com>",
                "To": "Bob <bob@example.com>",
                "Cc": "Carol <carol@example.com>",
                "Date": "Tue, 28 Apr 2026 10:00:01 -0400",
            },
        ),
        _make_message(
            msg_id="m2",
            internal_date="2000",
            headers={
                "Subject": "Re: x",
                "From": "Bob <bob@example.com>",
                "To": "Alice <alice@example.com>, Carol <carol@example.com>",
                "Cc": "Dave <dave@example.com>",
                "Date": "Tue, 28 Apr 2026 10:00:02 -0400",
            },
        ),
        _make_message(
            msg_id="m3",
            internal_date="3000",
            headers={
                "Subject": "Re: x",
                # Different case — should collapse via case-insensitive dedupe.
                "From": "ALICE <alice@example.com>",
                "To": "Eve <eve@example.com>",
                "Date": "Tue, 28 Apr 2026 10:00:03 -0400",
            },
        ),
        _make_message(
            msg_id="m4",
            internal_date="4000",
            headers={
                "Subject": "Re: x",
                "From": "Carol <carol@example.com>",
                "To": "Bob <bob@example.com>",
                "Date": "Tue, 28 Apr 2026 10:00:04 -0400",
            },
        ),
    ]
    doc = to_extracted_thread(msgs)
    participants = doc.metadata["participants"]
    # All five distinct addresses — the duplicate-cased "ALICE" is dropped.
    assert participants == [
        "Alice <alice@example.com>",
        "Bob <bob@example.com>",
        "Carol <carol@example.com>",
        "Dave <dave@example.com>",
        "Eve <eve@example.com>",
    ]


# --- 6. Boilerplate stripped per message -----------------------------------


def test_strip_boilerplate_applied_per_message() -> None:
    """``Sent from my iPhone`` (a known boilerplate pattern) is removed per section."""
    msgs = [
        _make_message(
            msg_id="m1",
            internal_date="1000",
            headers={
                "Subject": "x",
                "From": "a@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:01 -0400",
            },
            body_text="real content here\n\nSent from my iPhone",
        ),
        _make_message(
            msg_id="m2",
            internal_date="2000",
            headers={
                "Subject": "Re: x",
                "From": "b@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:02 -0400",
            },
            body_text="reply content",
        ),
    ]
    doc = to_extracted_thread(msgs)
    assert "Sent from my iPhone" not in doc.content
    assert "real content here" in doc.content


# --- 7. Most recent message stays expanded; older ones collapse ------------


def test_most_recent_message_not_collapsed() -> None:
    """Latest message renders as plain H2; all earlier messages get <details>."""
    msgs = [
        _make_message(
            msg_id="m1",
            internal_date="1000",
            headers={
                "Subject": "x",
                "From": "a@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:01 -0400",
            },
            body_text="OLD MESSAGE BODY",
        ),
        _make_message(
            msg_id="m2",
            internal_date="2000",
            headers={
                "Subject": "Re: x",
                "From": "b@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:02 -0400",
            },
            body_text="NEWEST MESSAGE BODY",
        ),
    ]
    doc = to_extracted_thread(msgs)
    # Older message wrapped in <details>.
    assert "<details>" in doc.content
    assert "<summary>" in doc.content
    assert "OLD MESSAGE BODY" in doc.content
    # The newest message is rendered as a plain H2 — find its heading and
    # confirm there is no <details> after it (i.e. it's not wrapped).
    newest_pos = doc.content.index("NEWEST MESSAGE BODY")
    # The plain H2 for the newest section has the form `## YYYY-MM-DD HH:MM`
    # immediately preceding it (one blank line apart).
    preceding = doc.content[:newest_pos]
    # The last `## ` heading before the newest body is the plain H2 for it.
    last_h2 = preceding.rfind("\n## ")
    last_details = preceding.rfind("<details>")
    # The plain H2 appears AFTER the last <details> open tag — confirming
    # the newest message isn't wrapped in <details>.
    assert last_h2 > last_details


# --- 8. thread_id from FIRST message ---------------------------------------


def test_metadata_thread_id_from_first() -> None:
    """metadata.thread_id is the FIRST message's threadId (stable across thread)."""
    msgs = [
        _make_message(
            msg_id="m1",
            thread_id="abc123",
            internal_date="1000",
            headers={
                "Subject": "x",
                "From": "a@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:01 -0400",
            },
        ),
        _make_message(
            msg_id="m2",
            thread_id="abc123",
            internal_date="2000",
            headers={
                "Subject": "Re: x",
                "From": "b@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:02 -0400",
            },
        ),
        _make_message(
            msg_id="m3",
            thread_id="abc123",
            internal_date="3000",
            headers={
                "Subject": "Re: x",
                "From": "c@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:03 -0400",
            },
        ),
        _make_message(
            msg_id="m4",
            thread_id="abc123",
            internal_date="4000",
            headers={
                "Subject": "Re: x",
                "From": "d@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:04 -0400",
            },
        ),
    ]
    doc = to_extracted_thread(msgs)
    assert doc.metadata["thread_id"] == "abc123"


# --- 9. sent_at from LATEST message ----------------------------------------


def test_metadata_sent_at_from_latest() -> None:
    """metadata.sent_at parses the LATEST message's Date header to ISO UTC."""
    msgs = [
        _make_message(
            msg_id="m1",
            internal_date="1000",
            headers={
                "Subject": "x",
                "From": "a@x.com",
                "Date": "Tue, 28 Apr 2026 09:00:00 -0400",
            },
        ),
        _make_message(
            msg_id="m2",
            internal_date="2000",
            headers={
                "Subject": "Re: x",
                "From": "b@x.com",
                "Date": "Tue, 28 Apr 2026 16:38:01 -0400",
            },
        ),
    ]
    doc = to_extracted_thread(msgs)
    # 16:38 -04:00 = 20:38 UTC.
    assert doc.metadata["sent_at"] == "2026-04-28T20:38:01+00:00"


# --- 10. rfc_message_id from LATEST ----------------------------------------


def test_metadata_rfc_message_id_from_latest() -> None:
    """metadata.rfc_message_id is the LATEST message's Message-ID header."""
    msgs = [
        _make_message(
            msg_id="m1",
            internal_date="1000",
            headers={
                "Subject": "x",
                "From": "a@x.com",
                "Date": "Tue, 28 Apr 2026 09:00:00 -0400",
                "Message-ID": "<first@example.com>",
            },
        ),
        _make_message(
            msg_id="m2",
            internal_date="2000",
            headers={
                "Subject": "Re: x",
                "From": "b@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:00 -0400",
                "Message-ID": "<latest@example.com>",
            },
        ),
    ]
    doc = to_extracted_thread(msgs)
    assert doc.metadata["rfc_message_id"] == "<latest@example.com>"


# --- 11. in_reply_to from LATEST -------------------------------------------


def test_metadata_in_reply_to_from_latest() -> None:
    """metadata.in_reply_to is the LATEST message's In-Reply-To header (parent)."""
    msgs = [
        _make_message(
            msg_id="m1",
            internal_date="1000",
            headers={
                "Subject": "x",
                "From": "a@x.com",
                "Date": "Tue, 28 Apr 2026 09:00:00 -0400",
                # No In-Reply-To on the first message — that's normal.
            },
        ),
        _make_message(
            msg_id="m2",
            internal_date="2000",
            headers={
                "Subject": "Re: x",
                "From": "b@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:00 -0400",
                "In-Reply-To": "<first@example.com>",
            },
        ),
    ]
    doc = to_extracted_thread(msgs)
    assert doc.metadata["in_reply_to"] == "<first@example.com>"


# --- 12. label_ids union ---------------------------------------------------


def test_label_ids_union() -> None:
    """label_ids is the sorted union across every message in the thread."""
    msgs = [
        _make_message(
            msg_id="m1",
            internal_date="1000",
            headers={
                "Subject": "x",
                "From": "a@x.com",
                "Date": "Tue, 28 Apr 2026 09:00:00 -0400",
            },
            label_ids=["INBOX", "IMPORTANT"],
        ),
        _make_message(
            msg_id="m2",
            internal_date="2000",
            headers={
                "Subject": "Re: x",
                "From": "b@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:00 -0400",
            },
            label_ids=["INBOX", "STARRED"],
        ),
    ]
    doc = to_extracted_thread(msgs)
    assert doc.metadata["label_ids"] == ["IMPORTANT", "INBOX", "STARRED"]


# --- 13. Long subject truncated at word boundary ---------------------------


def test_long_subject_truncated() -> None:
    """A 300-char subject is truncated at the last whole word ≤200 + ``…``."""
    # 60 copies of "word " → 300 chars.
    long_subject = ("word " * 60).strip()
    assert len(long_subject) > 200
    msg = _make_message(
        headers={
            "Subject": long_subject,
            "From": "a@x.com",
            "Date": "Tue, 28 Apr 2026 10:00:00 -0400",
        }
    )
    doc = to_extracted_thread([msg])
    # Total length cap: 200 + 1 (the appended ellipsis).
    assert len(doc.title) <= 201
    # Ends with the ellipsis sentinel.
    assert doc.title.endswith("…")
    # No mid-word break — every char before the ellipsis is part of a
    # whole "word" token (or a space between them).
    body_part = doc.title[:-1]
    assert body_part.endswith("word")


# --- 14. Garbage internalDate falls back to Date header --------------------


def test_unparseable_internal_date_falls_back_to_date_header() -> None:
    """Garbage ``internalDate`` strings fall back to ``Date:`` parse for ordering."""
    msgs = [
        # First chronologically (Date: 09:00) but listed second.
        _make_message(
            msg_id="m1",
            internal_date="not-a-number",
            headers={
                "Subject": "x",
                "From": "a@x.com",
                "Date": "Tue, 28 Apr 2026 09:00:00 -0400",
            },
            body_text="EARLIER body",
        ),
        # Second chronologically (Date: 10:00) but listed first.
        _make_message(
            msg_id="m2",
            internal_date="also-garbage",
            headers={
                "Subject": "Re: x",
                "From": "b@x.com",
                "Date": "Tue, 28 Apr 2026 10:00:00 -0400",
            },
            body_text="LATER body",
        ),
    ]
    # Reverse the list so internalDate isn't doing the work — the fallback is.
    doc = to_extracted_thread(list(reversed(msgs)))
    pos_earlier = doc.content.index("EARLIER body")
    pos_later = doc.content.index("LATER body")
    assert pos_earlier < pos_later


# --- 15. Empty subject → "(no subject)" ------------------------------------


def test_empty_subject_uses_no_subject() -> None:
    """First message with no Subject header → title falls back to ``(no subject)``."""
    msg = _make_message(
        headers={
            # No Subject key at all.
            "From": "a@x.com",
            "Date": "Tue, 28 Apr 2026 10:00:00 -0400",
        }
    )
    doc = to_extracted_thread([msg])
    assert doc.title == "(no subject)"


# --- 16. Missing To / Cc handled gracefully --------------------------------


def test_handles_missing_to_or_cc() -> None:
    """Messages without ``To`` or ``Cc`` → participants from ``From`` only."""
    msgs = [
        _make_message(
            msg_id="m1",
            internal_date="1000",
            headers={
                "Subject": "x",
                "From": "Alice <alice@example.com>",
                "Date": "Tue, 28 Apr 2026 09:00:00 -0400",
                # No To, no Cc.
            },
        ),
        _make_message(
            msg_id="m2",
            internal_date="2000",
            headers={
                "Subject": "Re: x",
                "From": "Bob <bob@example.com>",
                "Date": "Tue, 28 Apr 2026 10:00:00 -0400",
            },
        ),
    ]
    doc = to_extracted_thread(msgs)
    assert doc.metadata["participants"] == [
        "Alice <alice@example.com>",
        "Bob <bob@example.com>",
    ]


# --- Bonus: empty list raises ----------------------------------------------


def test_empty_message_list_raises() -> None:
    """Calling with no messages is a programmer error and raises ValueError."""
    with pytest.raises(ValueError, match="at least one message"):
        to_extracted_thread([])
