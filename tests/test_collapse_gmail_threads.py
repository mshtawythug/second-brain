"""Integration tests for the destructive Gmail thread-collapse script.

Each test starts from a fresh schema (``test_db`` fixture). We seed
``content_type='email'`` rows that mirror the legacy per-message ingest
shape, then run the collapse and assert the post-state. The Gmail API
fetch is faked via :mod:`tests.test_gmail_thread_upsert`-style messages
shipped through a runner that returns canned JSON for
``gws gmail users messages get`` calls.

Mock surface (kept narrow on purpose):

- ``read_message`` is called via the ``runner`` argument the script
  passes through. The fake runner returns a JSON-encoded message
  resource for every ``--params {"id": ...}`` lookup.
- The :class:`tests.conftest.FakeEmbedder` powers chunk embeddings.
- The ``collapse_threads`` API is called directly (not via the CLI
  ``main`` entry point) so the per-thread orchestration is exercised
  with the script's own dependency-injected runner — no
  ``brain.ingest.gmail._run`` monkey-patching of production code.
"""
from __future__ import annotations

import base64
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.ingest import ExtractedDoc, ingest_document

# Add scripts/ to sys.path so the test file can import the script under test.
# The script is intentionally not part of the ``brain`` package — it's a
# standalone one-shot piece of plumbing per CLAUDE.md.
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import collapse_gmail_threads as collapse  # noqa: E402  (sys.path setup above)

from tests.conftest import FakeEmbedder  # noqa: E402

# ---------------------------------------------------------------------------
# Test fixtures — minimal Gmail Message resource builder + runner factory.
# ---------------------------------------------------------------------------


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _make_message(
    *,
    msg_id: str,
    thread_id: str,
    subject: str,
    sender: str,
    date_header: str,
    internal_date: str,
    body_text: str,
    label_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a single Gmail ``users.messages.get`` response."""
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": "pat@example.com"},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": date_header},
        {"name": "Message-ID", "value": f"<{msg_id}@example.com>"},
    ]
    return {
        "id": msg_id,
        "threadId": thread_id,
        "labelIds": list(label_ids) if label_ids is not None else ["INBOX"],
        "internalDate": internal_date,
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": _b64url(body_text)},
        },
    }


def _make_thread_messages(
    *, thread_id: str, message_count: int, subject_base: str = "Quarterly review"
) -> list[dict[str, Any]]:
    """Return ``message_count`` messages spanning hours 09:00–09:0N."""
    msgs: list[dict[str, Any]] = []
    for i in range(message_count):
        subject = (
            subject_base if i == 0 else f"Re: {subject_base}"
        )
        msgs.append(
            _make_message(
                msg_id=f"{thread_id}-m{i + 1}",
                thread_id=thread_id,
                subject=subject,
                sender="alice@example.com" if i % 2 == 0 else "bob@example.com",
                date_header=f"Tue, 28 Apr 2026 {9 + i:02d}:00:00 -0400",
                internal_date=str(1_700_000_000_000 + i * 1000),
                body_text=f"Message {i + 1} body for thread {thread_id}.",
            )
        )
    return msgs


def _fake_runner(
    messages_by_id: dict[str, dict[str, Any]],
    *,
    failing_ids: set[str] | None = None,
) -> Callable[[list[str]], str]:
    """Return a runner that handles ``gws gmail users messages get`` calls.

    ``failing_ids`` simulates a transient Gmail-side fetch error for a
    subset of message ids; the runner raises :class:`GmailError` for
    those, mirroring the production failure surface.
    """
    failing_ids = failing_ids or set()

    def runner(cmd: list[str]) -> str:
        # Shape: gws gmail users messages get --params {"id": "..."} --format json
        assert cmd[:5] == ["gws", "gmail", "users", "messages", "get"], cmd
        params = json.loads(cmd[6])
        msg_id = params["id"]
        if msg_id in failing_ids:
            from brain.ingest.gmail import GmailError

            raise GmailError(f"simulated transient fetch failure for {msg_id}")
        return json.dumps(messages_by_id[msg_id])

    return runner


def _seed_per_message_rows(
    conn: psycopg.Connection,
    embedder: FakeEmbedder,
    *,
    thread_id: str,
    message_count: int,
    tags_per_message: list[list[str]] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Seed N ``content_type='email'`` rows for the same Gmail thread.

    Returns ``(doc_ids, original_messages)`` so callers can assert which
    rows were collapsed and fake the runner to fetch them again.
    """
    msgs = _make_thread_messages(
        thread_id=thread_id, message_count=message_count
    )
    doc_ids: list[str] = []
    for idx, raw_msg in enumerate(msgs):
        # Build a per-message ExtractedDoc that matches what the legacy
        # gmail extractor would have produced.
        from brain.ingest.gmail import to_extracted_doc

        doc = to_extracted_doc(raw_msg)
        tags = list(tags_per_message[idx]) if tags_per_message else []
        result = ingest_document(
            conn,
            embedder=embedder,
            doc=doc,
            source_kind="gmail",
            source_external_id=raw_msg["id"],
            tags=tags,
        )
        assert result.document_id is not None
        doc_ids.append(result.document_id)
    return doc_ids, msgs


# ---------------------------------------------------------------------------
# 1. Dry-run: no DB or filesystem writes.
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    tmp_path: Path,
) -> None:
    """``--dry-run`` reports the plan without writing anything."""
    _, _ = _seed_per_message_rows(
        test_db, fake_embedder, thread_id="thread-A", message_count=4
    )
    _seed_per_message_rows(
        test_db, fake_embedder, thread_id="thread-B", message_count=1
    )
    before_docs = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert before_docs is not None
    before_count = before_docs[0]

    # Tap a sentinel file to confirm the dry-run never touches disk.
    sentinel = tmp_path / "sentinel.md"
    sentinel.write_text("hello", encoding="utf-8")

    report = collapse.collapse_threads(
        test_db,
        embedder=None,
        runner=None,
        vault_path=tmp_path,
        dry_run=True,
    )

    assert report.dry_run is True
    assert len(report.processed) == 1  # only the 4-message thread
    assert report.processed[0].thread_id == "thread-A"
    assert report.processed[0].msg_count_before == 4

    after_docs = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert after_docs is not None
    assert after_docs[0] == before_count
    assert sentinel.read_text() == "hello"


