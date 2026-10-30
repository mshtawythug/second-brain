"""`brain ingest-calendar` end to end (RM-30), through the CLI and the 5434 database.

The ``gws`` runner is the synthetic ``FakeGws`` (``tests/calendar_fixtures.py``),
swapped in with ``monkeypatch.setattr`` (a standard test double, rule 13). No
real calendar is ever read. The window passed on the command line is relative
to today, because the scope rules are; the canned events carry fixed dates,
which the fake runner returns whatever the window.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner, Result

from brain.cli import app
from brain.errors import DirectoryRefreshError
from tests import calendar_fixtures as fx
from tests.conftest import CountingEmbedder

_TODAY = datetime.now(UTC).date()
_WINDOW = ["--since", str(_TODAY - timedelta(days=60)), "--until", str(_TODAY + timedelta(days=1))]


def _all_events() -> list[dict[str, Any]]:
    return [fx.single(), *fx.series_instances(), fx.private(), fx.all_day(), fx.declined()]


@pytest.fixture
def gws(monkeypatch: pytest.MonkeyPatch) -> fx.FakeGws:
    fake = fx.FakeGws(_all_events(), masters={fx.SERIES_ID: fx.series_master()})
    monkeypatch.setattr("brain.cli_ingest_calendar.calendar_gws_runner", fake)
    return fake


@pytest.fixture
def embedder(
    counting_embedder: CountingEmbedder, patch_embedder: Any
) -> CountingEmbedder:
    patch_embedder(counting_embedder)
    return counting_embedder


def _run(*extra: str) -> Result:
    return CliRunner().invoke(app, ["ingest-calendar", *_WINDOW, *extra])


def _calendar_docs(conn: psycopg.Connection[Any]) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.external_id, d.id::text, d.title, d.content, d.content_type,
               d.sensitivity, d.sent_at, d.participants, d.metadata, d.content_hash
        FROM documents d JOIN sources s ON s.id = d.source_id
        WHERE s.kind = 'calendar'
        """
    ).fetchall()
    keys = ("id", "title", "content", "content_type", "sensitivity", "sent_at",
            "participants", "metadata", "content_hash")
    return {str(r[0]): dict(zip(keys, r[1:], strict=True)) for r in rows}


def _chunk_ids(conn: psycopg.Connection[Any], document_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT id::text FROM chunks WHERE document_id = %s ORDER BY id", (document_id,)
    ).fetchall()
    return [str(r[0]) for r in rows]


# --- scope ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--since", "2026-10-01"],
        ["--until", "2026-10-01"],
    ],
)
def test_missing_scope_is_refused(args: list[str], gws: fx.FakeGws) -> None:
    result = CliRunner().invoke(app, ["ingest-calendar", *args])

    assert result.exit_code == 2
    assert (
        "ingest-calendar requires --since and --until "
        "(at most 92 days, ending at most 14 days ahead)"
    ) in result.output
    assert gws.calls == []


def test_a_window_over_92_days_is_refused(gws: fx.FakeGws) -> None:
    since = _TODAY - timedelta(days=93)

    result = CliRunner().invoke(
        app, ["ingest-calendar", "--since", str(since), "--until", str(_TODAY)]
    )

    assert result.exit_code == 2
    assert "the window is 93 days" in result.output
    assert gws.calls == []


def test_a_92_day_window_is_accepted(gws: fx.FakeGws) -> None:
    since = _TODAY - timedelta(days=92)

    result = CliRunner().invoke(
        app, ["ingest-calendar", "--since", str(since), "--until", str(_TODAY), "--dry-run"]
    )

    assert result.exit_code == 0, result.output


def test_a_window_ending_more_than_14_days_ahead_is_refused(gws: fx.FakeGws) -> None:
    until = _TODAY + timedelta(days=15)

    result = CliRunner().invoke(
        app, ["ingest-calendar", "--since", str(_TODAY), "--until", str(until)]
    )

    assert result.exit_code == 2
    assert "more than 14 days ahead" in result.output
    assert gws.calls == []


