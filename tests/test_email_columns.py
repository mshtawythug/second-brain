"""Tests for migration 007 + the email/krisp metadata→typed-column promotion.

Migration 007 adds six narrow columns on ``documents`` so the linker, graph,
and search filters can query email-thread / call-duration metadata without
walking the JSONB ``metadata`` blob:

    thread_id, rfc_message_id, in_reply_to, sent_at, participants,
    duration_min, draft

These tests exercise both the schema-level migration (idempotency, column
types, partial indexes) and the ingest-pipeline wiring that promotes
recognized metadata keys onto the typed columns on INSERT and UPDATE.
"""
import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from brain.db import connect, migrations_dir, run_migrations
from brain.ingest import ExtractedDoc, ingest_document, update_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

# Tests here own-connection ``DROP SCHEMA`` + migrate and re-apply migration 007
# SQL directly (schema mutation).
pytestmark = pytest.mark.fresh_schema


# --- migration ---------------------------------------------------------------


def test_migration_applies(test_db: psycopg.Connection) -> None:
    """007 adds the seven typed columns + their partial indexes; column types
    match the spec (TEXT, TIMESTAMPTZ, TEXT[], INTEGER, BOOLEAN)."""
    rows = test_db.execute(
        """
        SELECT column_name, data_type, udt_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='documents'
        AND column_name IN (
            'thread_id', 'rfc_message_id', 'in_reply_to', 'sent_at',
            'participants', 'duration_min', 'draft'
        )
        ORDER BY column_name
        """
    ).fetchall()
    by_name = {r[0]: r for r in rows}

    assert set(by_name) == {
        "draft",
        "duration_min",
        "in_reply_to",
        "participants",
        "rfc_message_id",
        "sent_at",
        "thread_id",
    }

    assert by_name["thread_id"][1] == "text"
    assert by_name["rfc_message_id"][1] == "text"
    assert by_name["in_reply_to"][1] == "text"
    assert by_name["sent_at"][1] == "timestamp with time zone"
    # TEXT[] surfaces as ARRAY in data_type; udt_name preserves the element.
    assert by_name["participants"][1] == "ARRAY"
    assert by_name["participants"][2] == "_text"
    assert by_name["duration_min"][1] == "integer"
    assert by_name["draft"][1] == "boolean"

    # All thread/message metadata is nullable except draft.
    for col in (
        "thread_id",
        "rfc_message_id",
        "in_reply_to",
        "sent_at",
        "participants",
        "duration_min",
    ):
        assert by_name[col][3] == "YES", f"{col} should be nullable"
    assert by_name["draft"][3] == "NO", "draft must be NOT NULL"
    assert by_name["draft"][4] == "false", "draft must default to FALSE"

    # Partial indexes — names match the migration.
    index_rows = test_db.execute(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname='public' AND tablename='documents' "
        "AND indexname IN ("
        "  'idx_documents_thread_id', 'idx_documents_sent_at', "
        "  'idx_documents_draft'"
        ")"
    ).fetchall()
    assert sorted(r[0] for r in index_rows) == [
        "idx_documents_draft",
        "idx_documents_sent_at",
        "idx_documents_thread_id",
    ]


def test_idempotent_migration() -> None:
    """Running the migration runner twice is a no-op on the second pass —
    schema_migrations dedups, and the SQL itself uses ``IF NOT EXISTS`` for
    every column + index so even a forced re-run wouldn't crash."""
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        first = run_migrations(conn)
        second = run_migrations(conn)
    assert "007_email_thread_and_draft.sql" in first
    assert second == []


def test_migration_is_safe_to_re_execute_directly() -> None:
    """Direct re-execution of the SQL file (bypassing schema_migrations)
    is also safe — every DDL statement is idempotent. This guards against
    a future operator who runs the SQL by hand or via a different runner.
    """
    sql = (migrations_dir() / "007_email_thread_and_draft.sql").read_text()
    with connect(TEST_DATABASE_URL) as conn:
        conn.autocommit = True
        # First run is provided by the session-scoped fixture; this one is
        # the literal "second hand-execution" we're testing.
        conn.execute(sql)
        conn.execute(sql)


# --- INSERT path -------------------------------------------------------------


