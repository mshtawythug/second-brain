"""Integration tests for the gmail-slug wiring in brain.vault.export.

The unit tests in ``test_gmail_slug.py`` pin the slug rules in isolation;
these tests exercise the full ingest → export path so the export module
actually invokes :func:`gmail_slug` for ``source_kind='gmail'`` rows and
lays the file at ``_ingested/gmail/<gmail-slug>.md``.
"""
import hashlib
from datetime import datetime
from pathlib import Path

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.vault.export import export_vault
from brain.vault.slug import gmail_slug


def _thread6(thread_id: str) -> str:
    """Helper — recompute the expected thread6 the same way production does."""
    return hashlib.sha1(thread_id.encode("utf-8")).hexdigest()[:6]


def _ingest_gmail(
    conn: psycopg.Connection,
    *,
    embedder,
    title: str,
    content: str,
    external_id: str,
    thread_id: str,
    sent_at_iso: str,
) -> str:
    """Ingest a single gmail document and return its document id.

    Mirrors the metadata shape produced by ``brain.ingest.gmail.to_extracted_doc``
    so the export branch sees realistic data.
    """
    res = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="email",
            source_path=None,
            metadata={
                "thread_id": thread_id,
                "message_id": external_id,
                "sent_at": sent_at_iso,
                "date": sent_at_iso,
                "from": "alice@example.com",
                "to": "bob@example.com",
            },
        ),
        source_kind="gmail",
        source_external_id=external_id,
        source_metadata={"thread_id": thread_id},
    )
    assert res.document_id is not None
    return res.document_id


def test_gmail_export_uses_gmail_slug_path(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A gmail doc with thread_id + sent_at lands at the gmail_slug path."""
    thread_id = "thread-19de56cd"
    sent_at = "2026-05-01T12:00:00+00:00"
    title = "Re: external — Re: Ali Sarkis vendor-ev"
    _ingest_gmail(
        test_db,
        embedder=fake_embedder,
        title=title,
        content="Sample email body.",
        external_id="msg-001",
        thread_id=thread_id,
        sent_at_iso=sent_at,
    )
    summary = export_vault(test_db, vault_path=tmp_path / "vault")
    assert summary.written == 1

    expected_slug = gmail_slug(
        thread_id,
        # Re-parse the same ISO string so the test compares production's path
        # against a value computed exactly the way export.py would.
        datetime.fromisoformat(sent_at),
        title,
    )
    expected = tmp_path / "vault" / "_ingested" / "gmail" / f"{expected_slug}.md"
    assert expected.is_file(), f"expected {expected} to exist"

    # Cross-check the slug shape: starts with the date, then thread6, then
    # the cleaned subject (no comma / day-of-week / Re: leakage).
    name = expected.name
    assert name.startswith(f"2026-05-01-{_thread6(thread_id)}-")
    assert "," not in name
    # The "Re:" prefixes are stripped from the subject portion.
    subject_portion = name.removesuffix(".md").split("-", 4)[-1]
    assert not subject_portion.startswith("re-")
    assert "vendor-ev" in subject_portion


def test_gmail_re_export_is_idempotent(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Re-exporting the same gmail doc lands at the same path; body unchanged."""
    thread_id = "thread-stable"
    sent_at = "2026-05-01T12:00:00+00:00"
    _ingest_gmail(
        test_db,
        embedder=fake_embedder,
        title="Quick question",
        content="Original body.",
        external_id="msg-002",
        thread_id=thread_id,
        sent_at_iso=sent_at,
    )
    first = export_vault(test_db, vault_path=tmp_path / "vault")
    assert first.written == 1
    files_first = list((tmp_path / "vault" / "_ingested" / "gmail").glob("*.md"))
    assert len(files_first) == 1

    # Second pass: same row, same content_hash → idempotent skip.
    second = export_vault(test_db, vault_path=tmp_path / "vault")
    assert second.written == 0
    assert second.skipped == 1
    files_second = list((tmp_path / "vault" / "_ingested" / "gmail").glob("*.md"))
    # Path is stable across re-exports.
    assert files_second == files_first


def test_gmail_legacy_doc_without_thread_id_falls_back(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A legacy gmail doc lacking ``thread_id`` keeps the old slug shape.

    Pre-P1.3 ingests have no ``thread_id`` in metadata. Those rows still
    need to export (just without the new stable slug); we fall back to the
    generic ``<date>-<external-id>-<slug>`` shape.
    """
    res = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Legacy gmail",
            content="legacy body",
            content_type="email",
            source_path=None,
            metadata={"date": "2026-04-15"},  # no thread_id
        ),
        source_kind="gmail",
        source_external_id="legacy-msg-id",
        source_metadata={},
    )
    assert res.document_id is not None

    summary = export_vault(test_db, vault_path=tmp_path / "vault")
    assert summary.written == 1
    files = list((tmp_path / "vault" / "_ingested" / "gmail").glob("*.md"))
    assert len(files) == 1
    # Falls back to the generic shape: <date>-<external8>-<slug>.md.
    # The external-id helper strips hyphens before truncating, so
    # "legacy-msg-id" → "legacymsgid"[:8] = "legacyms".
    assert files[0].name == "2026-04-15-legacyms-legacy-gmail.md"


def test_gmail_slug_does_not_change_other_sources(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Krisp / slack / manual paths are untouched by the gmail-slug branch."""
    res = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Krisp call",
            content="meeting transcript",
            content_type="transcript",
            source_path=None,
            metadata={
                "date": "2026-04-15",
                "thread_id": "thread-irrelevant-for-krisp",
            },
        ),
        source_kind="krisp",
        source_external_id="krisp12345",
        source_metadata={"date": "2026-04-15"},
    )
    assert res.document_id is not None

    summary = export_vault(test_db, vault_path=tmp_path / "vault")
    assert summary.written == 1
    krisp_files = list((tmp_path / "vault" / "_ingested" / "krisp").glob("*.md"))
    assert len(krisp_files) == 1
    # Old shape with the krisp external id (first 8 chars) preserved.
    assert krisp_files[0].name.startswith("2026-04-15-krisp123")
