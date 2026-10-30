"""`brain ingest-calendar` end to end (RM-30), through the CLI and the 5434 database.

The ``gws`` runner is the synthetic ``FakeGws`` (``tests/calendar_fixtures.py``),
swapped in with ``monkeypatch.setattr`` (a standard test double, rule 13). No
real calendar is ever read. The window passed on the command line is relative
to today, because the scope rules are; the canned events carry fixed dates,
which the fake runner returns whatever the window.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner, Result

from brain.cli import app
from brain.enrichment import SummaryResult
from brain.errors import DirectoryRefreshError
from brain.vault.derived_links.directory import DirectoryStore
from tests import calendar_fixtures as fx
from tests.conftest import CountingEmbedder
from tests.secret_fixtures import SYNTHETIC_AWS_KEY

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
    assert "2026-10-27  Weekly platform sync (series, 3 occurrence(s) in this window)" in (
        result.output
    )
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
    assert docs[fx.SERIES_ID]["metadata"]["occurrences"]["run_window_count"] == 3
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
    assert after["metadata"]["occurrences"]["run_window_count"] == 4
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


# --- an earlier window after a later one (rev-96 R1) ------------------------------


def test_an_earlier_window_does_not_move_a_series_backwards(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    """Ingest October, then backfill September: the series keeps its October dates."""
    assert _run().exit_code == 0
    before = _calendar_docs(test_db)[fx.SERIES_ID]
    embed_calls = embedder.embed_calls
    gws.events = [fx.series_instance("2026-09-08"), fx.series_instance("2026-09-15")]

    result = CliRunner().invoke(
        app, ["ingest-calendar", "--since", "2026-09-01", "--until", "2026-10-01"]
    )

    assert result.exit_code == 0, result.output
    assert f"refreshed {fx.SERIES_TITLE}" in result.output
    after = _calendar_docs(test_db)[fx.SERIES_ID]
    assert after["sent_at"] == before["sent_at"] == datetime(2026, 10, 27, 16, 0, tzinfo=UTC)
    assert after["metadata"]["date"] == "2026-10-27T09:00:00-07:00"
    assert after["metadata"]["occurrences"] == {
        "run_window_count": 2,  # the most recent run's window
        "last": [
            "2026-09-08T09:00:00-07:00",
            "2026-09-15T09:00:00-07:00",
            "2026-10-06T09:00:00-07:00",
            "2026-10-13T11:00:00-07:00",
            "2026-10-27T09:00:00-07:00",
        ],
    }
    assert after["content_hash"] == before["content_hash"]
    assert embedder.embed_calls == embed_calls


# --- a series whose master cannot be read (rev-96 R3) -----------------------------


def test_a_stored_series_keeps_its_body_when_its_master_cannot_be_read(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    assert _run().exit_code == 0
    before = _calendar_docs(test_db)[fx.SERIES_ID]
    chunks = _chunk_ids(test_db, before["id"])
    embed_calls = embedder.embed_calls
    gws.masters = {}
    gws.events.append(fx.series_instance("2026-11-03"))

    result = _run()

    assert result.exit_code == 0, result.output
    assert "1 series master(s) could not be read" in result.output
    assert "0 ingested, 0 updated, 1 occurrences refreshed, 3 unchanged, 0 failed" in (
        result.output
    )
    after = _calendar_docs(test_db)[fx.SERIES_ID]
    assert (after["content"], after["content_hash"]) == (before["content"], before["content_hash"])
    assert "RRULE:FREQ=WEEKLY" in after["content"]  # still the master's body
    assert after["metadata"]["occurrences"]["last"][-1] == "2026-11-03T09:00:00-07:00"
    assert _chunk_ids(test_db, before["id"]) == chunks
    assert embedder.embed_calls == embed_calls


def test_a_new_series_without_a_readable_master_falls_back_to_an_instance(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    gws.masters = {}

    result = _run()

    assert result.exit_code == 0, result.output
    assert "1 series master(s) could not be read" in result.output
    body = _calendar_docs(test_db)[fx.SERIES_ID]["content"]
    assert "When: recurring series, 09:00–09:30 (UTC-07:00)" in body


# --- options and failure paths (aud-96) ------------------------------------------


@dataclass
class _CountingEnricher:
    """An enricher test double: counts ``summarize`` calls, returns a canned summary."""

    model: str = "fake-model"
    calls: int = 0

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def summarize(self, title: str, content: str) -> SummaryResult:
        self.calls += 1
        return SummaryResult(summary="Canned summary.", model=self.model)


@pytest.fixture
def enricher(monkeypatch: pytest.MonkeyPatch) -> _CountingEnricher:
    fake = _CountingEnricher()
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: fake)
    monkeypatch.setenv("BRAIN_ENRICH_MIN_TOKENS", "1")  # events are short
    return fake


def _summaries(conn: psycopg.Connection[Any]) -> list[str | None]:
    rows = conn.execute(
        "SELECT d.summary FROM documents d JOIN sources s ON s.id = d.source_id "
        "WHERE s.kind = 'calendar'"
    ).fetchall()
    return [r[0] for r in rows]


def test_no_summary_is_made_without_enrich(
    test_db: psycopg.Connection[Any],
    gws: fx.FakeGws,
    embedder: CountingEmbedder,
    enricher: _CountingEnricher,
) -> None:
    assert _run().exit_code == 0

    assert enricher.calls == 0
    assert _summaries(test_db) == [None] * 4


def test_enrich_opts_in_to_a_summary_per_document(
    test_db: psycopg.Connection[Any],
    gws: fx.FakeGws,
    embedder: CountingEmbedder,
    enricher: _CountingEnricher,
) -> None:
    result = _run("--enrich")

    assert result.exit_code == 0, result.output
    assert enricher.calls == 4
    assert _summaries(test_db) == ["Canned summary."] * 4


def test_tag_applies_to_every_document(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    assert _run("--tag", "Meetings").exit_code == 0

    rows = test_db.execute(
        "SELECT d.tags FROM documents d JOIN sources s ON s.id = d.source_id "
        "WHERE s.kind = 'calendar'"
    ).fetchall()
    assert [r[0] for r in rows] == [["meetings"]] * 4


def test_a_window_with_nothing_to_ingest_writes_nothing(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    gws.events = fx.noise()

    result = _run()

    assert result.exit_code == 0, result.output
    assert "found 5 event(s): 0 document(s), 5 skipped" in result.output
    assert "ingested" not in result.output
    assert _calendar_docs(test_db) == {}
    assert embedder.embed_calls == 0


def _event_with_a_secret() -> dict[str, Any]:
    return {**fx.single(), "description": f"Deploy key {SYNTHETIC_AWS_KEY}"}


def test_a_refused_document_is_counted_failed_and_the_run_continues(
    test_db: psycopg.Connection[Any],
    gws: fx.FakeGws,
    embedder: CountingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "reject")
    gws.events[0] = _event_with_a_secret()

    result = _run()

    assert result.exit_code == 0, result.output
    assert f"refused {fx.SINGLE_TITLE}" in result.output
    assert "3 ingested, 0 updated, 0 occurrences refreshed, 0 unchanged, 1 failed" in (
        result.output
    )
    assert set(_calendar_docs(test_db)) == {fx.SERIES_ID, fx.PRIVATE_ID, fx.ALLDAY_ID}


def test_allow_secrets_ingests_a_document_the_guard_would_refuse(
    test_db: psycopg.Connection[Any],
    gws: fx.FakeGws,
    embedder: CountingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "reject")
    gws.events[0] = _event_with_a_secret()

    result = _run("--allow-secrets")

    assert result.exit_code == 0, result.output
    assert "4 ingested" in result.output
    assert fx.SINGLE_ID in _calendar_docs(test_db)


def test_a_changed_address_alone_refreshes_participants_and_the_directory(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    """Same names, new address: the body is unchanged, so only metadata moves."""
    assert _run().exit_code == 0
    before = _calendar_docs(test_db)[fx.SINGLE_ID]
    moved = ("Avery Sample", "avery.sample@example.com")
    gws.events[0] = {**fx.single(), "attendees": [fx.owner(), fx.attendee(moved)]}

    result = _run()

    assert result.exit_code == 0, result.output
    assert f"refreshed {fx.SINGLE_TITLE}" in result.output
    after = _calendar_docs(test_db)[fx.SINGLE_ID]
    assert after["content_hash"] == before["content_hash"]
    assert after["participants"] == [moved[1], fx.OWNER[1]]
    row = test_db.execute(
        "SELECT count(*) FROM directory_entries WHERE source = 'calendar' AND email = %s",
        (moved[1],),
    ).fetchone()
    assert row is not None and row[0] == 1



# --- sec-96 S1, rev-96 R5 and R6 ------------------------------------------------


def test_a_fallback_series_that_turned_private_becomes_confidential(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    """S1: an unreadable master must not bypass the escalate-only sensitivity update."""
    assert _run().exit_code == 0
    assert _calendar_docs(test_db)[fx.SERIES_ID]["sensitivity"] == "normal"
    gws.masters = {}
    gws.events = [
        {**e, "visibility": "private"} if e.get("recurringEventId") else e for e in gws.events
    ]

    result = _run()

    assert result.exit_code == 0, result.output
    assert _calendar_docs(test_db)[fx.SERIES_ID]["sensitivity"] == "confidential"


def _person_keys_of(conn: psycopg.Connection[Any], document_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT e.canonical_key FROM graph_entity_mentions m "
        "JOIN graph_entities e ON e.tenant_id = m.tenant_id AND e.id = m.entity_id "
        "WHERE m.document_id = %s AND e.entity_type = 'person'",
        (document_id,),
    ).fetchall()
    return {str(r[0]) for r in rows}


def test_an_address_only_attendee_reaches_the_people_graph_on_a_refresh(
    test_db: psycopg.Connection[Any],
    gws: fx.FakeGws,
    embedder: CountingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5: a participants-only refresh re-indexes the people aspect."""
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "true")
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")
    casey = ("Casey Sample", "casey@example.com")
    DirectoryStore(test_db).upsert_pair(display_name=casey[0], email=casey[1], source="gmail")
    assert _run().exit_code == 0
    doc_id = _calendar_docs(test_db)[fx.SINGLE_ID]["id"]
    assert "casey sample" not in _person_keys_of(test_db, doc_id)
    # An attendee with no display name: the body (names only) does not change.
    gws.events[0] = {**fx.single(), "attendees": [*fx.single()["attendees"], {"email": casey[1]}]}

    result = _run()

    assert result.exit_code == 0, result.output
    assert f"refreshed {fx.SINGLE_TITLE}" in result.output
    assert "casey sample" in _person_keys_of(test_db, doc_id)