def test_metadata_promoted_on_insert_gmail(test_db, fake_embedder) -> None:
    """Gmail-shaped metadata (thread_id, rfc_message_id, in_reply_to, RFC 2822
    Date) populates the typed columns on INSERT. ``sent_at`` ends up
    TZ-aware and normalized to UTC regardless of the input offset."""
    doc = ExtractedDoc(
        title="Re: launch plan",
        content="body of the email",
        content_type="email",
        source_path=None,
        metadata={
            "thread_id": "thr-abc-123",
            "rfc_message_id": "<CADf=msg-1@mail.example.com>",
            "in_reply_to": "<CADf=msg-0@mail.example.com>",
            "date": "Tue, 04 May 2026 14:23:01 -0400",
        },
    )
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="gmail",
        source_external_id="msg-1",
    )
    assert result.document_id is not None

    row = test_db.execute(
        "SELECT thread_id, rfc_message_id, in_reply_to, sent_at "
        "FROM documents WHERE id=%s",
        (result.document_id,),
    ).fetchone()
    assert row is not None
    thread_id, rfc_message_id, in_reply_to, sent_at = row
    assert thread_id == "thr-abc-123"
    assert rfc_message_id == "<CADf=msg-1@mail.example.com>"
    assert in_reply_to == "<CADf=msg-0@mail.example.com>"
    assert sent_at is not None
    assert sent_at.tzinfo is not None
    # 14:23:01 -0400 == 18:23:01 UTC.
    assert sent_at.astimezone(UTC) == datetime(2026, 5, 4, 18, 23, 1, tzinfo=UTC)


def test_metadata_promoted_on_insert_iso_date(test_db, fake_embedder) -> None:
    """ISO 8601 dates (Krisp/manual style) are also accepted by the date
    parser — falls back from ``parsedate_to_datetime`` to
    ``datetime.fromisoformat``."""
    doc = ExtractedDoc(
        title="iso-date doc",
        content="body",
        content_type="email",
        source_path=None,
        metadata={"date": "2026-05-04T14:23:01+00:00"},
    )
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="manual",
        source_external_id="iso-1",
    )
    assert result.document_id is not None
    sent_at = test_db.execute(
        "SELECT sent_at FROM documents WHERE id=%s", (result.document_id,)
    ).fetchone()[0]
    assert sent_at == datetime(2026, 5, 4, 14, 23, 1, tzinfo=UTC)


def test_metadata_promoted_on_insert_krisp(test_db, fake_embedder) -> None:
    """Krisp-shaped metadata (participants list + duration_min int) populates
    the array + integer columns on INSERT."""
    doc = ExtractedDoc(
        title="Q2 sync",
        content="transcript body",
        content_type="transcript",
        source_path=None,
        metadata={
            "participants": ["Pat Morgan", "person-x last-b"],
            "duration_min": 42,
        },
    )
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="krisp",
        source_external_id="meeting-42",
    )
    assert result.document_id is not None

    row = test_db.execute(
        "SELECT participants, duration_min FROM documents WHERE id=%s",
        (result.document_id,),
    ).fetchone()
    assert row is not None
    participants, duration_min = row
    assert participants == ["Pat Morgan", "person-x last-b"]
    assert duration_min == 42


def test_draft_defaults_to_false_on_insert(test_db, fake_embedder) -> None:
    """``draft`` is added as NOT NULL DEFAULT FALSE; ingests don't have to
    write it explicitly. P1.6 will flip it via a separate CLI."""
    doc = ExtractedDoc(
        title="not a draft",
        content="body",
        content_type="email",
        source_path=None,
        metadata={"thread_id": "t1"},
    )
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="gmail",
        source_external_id="msg-not-draft",
    )
    assert result.document_id is not None
    draft = test_db.execute(
        "SELECT draft FROM documents WHERE id=%s", (result.document_id,)
    ).fetchone()[0]
    assert draft is False


