"""Tests for `brain ingest-stdin` — the generic Claude-orchestrated ingester.

The B.3 hook lands here too: Krisp ingests must (a) materialize
``_participant_keys`` into ``documents.metadata`` from the transcript body,
(b) trigger a Calendar refresh window (YTD on first run, incremental
afterward), and (c) trigger a Contacts refresh only when stale (>24h old).
All directory side effects degrade soft — refresh failures must not fail
the ingest itself.
"""
import datetime
import json
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document
from tests.conftest import FakeRunner

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _patch_embedder(monkeypatch: pytest.MonkeyPatch, fake_embedder: object) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)


# ---------------------------------------------------------------------------
# B.3 — Krisp ingest hook fixtures + helpers.
# ---------------------------------------------------------------------------


KRISP_BODY_TWO_SPEAKERS = (
    "**Ali | 0:01**\nHey, thanks for joining.\n\n"
    "**bob@example.com | 0:02**\nHi there.\n"
)
"""A canonical Krisp transcript body with one name + one email speaker."""


def _krisp_doc(*, body: str = KRISP_BODY_TWO_SPEAKERS) -> ExtractedDoc:
    """Build an ``ExtractedDoc`` shaped like a Krisp stdin ingest."""
    return ExtractedDoc(
        title="person-x sync",
        content=body,
        content_type="transcript",
        source_path=None,
        metadata={
            "date": "2026-04-29",
            "krisp_meeting_id": "meeting-42",
        },
    )


def _refresh_state(
    conn: psycopg.Connection, source: str
) -> tuple[Any, ...] | None:
    """Fetch (last_refreshed_at, records_seen) for the given source, or None."""
    return conn.execute(
        "SELECT last_refreshed_at, records_seen "
        "FROM directory_refresh_state WHERE source = %s",
        (source,),
    ).fetchone()


def test_ingest_stdin_creates_document(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    # Sandbox vault so mirror writes don't touch ~/brain-vault.
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", "krisp",
            "--external-id", "meeting-42",
            "--title", "person-x sync",
            "--content-type", "transcript",
            "--metadata",
            json.dumps({"participants": ["person-x", "Ali"], "duration_min": 28}),
        ],
        input="Hello person-x. Let me catch you up on COMPANY_REDACTED.\n\nIt was great.\n",
    )
    assert result.exit_code == 0, result.output
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT d.title, d.content_type, s.kind, s.external_id "
            "FROM documents d JOIN sources s ON s.id=d.source_id "
            "WHERE s.external_id='meeting-42'"
        ).fetchone()
    assert row is not None
    assert row[0] == "person-x sync"
    assert row[1] == "transcript"
    assert row[2] == "krisp"
    assert row[3] == "meeting-42"