def test_a_cancelled_future_occurrence_stops_dating_the_series(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    """R6: a start inside the run's window that the run no longer returns is dropped."""
    window = ["ingest-calendar", "--since", "2026-10-01", "--until", "2026-11-10"]
    gws.events.append(fx.series_instance("2026-11-03"))
    assert CliRunner().invoke(app, window).exit_code == 0
    assert _calendar_docs(test_db)[fx.SERIES_ID]["sent_at"] == datetime(
        2026, 11, 3, 16, 0, tzinfo=UTC
    )
    gws.events.pop()  # the 3 November occurrence was cancelled

    result = CliRunner().invoke(app, window)

    assert result.exit_code == 0, result.output
    after = _calendar_docs(test_db)[fx.SERIES_ID]
    assert after["sent_at"] == datetime(2026, 10, 27, 16, 0, tzinfo=UTC)
    assert "2026-11-03T09:00:00-07:00" not in after["metadata"]["occurrences"]["last"]



def test_a_first_run_counts_each_attendee_once_per_document(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    """aud-96 D1: a first ingest recorded every attendee twice, skewing the
    directory's name resolution, which picks by the summed count."""
    assert _run().exit_code == 0

    rows = test_db.execute(
        "SELECT email, occurrence_count FROM directory_entries WHERE source = 'calendar'"
    ).fetchall()
    counts = {str(r[0]): int(r[1]) for r in rows}
    # Avery: the single event and the series. Blake: the series and the all-day
    # event. The owner attends those three (the private event lists no one).
    assert counts == {fx.AVERY[1]: 2, fx.BLAKE[1]: 2, fx.OWNER[1]: 3}

    assert _run().exit_code == 0
    rows = test_db.execute(
        "SELECT email, occurrence_count FROM directory_entries WHERE source = 'calendar'"
    ).fetchall()
    assert {str(r[0]): int(r[1]) for r in rows} == counts  # an unchanged re-run adds nothing


def test_a_renamed_attendee_reaches_the_directory(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    """aud-96: the post-write re-record on a body change.

    Renaming an attendee changes the body (names live there) but not the
    participants (the address is the same), so only the post-write record can
    add the new name to the directory.
    """
    assert _run().exit_code == 0
    renamed = ("Blake Renamed", fx.BLAKE[1])
    master = fx.series_master(attendees=[fx.owner(), fx.attendee(fx.AVERY), fx.attendee(renamed)])
    gws.masters = {fx.SERIES_ID: master}

    result = _run()

    assert result.exit_code == 0, result.output
    assert f"updated {fx.SERIES_TITLE}" in result.output
    rows = test_db.execute(
        "SELECT display_name FROM directory_entries WHERE source = 'calendar' AND email = %s",
        (fx.BLAKE[1],),
    ).fetchall()
    assert "blake renamed" in {str(r[0]).lower() for r in rows}
    search = CliRunner().invoke(
        app, ["search", "platform sync", "--source", "calendar", "--person", renamed[0], "--json"]
    )
    assert search.exit_code == 0, search.output
    assert fx.SERIES_TITLE in [hit["title"] for hit in json.loads(search.stdout)]


# --- BL-34: a same-body event never touches another event's row ------------------------


DUPLICATE_ID = "evtsingle002"


def _duplicate_invite() -> dict[str, Any]:
    """The same meeting under a second id (a duplicated invite): an identical body."""
    return {**fx.single(), "id": DUPLICATE_ID}


def test_a_same_body_event_is_reported_and_never_patches_the_other_row(
    test_db: psycopg.Connection[Any], embedder: CountingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BL-34: the second id fell through ingest_document's content-hash fallback onto the
    first event's row, which was then patched (occurrences, date, participants) and
    reported refreshed, and the second event was never stored.
    """
    fake = fx.FakeGws([fx.single(), _duplicate_invite()])
    monkeypatch.setattr("brain.cli_ingest_calendar.calendar_gws_runner", fake)

    first = _run()
    stored = _calendar_docs(test_db)
    second = _run()

    for result in (first, second):
        assert result.exit_code == 0, result.output
        assert f"same body as another event: {DUPLICATE_ID}" in result.output
        assert result.output.rstrip().endswith(", 1 same body as another event")
    assert "1 ingested" in first.output
    assert "0 ingested, 0 updated, 0 occurrences refreshed, 1 unchanged, 0 failed" in (
        second.output
    )
    assert set(_calendar_docs(test_db)) == {fx.SINGLE_ID}
    assert _calendar_docs(test_db) == stored  # the first event's row is untouched


DUPLICATE_SERIES_ID = "evtseries002"


def _duplicate_series() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A second series whose master renders the SAME body (same title, rule, time, names,
    description) but whose volatile metadata differs from the first's: its instances fall
    on other days (occurrences, date), and one attendee shares a display name but not an
    address (participants). A wrongful patch would therefore be visible.
    """
    other_avery = (fx.AVERY[0], "avery.other@example.com")
    attendees = [fx.owner(), fx.attendee(other_avery), fx.attendee(fx.BLAKE)]
    master = fx.series_master(id=DUPLICATE_SERIES_ID, attendees=attendees)
    instances = [
        {
            **fx.series_instance(day),
            "id": f"{DUPLICATE_SERIES_ID}_{day.replace('-', '')}",
            "recurringEventId": DUPLICATE_SERIES_ID,
            "attendees": attendees,
        }
        for day in ("2026-09-08", "2026-09-22")
    ]
    return master, instances


def test_a_same_body_series_never_overwrites_the_other_series_volatile_metadata(
    test_db: psycopg.Connection[Any], embedder: CountingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BL-34 (aud-106): the second series carries different occurrences, date and
    participants. On master its refresh landed on the first series' row and replaced all
    three; now that row keeps its own values on every run.
    """
    dup_master, dup_instances = _duplicate_series()
    fake = fx.FakeGws(
        [*fx.series_instances(), *dup_instances],
        masters={fx.SERIES_ID: fx.series_master(), DUPLICATE_SERIES_ID: dup_master},
    )
    monkeypatch.setattr("brain.cli_ingest_calendar.calendar_gws_runner", fake)

    first = _run()
    own = _calendar_docs(test_db)[fx.SERIES_ID]
    second = _run()

    for result in (first, second):
        assert result.exit_code == 0, result.output
        assert f"same body as another event: {DUPLICATE_SERIES_ID}" in result.output
    docs = _calendar_docs(test_db)
    assert set(docs) == {fx.SERIES_ID}
    meta = docs[fx.SERIES_ID]["metadata"]
    assert meta == own["metadata"]
    assert meta["date"].startswith("2026-10-27")  # the first series' own last occurrence
    assert not any(start.startswith("2026-09") for start in meta["occurrences"]["last"])
    assert "avery.other@example.com" not in meta["participants"]
    assert fx.AVERY[1] in meta["participants"]


def test_a_run_without_rare_outcomes_prints_the_summary_unchanged(
    test_db: psycopg.Connection[Any], gws: fx.FakeGws, embedder: CountingEmbedder
) -> None:
    """BL-34 D1: the new counts appear only when non-zero."""
    result = _run()

    [summary] = [line for line in result.output.splitlines() if "occurrences refreshed" in line]
    assert summary.endswith(" failed")
    assert "same body" not in result.output and "empty" not in summary


def test_no_document_from_ingest_is_empty_not_an_assertion(
    test_db: psycopg.Connection[Any], embedder: CountingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BL-34: ``document_id=None`` (nothing to chunk) used to hit an assert."""
    from brain.ingest import IngestResult

    fake = fx.FakeGws([fx.single()])
    monkeypatch.setattr("brain.cli_ingest_calendar.calendar_gws_runner", fake)
    monkeypatch.setattr(
        "brain.cli_ingest_calendar.ingest_document",
        lambda *_a, **_k: IngestResult(document_id=None, created=False),
    )

    result = _run()

    assert result.exit_code == 0, result.output
    assert f"  empty {fx.SINGLE_ID}" in result.output
    assert result.output.rstrip().endswith("0 failed, 1 empty")