def test_insert_with_no_promoted_metadata_leaves_columns_null(
    test_db, fake_embedder
) -> None:
    """A vanilla manual ingest (no thread_id / participants / etc.) leaves
    every promoted column NULL — the dynamic-suffix INSERT must not write
    spurious values when the helper returns an empty dict."""
    doc = ExtractedDoc(
        title="plain note",
        content="just text",
        content_type="note",
        source_path=None,
        metadata={"unrelated_key": "value"},
    )
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="manual",
        source_external_id="plain-1",
    )
    assert result.document_id is not None
    row = test_db.execute(
        "SELECT thread_id, rfc_message_id, in_reply_to, sent_at, "
        "participants, duration_min FROM documents WHERE id=%s",
        (result.document_id,),
    ).fetchone()
    assert row == (None, None, None, None, None, None)


# --- malformed metadata ------------------------------------------------------


def test_malformed_date_does_not_crash(test_db, fake_embedder) -> None:
    """An unparseable ``date`` metadata value must NOT crash ingest — the
    column is left NULL and the doc is stored with the rest of its
    metadata intact."""
    doc = ExtractedDoc(
        title="bad date",
        content="email body",
        content_type="email",
        source_path=None,
        metadata={
            "thread_id": "t-bad-date",
            "date": "garbage not a date at all",
        },
    )
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="gmail",
        source_external_id="bad-date-1",
    )
    assert result.document_id is not None

    row = test_db.execute(
        "SELECT thread_id, sent_at, metadata FROM documents WHERE id=%s",
        (result.document_id,),
    ).fetchone()
    assert row is not None
    thread_id, sent_at, metadata = row
    # thread_id still promoted.
    assert thread_id == "t-bad-date"
    # sent_at left NULL because the date was unparseable.
    assert sent_at is None
    # The original raw string is preserved in the JSONB blob — backfill
    # tooling later could try a different parser.
    assert metadata["date"] == "garbage not a date at all"


def test_malformed_participants_skipped(test_db, fake_embedder) -> None:
    """``participants`` must be ``list[str]``; a non-list value (e.g. raw
    string) is skipped rather than coerced. Matches the helper's defensive
    contract — never silently corrupt the column."""
    doc = ExtractedDoc(
        title="bad participants",
        content="x",
        content_type="transcript",
        source_path=None,
        metadata={
            "duration_min": 10,
            "participants": "Pat, person-x",  # WRONG: should be a list
        },
    )
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="krisp",
        source_external_id="bad-participants-1",
    )
    assert result.document_id is not None
    row = test_db.execute(
        "SELECT participants, duration_min FROM documents WHERE id=%s",
        (result.document_id,),
    ).fetchone()
    assert row == (None, 10)


def test_malformed_duration_skipped(test_db, fake_embedder) -> None:
    """``duration_min`` must coerce to ``int``; non-numeric strings are
    skipped, NOT coerced to 0 or stored as NaN."""
    doc = ExtractedDoc(
        title="bad duration",
        content="x",
        content_type="transcript",
        source_path=None,
        metadata={
            "participants": ["A", "B"],
            "duration_min": "twelve minutes",
        },
    )
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="krisp",
        source_external_id="bad-duration-1",
    )
    assert result.document_id is not None
    row = test_db.execute(
        "SELECT participants, duration_min FROM documents WHERE id=%s",
        (result.document_id,),
    ).fetchone()
    assert row == (["A", "B"], None)


def test_non_string_thread_id_skipped(test_db, fake_embedder, caplog) -> None:
    """A non-string ``thread_id`` is logged + skipped, not coerced via
    ``str()``. The doc still ingests."""
    import logging

    caplog.set_level(logging.WARNING, logger="brain.ingest")
    doc = ExtractedDoc(
        title="non-string thread_id",
        content="x",
        content_type="email",
        source_path=None,
        metadata={"thread_id": 12345},
    )
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="gmail",
        source_external_id="non-string-1",
    )
    assert result.document_id is not None
    thread_id = test_db.execute(
        "SELECT thread_id FROM documents WHERE id=%s", (result.document_id,)
    ).fetchone()[0]
    assert thread_id is None
    assert any(
        "metadata.thread_id expected str" in r.message for r in caplog.records
    )


# --- UPDATE path -------------------------------------------------------------