def test_ingest_stdin_dedups_on_external_id(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    # Sandbox vault so mirror writes don't touch ~/brain-vault.
    # Without this, "ts-1" + title "Thread" leaked _ingested/slack/YYYY-MM-DD-ts1-thread.md
    # into the prod vault on 2026-05-08/09.
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    args = [
        "ingest-stdin",
        "--source", "slack",
        "--external-id", "ts-1",
        "--title", "Thread",
        "--content-type", "transcript",
    ]
    CliRunner().invoke(app, args, input="same content")
    CliRunner().invoke(app, args, input="same content")
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        n_row = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert n_row is not None
    assert n_row[0] == 1


def test_ingest_stdin_empty_input_fails(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Empty stdin content is a user error — exit 1 with a red message."""
    # Sandbox vault even though empty input exits before any write.
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", "krisp",
            "--external-id", "mt-empty",
            "--title", "Empty",
            "--content-type", "transcript",
        ],
        input="   \n\n   ",
    )
    assert result.exit_code == 1
    assert "stdin was empty" in result.output


def test_ingest_stdin_date_flag_populates_metadata(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """--date is merged into source+document metadata under the 'date' key."""
    # Sandbox vault so mirror writes don't touch ~/brain-vault.
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", "krisp",
            "--external-id", "mt-dated",
            "--title", "Dated sync",
            "--content-type", "transcript",
            "--date", "2026-04-24",
        ],
        input="some call content",
    )
    assert result.exit_code == 0, result.output
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT d.metadata, s.metadata FROM documents d "
            "JOIN sources s ON s.id=d.source_id "
            "WHERE s.external_id='mt-dated'"
        ).fetchone()
    assert row is not None
    assert row[0]["date"] == "2026-04-24"
    assert row[1]["date"] == "2026-04-24"


def test_ingest_stdin_creates_vault_mirror(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`brain ingest-stdin` writes a mirror under ``<vault>/_ingested/<source>/``.

    Setup: sandbox ``BRAIN_VAULT_PATH`` to ``tmp_path``.
    Exercise: invoke ingest-stdin with a slack snippet on stdin.
    Verify: a single Markdown file lands under ``_ingested/slack/`` whose
    body contains the stdin content.
    """
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    body = "Mirror this slack thread into the vault.\n"
    result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", "slack",
            "--external-id", "ts-mirror",
            "--title", "Slack mirror test",
            "--content-type", "transcript",
        ],
        input=body,
    )

    assert result.exit_code == 0, result.output
    mirror_dir = tmp_path / "_ingested" / "slack"
    assert mirror_dir.is_dir(), f"missing mirror dir: {mirror_dir}"
    mirrors = list(mirror_dir.glob("*.md"))
    assert len(mirrors) == 1, f"expected one mirror file, got {mirrors}"
    assert "Mirror this slack thread" in mirrors[0].read_text(encoding="utf-8")