@pytest.mark.parametrize(
    ("since", "until", "message"),
    [
        ("2026/10/01", "2026-10-02", "dates must be YYYY-MM-DD"),
        ("2026-10-02", "2026-10-02", "--until must be after --since"),
    ],
)
def test_malformed_or_empty_windows_are_refused(
    since: str, until: str, message: str, gws: fx.FakeGws
) -> None:
    result = CliRunner().invoke(app, ["ingest-calendar", "--since", since, "--until", until])

    assert result.exit_code == 2
    assert message in result.output


# --- dry run and gws failure -----------------------------------------------------


def test_dry_run_lists_titles_and_dates_and_writes_nothing(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    result = _run("--dry-run")

    assert result.exit_code == 0, result.output
    assert "2026-10-06  Vendor intro with Acme Widgets" in result.output
    assert "2026-10-27  Weekly platform sync (series, 3 occurrence(s))" in result.output
    assert "Optional lunch talk" not in result.output  # declined
    assert _calendar_docs(test_db) == {}
    assert embedder.embed_calls == 0


def test_a_gws_failure_exits_1_and_prints_the_class_not_the_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(args: list[str]) -> str:
        raise DirectoryRefreshError(
            "gws command failed (exit 2): token=SECRETTOKEN for owner@example.com",
            log_detail="exit 2",
        )

    monkeypatch.setattr("brain.cli_ingest_calendar.calendar_gws_runner", failing)

    result = _run()

    assert result.exit_code == 1
    assert "gws calendar events list failed (DirectoryRefreshError[exit 2])" in result.output
    assert "SECRETTOKEN" not in result.output
    assert "owner@example.com" not in result.output


class _EmbedderThatLeaks:
    """Raises a ``ValueError`` whose message stands in for quoted row content."""

    dim = 1024

    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        raise ValueError("row value SECRETROW")

    def count_tokens(self, text: str) -> int:
        return len(text.split())


def test_a_per_document_failure_prints_the_class_not_the_message(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, patch_embedder: Any
) -> None:
    patch_embedder(_EmbedderThatLeaks())

    result = _run()

    assert result.exit_code == 0, result.output
    assert f"failed {fx.SINGLE_TITLE}: ValueError" in result.output
    assert "SECRETROW" not in result.output
    assert "0 ingested, 0 updated, 0 occurrences refreshed, 0 unchanged, 4 failed" in (
        result.output
    )


# --- end to end ------------------------------------------------------------------


def test_each_fixture_class_lands_as_specified(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    """Criterion 1: single, series, declined skipped, private confidential, all-day."""
    result = _run()

    assert result.exit_code == 0, result.output
    assert "4 ingested, 0 updated, 0 occurrences refreshed, 0 unchanged, 0 failed" in (
        result.output
    )
    docs = _calendar_docs(test_db)
    assert set(docs) == {fx.SINGLE_ID, fx.SERIES_ID, fx.PRIVATE_ID, fx.ALLDAY_ID}
    assert {d["content_type"] for d in docs.values()} == {"calendar_event"}
    assert docs[fx.PRIVATE_ID]["sensitivity"] == "confidential"
    assert {docs[k]["sensitivity"] for k in (fx.SINGLE_ID, fx.SERIES_ID, fx.ALLDAY_ID)} == {
        "normal"
    }
    assert docs[fx.SINGLE_ID]["sent_at"] == datetime(2026, 10, 6, 17, 0, tzinfo=UTC)
    assert docs[fx.SERIES_ID]["sent_at"] == datetime(2026, 10, 27, 16, 0, tzinfo=UTC)
    assert docs[fx.ALLDAY_ID]["sent_at"] == datetime(2026, 10, 14, tzinfo=UTC)
    assert docs[fx.SERIES_ID]["metadata"]["occurrences"]["count"] == 3
    assert docs[fx.SINGLE_ID]["participants"] == ["avery@example.com", "owner@example.com"]


def test_no_attendee_address_appears_in_any_body(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    """Criterion 5: in the body, the chunks, or the title."""
    assert _run().exit_code == 0
    addresses = [fx.OWNER[1], fx.AVERY[1], fx.BLAKE[1], fx.ROOM[1]]

    for doc in _calendar_docs(test_db).values():
        for address in addresses:
            assert address not in doc["content"]
            assert address not in doc["title"]
    chunk_text = " ".join(
        str(r[0]) for r in test_db.execute("SELECT content FROM chunks").fetchall()
    )
    assert not any(address in chunk_text for address in addresses)


def test_a_re_run_is_a_no_op(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    """Criterion 2, first half."""
    assert _run().exit_code == 0
    before = _calendar_docs(test_db)
    chunks = {k: _chunk_ids(test_db, d["id"]) for k, d in before.items()}
    embed_calls = embedder.embed_calls

    result = _run()

    assert result.exit_code == 0, result.output
    assert "0 ingested, 0 updated, 0 occurrences refreshed, 4 unchanged, 0 failed" in (
        result.output
    )
    assert _calendar_docs(test_db) == before
    assert {k: _chunk_ids(test_db, d["id"]) for k, d in before.items()} == chunks
    assert embedder.embed_calls == embed_calls


def test_a_changed_description_updates_in_place(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    """Criterion 2: same document id, new body."""
    assert _run().exit_code == 0
    before = _calendar_docs(test_db)[fx.SINGLE_ID]
    gws.events[0] = {**fx.single(), "description": "Agenda: pricing only."}

    result = _run()

    assert result.exit_code == 0, result.output
    assert f"updated {fx.SINGLE_TITLE}" in result.output
    after = _calendar_docs(test_db)[fx.SINGLE_ID]
    assert after["id"] == before["id"]
    assert "Agenda: pricing only." in after["content"]
    assert after["content_hash"] != before["content_hash"]


def test_new_occurrences_change_only_the_metadata_without_re_chunking(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    """Criterion 2, and the test plan's "a metadata-only re-run does not call the embedder"."""
    assert _run().exit_code == 0
    before = _calendar_docs(test_db)[fx.SERIES_ID]
    chunks = _chunk_ids(test_db, before["id"])
    embed_calls = embedder.embed_calls
    gws.events.append(fx.series_instance("2026-11-03"))

    result = _run()

    assert result.exit_code == 0, result.output
    assert "0 ingested, 0 updated, 1 occurrences refreshed, 3 unchanged, 0 failed" in (
        result.output
    )
    after = _calendar_docs(test_db)[fx.SERIES_ID]
    assert (after["id"], after["content"], after["content_hash"]) == (
        before["id"],
        before["content"],
        before["content_hash"],
    )
    assert after["metadata"]["occurrences"]["count"] == 4
    assert after["metadata"]["occurrences"]["last"][-1] == "2026-11-03T09:00:00-07:00"
    assert after["sent_at"] == datetime(2026, 11, 3, 16, 0, tzinfo=UTC)
    assert _chunk_ids(test_db, before["id"]) == chunks
    assert embedder.embed_calls == embed_calls


def test_an_event_turning_private_becomes_confidential(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    assert _run().exit_code == 0
    gws.events[0] = {**fx.single(), "visibility": "private"}

    assert _run().exit_code == 0

    assert _calendar_docs(test_db)[fx.SINGLE_ID]["sensitivity"] == "confidential"


def test_search_by_source_and_person_finds_the_event(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    """Criterion 3: ``brain search --source calendar --person <synthetic>``."""
    assert _run().exit_code == 0

    result = CliRunner().invoke(
        app,
        ["search", "vendor intro", "--source", "calendar", "--person", fx.AVERY[0], "--json"],
    )

    assert result.exit_code == 0, result.output
    titles = [hit["title"] for hit in json.loads(result.stdout)]
    assert fx.SINGLE_TITLE in titles


def test_the_attendee_pairs_reach_the_people_directory(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    assert _run().exit_code == 0

    rows = test_db.execute(
        "SELECT display_name, email FROM directory_entries WHERE source = 'calendar'"
    ).fetchall()

    assert {(str(r[0]).lower(), str(r[1])) for r in rows} >= {
        ("avery sample", fx.AVERY[1]),
        ("blake sample", fx.BLAKE[1]),
    }