def test_metadata_promoted_on_update(test_db, fake_embedder) -> None:
    """``update_document`` with a metadata_patch that adds promoted keys
    must write the typed columns alongside the JSONB blob."""
    # Seed a doc with no promoted metadata.
    doc = ExtractedDoc(
        title="email to be patched",
        content="initial body",
        content_type="email",
        source_path=None,
        metadata={"unrelated": "v"},
    )
    seed = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="gmail",
        source_external_id="patch-target-1",
    )
    assert seed.document_id is not None

    # Confirm columns start NULL.
    pre = test_db.execute(
        "SELECT thread_id, sent_at, duration_min FROM documents WHERE id=%s",
        (seed.document_id,),
    ).fetchone()
    assert pre == (None, None, None)

    # Patch in metadata that promotes onto every column.
    result = update_document(
        test_db,
        document_id=seed.document_id,
        metadata_patch={
            "thread_id": "thr-patched",
            "rfc_message_id": "<patched@example.com>",
            "in_reply_to": "<orig@example.com>",
            "date": "Wed, 05 May 2026 09:00:00 +0000",
            "participants": ["Pat", "person-x"],
            "duration_min": 30,
        },
    )
    assert result.fields_changed == ["metadata"]

    row = test_db.execute(
        """
        SELECT thread_id, rfc_message_id, in_reply_to, sent_at,
               participants, duration_min
        FROM documents WHERE id=%s
        """,
        (seed.document_id,),
    ).fetchone()
    assert row is not None
    thread_id, rfc_id, in_reply, sent_at, participants, duration = row
    assert thread_id == "thr-patched"
    assert rfc_id == "<patched@example.com>"
    assert in_reply == "<orig@example.com>"
    assert sent_at == datetime(2026, 5, 5, 9, 0, 0, tzinfo=UTC)
    assert participants == ["Pat", "person-x"]
    assert duration == 30


def test_update_replace_metadata_clears_dropped_promoted_columns(
    test_db, fake_embedder
) -> None:
    """When ``replace_metadata=True`` drops a promoted key, the typed column
    must be NULLed so it doesn't drift out of sync with the JSONB blob."""
    doc = ExtractedDoc(
        title="replaced",
        content="body",
        content_type="email",
        source_path=None,
        metadata={
            "thread_id": "t-original",
            "rfc_message_id": "<orig@example.com>",
            "duration_min": 15,
        },
    )
    seed = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="gmail",
        source_external_id="replace-target-1",
    )
    assert seed.document_id is not None

    # Sanity check: typed columns populated from initial ingest.
    pre = test_db.execute(
        "SELECT thread_id, rfc_message_id, duration_min "
        "FROM documents WHERE id=%s",
        (seed.document_id,),
    ).fetchone()
    assert pre == ("t-original", "<orig@example.com>", 15)

    # Replace the metadata blob entirely with one that has no promoted keys.
    result = update_document(
        test_db,
        document_id=seed.document_id,
        metadata_patch={"only": "this-key"},
        replace_metadata=True,
    )
    assert result.fields_changed == ["metadata"]
    post = test_db.execute(
        "SELECT thread_id, rfc_message_id, duration_min "
        "FROM documents WHERE id=%s",
        (seed.document_id,),
    ).fetchone()
    assert post == (None, None, None)


def test_update_with_no_metadata_patch_leaves_columns_alone(
    test_db, fake_embedder
) -> None:
    """A title-only edit must NOT touch the typed columns. Regression guard
    against accidentally clearing them when metadata isn't being changed."""
    doc = ExtractedDoc(
        title="orig",
        content="body",
        content_type="email",
        source_path=None,
        metadata={"thread_id": "thr-keep"},
    )
    seed = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="gmail",
        source_external_id="title-only-1",
    )
    assert seed.document_id is not None

    update_document(
        test_db, document_id=seed.document_id, new_title="renamed"
    )
    thread_id = test_db.execute(
        "SELECT thread_id FROM documents WHERE id=%s", (seed.document_id,)
    ).fetchone()[0]
    assert thread_id == "thr-keep"


# --- helper unit tests (covers the non-DB branches of the helper) -----------