def test_ingest_stdin_force_reingests(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """With --force, a repeat ingest of the same content UPDATEs in place."""
    # Sandbox vault so mirror writes don't touch ~/brain-vault.
    # Without this, "ts-force" + title "Thread" leaked _ingested/slack/YYYY-MM-DD-ts-force-thread.md
    # into the prod vault on 2026-05-08/09.
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    args = [
        "ingest-stdin",
        "--source", "slack",
        "--external-id", "ts-force",
        "--title", "Thread",
        "--content-type", "transcript",
    ]
    first = CliRunner().invoke(app, args, input="same content")
    assert first.exit_code == 0, first.output
    second = CliRunner().invoke(app, [*args, "--force"], input="same content")
    assert second.exit_code == 0, second.output
    # Post-fix: --force on an existing sourced doc returns "updated" (in-place UPDATE,
    # UUID preserved), not "ingested" (new UUID via DELETE+INSERT).
    assert "updated" in second.output.lower()
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        n_row = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert n_row is not None
    assert n_row[0] == 1


# ---------------------------------------------------------------------------
# B.3 — Krisp ingest hook tests.
#
# The hook is exercised via ``ingest_document`` directly (so we can pass a
# FakeRunner) plus one CLI smoke test for the runner-injection wiring.
# ---------------------------------------------------------------------------


def test_krisp_ingest_materializes_participant_keys(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Krisp ingest stores sorted participant keys in ``documents.metadata``."""
    runner = FakeRunner()
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_krisp_doc(),
        source_kind="krisp",
        source_external_id="meeting-42",
        gws_runner=runner,
    )
    assert result.created is True

    row = test_db.execute(
        "SELECT metadata FROM documents WHERE id = %s",
        (result.document_id,),
    ).fetchone()
    assert row is not None
    metadata = row[0]
    # Sorted alphabetically for deterministic JSONB ordering.
    assert metadata["_participant_keys"] == ["ali", "bob@example.com"]


def test_krisp_ingest_with_no_participants_stores_empty_list(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """A Krisp body with no speaker labels still gets the key — set to ``[]``.

    Empty list is the "this doc was processed" signal for the linker pass.
    """
    runner = FakeRunner()
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_krisp_doc(body="No speaker labels here, just plain narrative."),
        source_kind="krisp",
        source_external_id="meeting-nospk",
        gws_runner=runner,
    )
    assert result.created is True

    row = test_db.execute(
        "SELECT metadata FROM documents WHERE id = %s",
        (result.document_id,),
    ).fetchone()
    assert row is not None
    assert row[0]["_participant_keys"] == []


def test_krisp_ingest_triggers_calendar_refresh(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Empty calendar response is still a successful refresh — state row advances."""
    runner = FakeRunner(response="[]")
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_krisp_doc(),
        source_kind="krisp",
        source_external_id="meeting-cal",
        gws_runner=runner,
    )
    state = _refresh_state(test_db, "calendar")
    assert state is not None
    # Calendar runner was invoked exactly once with the calendar subcommand.
    cal_calls = [
        c for c in runner.calls if len(c) >= 2 and c[1] == "calendar"
    ]
    assert len(cal_calls) == 1


def test_krisp_ingest_first_run_uses_ytd_window(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """No prior calendar state → since=Jan 1 of the current year (UTC)."""
    runner = FakeRunner()
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_krisp_doc(),
        source_kind="krisp",
        source_external_id="meeting-ytd",
        gws_runner=runner,
    )
    # Real ``gws`` takes Calendar API params as a single ``--params`` JSON
    # blob; pull ``timeMin`` out of that to assert the YTD window.
    cal_call = next(c for c in runner.calls if c[1] == "calendar")
    params = json.loads(cal_call[cal_call.index("--params") + 1])
    parsed = datetime.datetime.fromisoformat(params["timeMin"])
    today = datetime.datetime.now(tz=datetime.UTC)
    assert parsed.year == today.year
    assert parsed.month == 1
    assert parsed.day == 1
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_krisp_ingest_subsequent_run_uses_incremental_window(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """A prior ``directory_refresh_state.calendar`` row anchors ``since``."""
    # Pre-populate the state row with a known timestamp.
    anchor = datetime.datetime(2026, 4, 1, 12, 0, 0, tzinfo=datetime.UTC)
    test_db.execute(
        "INSERT INTO directory_refresh_state (source, last_refreshed_at, records_seen) "
        "VALUES ('calendar', %s, 0)",
        (anchor,),
    )

    runner = FakeRunner()
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_krisp_doc(),
        source_kind="krisp",
        source_external_id="meeting-incr",
        gws_runner=runner,
    )
    # Real ``gws`` takes Calendar API params as a single ``--params`` JSON
    # blob; pull ``timeMin`` out to verify the incremental window anchor.
    cal_call = next(c for c in runner.calls if c[1] == "calendar")
    params = json.loads(cal_call[cal_call.index("--params") + 1])
    parsed = datetime.datetime.fromisoformat(params["timeMin"])
    # Allow microsecond drift across psycopg's tz coercion; equality on
    # the wall-clock components is what matters here.
    assert parsed == anchor


def test_krisp_ingest_triggers_contacts_refresh_when_stale(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Contacts state >24h old → refresh fires."""
    stale = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=48)
    test_db.execute(
        "INSERT INTO directory_refresh_state (source, last_refreshed_at, records_seen) "
        "VALUES ('contacts', %s, 0)",
        (stale,),
    )

    runner = FakeRunner()
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_krisp_doc(),
        source_kind="krisp",
        source_external_id="meeting-cstale",
        gws_runner=runner,
    )
    people_calls = [c for c in runner.calls if len(c) >= 2 and c[1] == "people"]
    assert len(people_calls) == 1


def test_krisp_ingest_skips_contacts_refresh_when_fresh(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Contacts state <24h old → refresh skipped (rate-limit honored)."""
    fresh = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=2)
    test_db.execute(
        "INSERT INTO directory_refresh_state (source, last_refreshed_at, records_seen) "
        "VALUES ('contacts', %s, 0)",
        (fresh,),
    )

    runner = FakeRunner()
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_krisp_doc(),
        source_kind="krisp",
        source_external_id="meeting-cfresh",
        gws_runner=runner,
    )
    people_calls = [c for c in runner.calls if len(c) >= 2 and c[1] == "people"]
    assert people_calls == []