# ---------------------------------------------------------------------------
# 2. Collapses one thread of 4 messages into one merged thread row.
# ---------------------------------------------------------------------------


def test_collapses_single_thread(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    """4 per-message rows → 1 ``content_type='email_thread'`` row."""
    _, msgs = _seed_per_message_rows(
        test_db, fake_embedder, thread_id="thread-A", message_count=4
    )
    runner = _fake_runner({m["id"]: m for m in msgs})

    report = collapse.collapse_threads(
        test_db,
        embedder=fake_embedder,
        runner=runner,
        vault_path=None,
        dry_run=False,
    )
    assert not report.failed, [r.error for r in report.failed]
    assert len(report.processed) == 1
    assert report.processed[0].msg_count_before == 4
    assert report.processed[0].msg_count_after == 1

    rows = test_db.execute(
        "SELECT count(*) FROM documents WHERE thread_id = %s",
        ("thread-A",),
    ).fetchone()
    assert rows is not None
    assert rows[0] == 1
    merged = test_db.execute(
        "SELECT content_type, content, metadata FROM documents "
        "WHERE thread_id = %s",
        ("thread-A",),
    ).fetchone()
    assert merged is not None
    assert merged[0] == "email_thread"
    # The merged body has 4 sections — the latest as a plain H2, the
    # earlier 3 wrapped in <details>.
    assert merged[1].count("<details>") == 3
    assert merged[1].count("\n## ") == 1
    assert merged[2]["message_count"] == 4


# ---------------------------------------------------------------------------
# 3. Tag union preserved.
# ---------------------------------------------------------------------------


def test_tag_union_preserved(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    """Merged doc's tags = sorted normalized union of every old doc's tags."""
    tags_per_message = [
        ["recruiter", "email"],
        ["vendor-ev", "email"],
        ["email", "follow-up"],
        ["email"],
    ]
    _, msgs = _seed_per_message_rows(
        test_db,
        fake_embedder,
        thread_id="thread-A",
        message_count=4,
        tags_per_message=tags_per_message,
    )
    runner = _fake_runner({m["id"]: m for m in msgs})
    report = collapse.collapse_threads(
        test_db,
        embedder=fake_embedder,
        runner=runner,
        vault_path=None,
        dry_run=False,
    )
    assert not report.failed
    row = test_db.execute(
        "SELECT tags FROM documents WHERE thread_id = %s",
        ("thread-A",),
    ).fetchone()
    assert row is not None
    assert sorted(row[0]) == ["email", "follow-up", "recruiter", "vendor-ev"]


# ---------------------------------------------------------------------------
# 4. Link refactor — ``links`` row dst retargeted to merged doc.
# ---------------------------------------------------------------------------


def test_link_refactor_db(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    """A ``links`` row pointing at message #2 retargets to the merged doc."""
    doc_ids, msgs = _seed_per_message_rows(
        test_db, fake_embedder, thread_id="thread-A", message_count=4
    )
    target_msg_doc_id = doc_ids[1]
    # Manually insert a "third doc" + a links row from third → message #2.
    third = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Third doc",
            content="Some other body that mentions message 2.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    assert third.document_id is not None
    test_db.execute(
        "INSERT INTO links (src_document_id, dst_document_id, link_text, link_kind) "
        "VALUES (%s, %s, %s, %s)",
        (third.document_id, target_msg_doc_id, "Re: Quarterly review", "wiki"),
    )

    runner = _fake_runner({m["id"]: m for m in msgs})
    report = collapse.collapse_threads(
        test_db,
        embedder=fake_embedder,
        runner=runner,
        vault_path=None,
        dry_run=False,
    )
    assert not report.failed
    merged_id_row = test_db.execute(
        "SELECT id::text FROM documents WHERE thread_id = %s",
        ("thread-A",),
    ).fetchone()
    assert merged_id_row is not None
    merged_id = merged_id_row[0]
    row = test_db.execute(
        "SELECT dst_document_id::text FROM links "
        "WHERE src_document_id = %s",
        (third.document_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == merged_id


# ---------------------------------------------------------------------------
# 5. Link refactor — ``derived_links`` row dst retargeted.
# ---------------------------------------------------------------------------


def test_link_refactor_derived(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    """A ``derived_links`` row pointing at message #2 retargets to the merged doc."""
    doc_ids, msgs = _seed_per_message_rows(
        test_db, fake_embedder, thread_id="thread-A", message_count=4
    )
    target_msg_doc_id = doc_ids[1]
    third = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Third doc",
            content="A different body — derived edge candidate.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    assert third.document_id is not None
    test_db.execute(
        "INSERT INTO derived_links "
        "(src_document_id, dst_document_id, rule, evidence, weight) "
        "VALUES (%s, %s, %s, %s::jsonb, %s)",
        (
            third.document_id,
            target_msg_doc_id,
            "shared_thread",
            json.dumps({"reason": "test"}),
            0.8,
        ),
    )
    runner = _fake_runner({m["id"]: m for m in msgs})
    report = collapse.collapse_threads(
        test_db,
        embedder=fake_embedder,
        runner=runner,
        vault_path=None,
        dry_run=False,
    )
    assert not report.failed
    merged_id_row = test_db.execute(
        "SELECT id::text FROM documents WHERE thread_id = %s",
        ("thread-A",),
    ).fetchone()
    assert merged_id_row is not None
    merged_id = merged_id_row[0]
    row = test_db.execute(
        "SELECT dst_document_id::text FROM derived_links "
        "WHERE src_document_id = %s",
        (third.document_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == merged_id


# ---------------------------------------------------------------------------
# 6. Link refactor — vault file rewrite.
# ---------------------------------------------------------------------------


def test_link_refactor_vault_files(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    tmp_path: Path,
) -> None:
    """A vault note referencing the old per-message title gets rewritten."""
    # Use a thread whose first-message subject already includes a "Re:"
    # so it differs from the merged-thread title (which strips "Re:").
    # The merged title is the *first* message's subject after stripping
    # leading Re/Fwd prefixes — for a thread of N "Re: Old Subject" rows,
    # that becomes "Old Subject". A vault note referencing the literal
    # "Re: Old Subject" should be rewritten to "[[Old Subject]]".
    msgs = []
    thread_id = "thread-RE"
    for i in range(4):
        msgs.append(
            _make_message(
                msg_id=f"{thread_id}-m{i + 1}",
                thread_id=thread_id,
                # All four messages prefix with Re: so the merged title
                # strips to "Old Subject".
                subject="Re: Old Subject",
                sender="alice@example.com" if i % 2 == 0 else "bob@example.com",
                date_header=f"Tue, 28 Apr 2026 {9 + i:02d}:00:00 -0400",
                internal_date=str(1_700_000_000_000 + i * 1000),
                body_text=f"Body for message {i + 1}.",
            )
        )
    # Manually seed via to_extracted_doc so the per-message rows match
    # what the legacy ingest produced.
    from brain.ingest.gmail import to_extracted_doc

    for m in msgs:
        result = ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=to_extracted_doc(m),
            source_kind="gmail",
            source_external_id=m["id"],
        )
        assert result.document_id is not None

    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    note_path = vault_path / "pat-vs-recruiter.md"
    note_path.write_text(
        "---\n"
        "id: 11111111-1111-1111-1111-111111111111\n"
        "title: Reference Note\n"
        "kind: vault\n"
        "---\n"
        "Earlier I wrote about [[Re: Old Subject]] in detail.\n",
        encoding="utf-8",
    )

    runner = _fake_runner({m["id"]: m for m in msgs})
    report = collapse.collapse_threads(
        test_db,
        embedder=fake_embedder,
        runner=runner,
        vault_path=vault_path,
        dry_run=False,
    )
    assert not report.failed, [r.error for r in report.failed]
    assert report.processed[0].refs_rewritten == 1

    rewritten = note_path.read_text(encoding="utf-8")
    # The reference now points at the merged-thread title, which had
    # the "Re: " prefix stripped during assembly.
    assert "[[Old Subject]]" in rewritten
    assert "[[Re: Old Subject]]" not in rewritten


# ---------------------------------------------------------------------------
# 7. Unresolved links cleared.
# ---------------------------------------------------------------------------


def test_unresolved_links_cleared(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    """An unresolved_links row whose link_text matches an old slug is dropped."""
    _, msgs = _seed_per_message_rows(
        test_db, fake_embedder, thread_id="thread-A", message_count=4
    )

    # Manual third doc that has an unresolved reference to the OLD title
    # form. ``link_text`` mirrors what the link parser would store —
    # ``"Re: Quarterly review"`` is one of the per-message titles seeded.
    third = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Holder",
            content="Holds the unresolved edge.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    assert third.document_id is not None
    test_db.execute(
        "INSERT INTO unresolved_links (src_document_id, link_text, link_kind) "
        "VALUES (%s, %s, %s)",
        (third.document_id, "Re: Quarterly review", "wiki"),
    )

    runner = _fake_runner({m["id"]: m for m in msgs})
    report = collapse.collapse_threads(
        test_db,
        embedder=fake_embedder,
        runner=runner,
        vault_path=None,
        dry_run=False,
    )
    assert not report.failed
    remaining = test_db.execute(
        "SELECT count(*) FROM unresolved_links WHERE link_text = %s",
        ("Re: Quarterly review",),
    ).fetchone()
    assert remaining is not None
    assert remaining[0] == 0


# ---------------------------------------------------------------------------
# 8. Idempotency — re-running on a collapsed corpus is a no-op.
# ---------------------------------------------------------------------------


def test_idempotent_re_run(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    """Second invocation reports zero processed threads."""
    _, msgs = _seed_per_message_rows(
        test_db, fake_embedder, thread_id="thread-A", message_count=4
    )
    runner = _fake_runner({m["id"]: m for m in msgs})

    first = collapse.collapse_threads(
        test_db,
        embedder=fake_embedder,
        runner=runner,
        vault_path=None,
        dry_run=False,
    )
    assert len(first.processed) == 1
    assert not first.failed

    second = collapse.collapse_threads(
        test_db,
        embedder=fake_embedder,
        runner=runner,
        vault_path=None,
        dry_run=False,
    )
    assert second.processed == []
    assert second.failed == []


# ---------------------------------------------------------------------------
# 9. Per-thread failure does not abort the whole run.
# ---------------------------------------------------------------------------


def test_per_thread_failure_does_not_abort(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    """A failed thread is reported; surviving threads still collapse."""
    _, msgs_a = _seed_per_message_rows(
        test_db, fake_embedder, thread_id="thread-A", message_count=4
    )
    _, msgs_b = _seed_per_message_rows(
        test_db, fake_embedder, thread_id="thread-B", message_count=4
    )
    # Combine and fake-fail every thread-B message id.
    combined = {m["id"]: m for m in (*msgs_a, *msgs_b)}
    failing = {m["id"] for m in msgs_b}
    runner = _fake_runner(combined, failing_ids=failing)

    report = collapse.collapse_threads(
        test_db,
        embedder=fake_embedder,
        runner=runner,
        vault_path=None,
        dry_run=False,
    )
    assert {r.thread_id for r in report.processed} == {"thread-A"}
    assert {r.thread_id for r in report.failed} == {"thread-B"}
    # Thread A successfully collapsed.
    a_count = test_db.execute(
        "SELECT count(*) FROM documents WHERE thread_id = %s",
        ("thread-A",),
    ).fetchone()
    assert a_count is not None
    assert a_count[0] == 1
    # Thread B is left intact (4 per-message rows still present).
    b_count = test_db.execute(
        "SELECT count(*) FROM documents WHERE thread_id = %s "
        "AND content_type = 'email'",
        ("thread-B",),
    ).fetchone()
    assert b_count is not None
    assert b_count[0] == 4


# ---------------------------------------------------------------------------
# 10. Verification step aborts on synthetic dangling FK.
# ---------------------------------------------------------------------------


def test_verification_aborts_on_dangling_fk(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    """Surface a dangling-FK style failure via a runner-side raise.

    The verification helper itself is unit-tested at the SQL level: we
    feed it a list of "deleted" doc ids that still have ``links`` rows
    pointing at them and assert it raises :class:`BrainError`.
    """
    # Seed two manual docs and a links row src→dst.
    src = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Src",
            content="Src body content here.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    dst = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Dst",
            content="Dst body content here.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    assert src.document_id is not None and dst.document_id is not None
    test_db.execute(
        "INSERT INTO links (src_document_id, dst_document_id, link_text, link_kind) "
        "VALUES (%s, %s, %s, %s)",
        (src.document_id, dst.document_id, "Dst", "wiki"),
    )

    # The verification helper sees that ``dst.document_id`` is supposedly
    # deleted while a links row still points at it — raises BrainError.
    from brain.errors import BrainError

    with pytest.raises(BrainError, match="verification failed"):
        collapse._verify_no_dangling_fk(
            test_db, deleted_ids=[dst.document_id]
        )


# ---------------------------------------------------------------------------
# 11. directory_entries.occurrence_count recomputed.
# ---------------------------------------------------------------------------


def test_directory_entries_recomputed(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    """After collapse, gmail directory_entries reflect the surviving thread doc.

    Each per-message ingest bumps the (display, email) directory pair
    once. After collapse, the directory recompute clears the gmail rows
    and re-derives them from the surviving merged thread doc — so the
    count matches one occurrence per (display, email) per thread doc.
    """
    _, msgs = _seed_per_message_rows(
        test_db, fake_embedder, thread_id="thread-A", message_count=4
    )
    # Pre-collapse: 4 messages → 4 ingest hooks → directory has bumped
    # counts for each (display, email) pair.
    pre = test_db.execute(
        "SELECT count(*), max(occurrence_count) "
        "FROM directory_entries WHERE source = 'gmail'"
    ).fetchone()
    assert pre is not None
    assert pre[0] >= 1
    pre_max = pre[1]
    assert pre_max >= 1

    runner = _fake_runner({m["id"]: m for m in msgs})
    report = collapse.collapse_threads(
        test_db,
        embedder=fake_embedder,
        runner=runner,
        vault_path=None,
        dry_run=False,
    )
    assert not report.failed

    # Post-collapse: each (display, email) appears in exactly one
    # surviving doc → occurrence_count == 1 across the board.
    post = test_db.execute(
        "SELECT count(*), max(occurrence_count) "
        "FROM directory_entries WHERE source = 'gmail'"
    ).fetchone()
    assert post is not None
    assert post[0] >= 1
    assert post[1] == 1


# ---------------------------------------------------------------------------
# 12. Singleton threads are skipped.
# ---------------------------------------------------------------------------


def test_singleton_threads_skipped(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    """A thread with one per-message row is left as-is."""
    _, msgs_a = _seed_per_message_rows(
        test_db, fake_embedder, thread_id="thread-A", message_count=4
    )
    _, msgs_b = _seed_per_message_rows(
        test_db, fake_embedder, thread_id="thread-B", message_count=1
    )
    runner = _fake_runner({m["id"]: m for m in (*msgs_a, *msgs_b)})
    report = collapse.collapse_threads(
        test_db,
        embedder=fake_embedder,
        runner=runner,
        vault_path=None,
        dry_run=False,
    )
    assert {r.thread_id for r in report.processed} == {"thread-A"}
    # The single-message thread-B row is still there as content_type='email'.
    row = test_db.execute(
        "SELECT count(*), max(content_type) FROM documents "
        "WHERE thread_id = %s",
        ("thread-B",),
    ).fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] == "email"


# ---------------------------------------------------------------------------
# 13. Drafts inside a legacy thread are filtered before assembly.
# ---------------------------------------------------------------------------


def test_collapse_drops_draft_from_thread(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    """A pre-existing thread with [sent, sent, draft, sent] collapses to 3 sections.

    Mirrors the COMPANY_REDACTED-thread case from prod: the legacy per-message rows
    were ingested before the draft filter shipped, so a discarded draft
    sits among real sent messages. The collapse runner re-fetches every
    message via ``gws gmail users messages get`` — Gmail still returns
    the draft (it exists in the user's drafts folder), so the per-thread
    extractor must filter it before assembly. The merged doc carries
    only the 3 sent messages; the 4 per-message rows (including the
    draft) are deleted post-collapse because the spec is "collapse legacy
    rows into the merged thread doc". The draft message *cannot* be
    fetched from Gmail in real life if it was discarded (we get 'not
    found') — that's the failure mode the user originally hit; this test
    pins the success path where the draft is still fetchable.
    """
    msgs = _make_thread_messages(thread_id="thread-A", message_count=4)
    # Mark the third message as a DRAFT.
    msgs[2]["labelIds"] = ["DRAFT"]

    # Seed all four as legacy per-message rows. We bypass `to_extracted_doc`
    # for the draft (which now raises) by building its ExtractedDoc directly
    # — the point of this test is that legacy rows already exist and
    # collapse-time filtering must drop the draft.
    from brain.ingest import ExtractedDoc
    from brain.ingest.gmail import _extract_body, strip_boilerplate, to_extracted_doc

    doc_ids: list[str] = []
    for raw in msgs:
        if "DRAFT" in (raw.get("labelIds") or []):
            payload = raw.get("payload") or {}
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
            doc = ExtractedDoc(
                title=headers.get("subject") or "(no subject)",
                content=strip_boilerplate(_extract_body(payload).strip()),
                content_type="email",
                source_path=None,
                metadata={
                    "from": headers.get("from"),
                    "to": headers.get("to"),
                    "date": headers.get("date"),
                    "message_id": raw["id"],
                    "thread_id": raw["threadId"],
                    "label_ids": raw.get("labelIds") or [],
                },
            )
        else:
            doc = to_extracted_doc(raw)
        result = ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=doc,
            source_kind="gmail",
            source_external_id=raw["id"],
        )
        assert result.document_id is not None
        doc_ids.append(result.document_id)

    # Sanity: 4 per-message rows exist before collapse.
    pre = test_db.execute(
        "SELECT count(*) FROM documents WHERE thread_id=%s AND content_type='email'",
        ("thread-A",),
    ).fetchone()
    assert pre is not None
    assert pre[0] == 4

    # The runner returns every message — including the draft — when the
    # collapse script re-fetches them. The extractor (not the runner)
    # filters drafts.
    runner = _fake_runner({m["id"]: m for m in msgs})
    report = collapse.collapse_threads(
        test_db,
        embedder=fake_embedder,
        runner=runner,
        vault_path=None,
        dry_run=False,
    )
    assert not report.failed, [r.error for r in report.failed]
    assert not report.skipped_drafts, [r.error for r in report.skipped_drafts]
    assert len(report.processed) == 1
    assert report.processed[0].msg_count_before == 4
    assert report.processed[0].msg_count_after == 1

    # Merged doc has exactly 3 sections: 1 plain H2 + 2 <details>.
    merged = test_db.execute(
        "SELECT content, metadata FROM documents "
        "WHERE thread_id=%s AND content_type='email_thread'",
        ("thread-A",),
    ).fetchone()
    assert merged is not None
    body, metadata = merged
    assert body.count("<details>") == 2
    assert metadata["message_count"] == 3
    # The draft body (Message 3) is filtered out.
    assert "Message 3 body for thread thread-A." not in body
    assert "Message 1 body for thread thread-A." in body
    assert "Message 2 body for thread thread-A." in body
    assert "Message 4 body for thread thread-A." in body

    # All four old per-message rows are deleted post-collapse.
    leftover = test_db.execute(
        "SELECT count(*) FROM documents WHERE thread_id=%s AND content_type='email'",
        ("thread-A",),
    ).fetchone()
    assert leftover is not None
    assert leftover[0] == 0


def test_collapse_skips_all_draft_thread(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    """An all-draft legacy thread is reported as skipped (drafts), not failed.

    The script's exit code does NOT flip non-zero because draft skips are
    semantically distinct from real failures — the drafts were filtered
    by the extractor on purpose, not because of a Gmail-side error.
    """
    msgs = _make_thread_messages(thread_id="thread-D", message_count=2)
    for raw in msgs:
        raw["labelIds"] = ["DRAFT"]

    # Seed two legacy per-message rows that pre-date the draft filter.
    from brain.ingest import ExtractedDoc
    from brain.ingest.gmail import _extract_body, strip_boilerplate

    for raw in msgs:
        payload = raw.get("payload") or {}
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        doc = ExtractedDoc(
            title=headers.get("subject") or "(no subject)",
            content=strip_boilerplate(_extract_body(payload).strip()),
            content_type="email",
            source_path=None,
            metadata={
                "from": headers.get("from"),
                "to": headers.get("to"),
                "date": headers.get("date"),
                "message_id": raw["id"],
                "thread_id": raw["threadId"],
                "label_ids": raw.get("labelIds") or [],
            },
        )
        ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=doc,
            source_kind="gmail",
            source_external_id=raw["id"],
        )

    runner = _fake_runner({m["id"]: m for m in msgs})
    report = collapse.collapse_threads(
        test_db,
        embedder=fake_embedder,
        runner=runner,
        vault_path=None,
        dry_run=False,
    )
    assert not report.processed
    assert not report.failed
    assert len(report.skipped_drafts) == 1
    skipped = report.skipped_drafts[0]
    assert skipped.thread_id == "thread-D"
    assert skipped.skipped_draft is True
    assert skipped.error is not None and "draft-only" in skipped.error
    # Legacy rows are LEFT IN PLACE — collapse_gmail_threads is for
    # collapsing real-thread legacy rows; cleaning out drafts is a
    # different operator action (already done by the user).
    leftover = test_db.execute(
        "SELECT count(*) FROM documents WHERE thread_id=%s",
        ("thread-D",),
    ).fetchone()
    assert leftover is not None
    assert leftover[0] == 2