def test_helper_handles_naive_iso_date_as_utc() -> None:
    """A naive ISO 8601 string (no offset) is treated as UTC, matching the
    helper's documented contract — better than rejecting it and losing the
    metadata."""
    from brain.ingest import _parse_sent_at

    parsed = _parse_sent_at("2026-05-04T14:23:01")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed == datetime(2026, 5, 4, 14, 23, 1, tzinfo=UTC)


def test_helper_handles_iso_z_suffix() -> None:
    """ISO 8601 with the trailing-Z UTC marker round-trips — Python's
    ``fromisoformat`` accepts ``Z`` in 3.11+ but only via the helper's
    explicit replacement, so cover the path."""
    from brain.ingest import _parse_sent_at

    parsed = _parse_sent_at("2026-05-04T14:23:01Z")
    assert parsed == datetime(2026, 5, 4, 14, 23, 1, tzinfo=UTC)


def test_helper_returns_none_for_non_string_date() -> None:
    """Non-string inputs (None, int, list) all return ``None`` — caller
    decides whether to log."""
    from brain.ingest import _parse_sent_at

    assert _parse_sent_at(None) is None
    assert _parse_sent_at("") is None
    assert _parse_sent_at(12345) is None
    assert _parse_sent_at([2026, 5, 4]) is None


def test_helper_rejects_bool_duration() -> None:
    """``True`` is a Python int (subclass), but storing it as ``1`` minutes
    would be a silent bug — the helper rejects bools explicitly."""
    from brain.ingest import _promote_metadata_to_columns

    out = _promote_metadata_to_columns({"duration_min": True})
    assert "duration_min" not in out


def test_helper_coerces_numeric_string_duration_to_int() -> None:
    """Regression (Task 2.12c): a numeric string like ``"42.7"`` must round
    down to ``42`` via ``int(float(...))`` — matching the docstring's promise
    that floats round down. ``int("42.7")`` alone raises ``ValueError`` and
    silently dropped the column, which was the bug."""
    from brain.ingest import _promote_metadata_to_columns

    # Float-valued string → round down (the bug: previously dropped as ValueError).
    assert _promote_metadata_to_columns({"duration_min": "42.7"})["duration_min"] == 42
    # Integer-valued string still works.
    assert _promote_metadata_to_columns({"duration_min": "42"})["duration_min"] == 42
    # Native float rounds down (already worked; guard against regression).
    assert _promote_metadata_to_columns({"duration_min": 42.7})["duration_min"] == 42
    # Native int passes through.
    assert _promote_metadata_to_columns({"duration_min": 30})["duration_min"] == 30
    # Non-numeric strings are still skipped, not coerced.
    assert "duration_min" not in _promote_metadata_to_columns(
        {"duration_min": "twelve"}
    )


def test_helper_omits_missing_keys() -> None:
    """Empty / absent metadata produces an empty promotion dict so the
    INSERT path collapses to its base column list."""
    from brain.ingest import _promote_metadata_to_columns

    assert _promote_metadata_to_columns({}) == {}
    assert _promote_metadata_to_columns({"thread_id": None}) == {}


# --- index sanity ------------------------------------------------------------


def test_partial_indexes_are_partial(test_db: psycopg.Connection) -> None:
    """The three indexes added by 007 are partial — verify the WHERE clause
    is preserved in pg_indexes definitions. Otherwise the index would
    needlessly track the every-row-is-NULL majority of ``documents``."""
    rows = test_db.execute(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname='public' AND tablename='documents' "
        "AND indexname LIKE 'idx_documents_%'"
    ).fetchall()
    by_name = {r[0]: r[1] for r in rows}
    assert "WHERE (thread_id IS NOT NULL)" in by_name["idx_documents_thread_id"]
    assert "WHERE (sent_at IS NOT NULL)" in by_name["idx_documents_sent_at"]
    assert "WHERE (draft = true)" in by_name["idx_documents_draft"]


# --- migrations directory shape ----------------------------------------------


def test_migration_file_exists() -> None:
    """Sanity check: the SQL file is on disk where the runner expects it."""
    path = migrations_dir() / "007_email_thread_and_draft.sql"
    assert path.exists()
    sql = path.read_text()
    assert "thread_id" in sql
    assert "draft" in sql


# Silence unused-import warning when pytest collects but skips this file.
_ = (Path, pytest)