def test_krisp_ingest_succeeds_when_runner_is_none(
    test_db: psycopg.Connection,
    fake_embedder: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No runner → ingest still inserts the doc; refreshes skipped with a warning."""
    import logging

    with caplog.at_level(logging.WARNING, logger="brain.ingest"):
        result = ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=_krisp_doc(),
            source_kind="krisp",
            source_external_id="meeting-norunner",
            gws_runner=None,
        )
    assert result.created is True
    # Document landed; metadata still has _participant_keys (pre-insert step
    # doesn't depend on the runner).
    row = test_db.execute(
        "SELECT metadata FROM documents WHERE id = %s",
        (result.document_id,),
    ).fetchone()
    assert row is not None
    assert row[0]["_participant_keys"] == ["ali", "bob@example.com"]
    # No directory state row was created (refreshes were short-circuited).
    assert _refresh_state(test_db, "calendar") is None
    assert _refresh_state(test_db, "contacts") is None
    assert any(
        "no gws_runner" in r.message for r in caplog.records
    )


def test_krisp_ingest_succeeds_when_calendar_refresh_fails(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """A FakeRunner that raises must NOT fail the ingest — refresh is soft."""
    runner = FakeRunner(raises=RuntimeError("simulated gws failure"))
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_krisp_doc(),
        source_kind="krisp",
        source_external_id="meeting-runfail",
        gws_runner=runner,
    )
    assert result.created is True
    # Document + chunks landed atomically; participant_keys still set.
    doc_row = test_db.execute(
        "SELECT metadata FROM documents WHERE id = %s",
        (result.document_id,),
    ).fetchone()
    assert doc_row is not None
    assert doc_row[0]["_participant_keys"] == ["ali", "bob@example.com"]

    chunk_count = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id = %s",
        (result.document_id,),
    ).fetchone()
    assert chunk_count is not None and chunk_count[0] >= 1


def test_gmail_hook_unchanged_after_b3_refactor(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Regression: B.2 Gmail hook still upserts ``directory_entries`` after the refactor.

    Confirms ``_run_source_hooks`` dispatches Gmail ingests to the same
    upsert behavior that ``test_cli_ingest_gmail.py`` exercised pre-B.3.
    """
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Hi",
            content="Hello world",
            content_type="email",
            source_path=None,
            metadata={
                "from": "Ali Sarkis <redacted@example.com>",
                "to": "person-x last-a <person-a@example.com>",
                "date": "2026-04-01",
                "message_id": "m1",
                "thread_id": "tm1",
                "label_ids": ["INBOX"],
            },
        ),
        source_kind="gmail",
        source_external_id="m1",
    )
    assert result.created is True
    rows = test_db.execute(
        "SELECT display_name, email, source FROM directory_entries "
        "ORDER BY email"
    ).fetchall()
    assert rows == [
        ("person-a last-a", "person-a@example.com", "gmail"),
        ("ali sarkis", "redacted@example.com", "gmail"),
    ]


def test_cli_krisp_ingest_threads_real_runner(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Smoke test: ``brain ingest-stdin --source krisp`` constructs ``real_gws_runner``.

    We don't want the CLI to actually shell out to ``gws`` in tests, so we
    monkeypatch ``shutil.which`` to return ``None`` — the real runner then
    raises ``DirectoryRefreshError`` on first call, which ``refresh_calendar``
    catches and downgrades to a warning. The ingest still succeeds and the
    document lands in the DB; ``_participant_keys`` is materialized.
    """
    # Sandbox vault so mirror writes don't touch ~/brain-vault.
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    # Force the real runner's PATH check to fail → DirectoryRefreshError
    # which the refresh helpers catch → soft warning, ingest continues.
    monkeypatch.setattr(
        "brain.vault.derived_links.gws.shutil.which", lambda _: None
    )

    result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", "krisp",
            "--external-id", "cli-meeting-1",
            "--title", "CLI Krisp",
            "--content-type", "transcript",
        ],
        input=KRISP_BODY_TWO_SPEAKERS,
    )
    assert result.exit_code == 0, result.output

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT d.metadata FROM documents d JOIN sources s ON s.id=d.source_id "
            "WHERE s.external_id='cli-meeting-1'"
        ).fetchone()
    assert row is not None
    assert row[0]["_participant_keys"] == ["ali", "bob@example.com"]
