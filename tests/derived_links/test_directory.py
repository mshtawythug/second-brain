"""Tests for brain.vault.derived_links.directory.

Covers DirectoryStore (real Postgres), load_people_yml (filesystem), and
refresh_calendar / refresh_contacts (fake GwsRunner — no subprocess).
"""
import datetime
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pytest_mock import MockerFixture

from brain.vault.derived_links.directory import (
    DirectoryStore,
    _score_directory_rows,
    _update_refresh_state,
    load_people_yml,
    refresh_calendar,
    refresh_contacts,
    refresh_people_yml,
)
from tests.conftest import FakeRunner


@pytest.fixture
def store(test_db: psycopg.Connection) -> Iterator[DirectoryStore]:
    """A DirectoryStore bound to the per-test connection."""
    yield DirectoryStore(test_db)


def _row_count(conn: psycopg.Connection, where_sql: str = "TRUE") -> int:
    """Count directory_entries rows matching ``where_sql`` (for assertions)."""
    row = conn.execute(
        f"SELECT count(*) FROM directory_entries WHERE {where_sql}"
    ).fetchone()
    assert row is not None
    return int(row[0])


class TestUpsertPair:
    """First-write inserts; conflicting writes bump occurrence_count."""

    def test_first_insert_creates_row_with_count_1(
        self, store: DirectoryStore, test_db: psycopg.Connection
    ) -> None:
        store.upsert_pair(
            display_name="Bob Smith", email="bob@example.com", source="gmail"
        )
        rows = test_db.execute(
            "SELECT display_name, email, source, occurrence_count "
            "FROM directory_entries"
        ).fetchall()
        assert rows == [("bob smith", "bob@example.com", "gmail", 1)]

    def test_repeat_upsert_increments_count(
        self, store: DirectoryStore, test_db: psycopg.Connection
    ) -> None:
        store.upsert_pair(
            display_name="Bob", email="bob@example.com", source="gmail"
        )
        store.upsert_pair(
            display_name="Bob", email="bob@example.com", source="gmail"
        )
        store.upsert_pair(
            display_name="Bob", email="bob@example.com", source="gmail"
        )
        row = test_db.execute(
            "SELECT occurrence_count, first_seen_at < last_seen_at "
            "OR first_seen_at = last_seen_at FROM directory_entries"
        ).fetchone()
        assert row is not None
        assert row[0] == 3
        # last_seen_at should be >= first_seen_at — note: rapid in-test
        # writes can land in the same NOW() instant, so we accept ``>=``.
        assert row[1] is True

    def test_different_display_name_creates_separate_row(
        self, store: DirectoryStore, test_db: psycopg.Connection
    ) -> None:
        store.upsert_pair(
            display_name="Robert", email="bob@example.com", source="gmail"
        )
        store.upsert_pair(
            display_name="Bob", email="bob@example.com", source="gmail"
        )
        assert _row_count(test_db) == 2

    def test_different_source_creates_separate_row(
        self, store: DirectoryStore, test_db: psycopg.Connection
    ) -> None:
        store.upsert_pair(
            display_name="Bob", email="bob@example.com", source="gmail"
        )
        store.upsert_pair(
            display_name="Bob", email="bob@example.com", source="calendar"
        )
        assert _row_count(test_db) == 2

    def test_none_display_name_stored_as_empty_string(
        self, store: DirectoryStore, test_db: psycopg.Connection
    ) -> None:
        store.upsert_pair(
            display_name=None, email="bob@example.com", source="gmail"
        )
        row = test_db.execute(
            "SELECT display_name FROM directory_entries"
        ).fetchone()
        assert row is not None
        assert row[0] == ""

    def test_empty_display_name_stored_as_empty_string(
        self, store: DirectoryStore, test_db: psycopg.Connection
    ) -> None:
        store.upsert_pair(
            display_name="   ", email="bob@example.com", source="gmail"
        )
        row = test_db.execute(
            "SELECT display_name FROM directory_entries"
        ).fetchone()
        assert row is not None
        assert row[0] == ""

    def test_empty_email_raises(self, store: DirectoryStore) -> None:
        with pytest.raises(ValueError, match="email cannot be empty"):
            store.upsert_pair(
                display_name="Bob", email="   ", source="gmail"
            )

    def test_invalid_source_raises(self, store: DirectoryStore) -> None:
        with pytest.raises(ValueError, match="invalid source"):
            store.upsert_pair(
                display_name="Bob", email="bob@example.com", source="slack"
            )

    def test_display_name_normalized(
        self, store: DirectoryStore, test_db: psycopg.Connection
    ) -> None:
        # "Pat Morgan." (trailing punctuation, mixed case) → "pat morgan"
        store.upsert_pair(
            display_name="Pat Morgan.",
            email="pat@example.com",
            source="gmail",
        )
        row = test_db.execute(
            "SELECT display_name FROM directory_entries"
        ).fetchone()
        assert row is not None
        assert row[0] == "pat morgan"

    def test_email_lowercased_and_stripped(
        self, store: DirectoryStore, test_db: psycopg.Connection
    ) -> None:
        store.upsert_pair(
            display_name="Bob",
            email="  Bob@Example.COM  ",
            source="gmail",
        )
        row = test_db.execute(
            "SELECT email FROM directory_entries"
        ).fetchone()
        assert row is not None
        assert row[0] == "bob@example.com"


class TestResolveNameToEmail:
    """Resolution semantics — single-match, ambiguity, people_yml priority."""

    def test_single_match_returns_email(self, store: DirectoryStore) -> None:
        store.upsert_pair(
            display_name="Bob", email="bob@example.com", source="gmail"
        )
        assert store.resolve_name_to_email("Bob") == "bob@example.com"

    def test_zero_matches_returns_none(self, store: DirectoryStore) -> None:
        assert store.resolve_name_to_email("Ghost") is None

    @pytest.mark.parametrize("name", ["", "  ", "\t", "Speaker_3"])
    def test_unnormalizable_name_returns_none(
        self, store: DirectoryStore, name: str
    ) -> None:
        assert store.resolve_name_to_email(name) is None

    def test_tied_counts_are_ambiguous(self, store: DirectoryStore) -> None:
        store.upsert_pair(
            display_name="Alex", email="a1@example.com", source="gmail"
        )
        store.upsert_pair(
            display_name="Alex", email="a2@example.com", source="gmail"
        )
        assert store.resolve_name_to_email("Alex") is None

    def test_dominant_email_wins(self, store: DirectoryStore) -> None:
        store.upsert_pair(
            display_name="Alex", email="a1@example.com", source="gmail"
        )
        store.upsert_pair(
            display_name="Alex", email="a1@example.com", source="gmail"
        )
        store.upsert_pair(
            display_name="Alex", email="a2@example.com", source="gmail"
        )
        assert store.resolve_name_to_email("Alex") == "a1@example.com"

    def test_people_yml_wins_over_higher_counts(
        self, store: DirectoryStore
    ) -> None:
        # gmail has 5 hits for the wrong email; people_yml has 1 — yml wins.
        for _ in range(5):
            store.upsert_pair(
                display_name="person-x",
                email="wrong@example.com",
                source="gmail",
            )
        store.upsert_pair(
            display_name="person-x",
            email="person-a@example.com",
            source="people_yml",
        )
        assert store.resolve_name_to_email("person-x") == "person-a@example.com"

    def test_cross_source_aggregation(self, store: DirectoryStore) -> None:
        # Bob seen once in gmail + twice in calendar = 3 total; Alice
        # seen 2 + 0 = 2. Bob wins.
        store.upsert_pair(
            display_name="Person", email="bob@x.com", source="gmail"
        )
        store.upsert_pair(
            display_name="Person", email="bob@x.com", source="calendar"
        )
        store.upsert_pair(
            display_name="Person", email="bob@x.com", source="calendar"
        )
        store.upsert_pair(
            display_name="Person", email="alice@x.com", source="gmail"
        )
        store.upsert_pair(
            display_name="Person", email="alice@x.com", source="gmail"
        )
        assert store.resolve_name_to_email("Person") == "bob@x.com"

    def test_empty_name_rows_skipped(self, store: DirectoryStore) -> None:
        # Bare-email Gmail headers leave display_name=''; we shouldn't
        # answer "" → email even though the row exists.
        store.upsert_pair(
            display_name=None, email="bob@example.com", source="gmail"
        )
        # normalize_participant("") returns None, so the function exits
        # early — but additionally the display_name <> '' clause guards
        # against any caller that constructs a normalized empty string.
        assert store.resolve_name_to_email("") is None


class TestAllEmails:
    """Distinct-email projection for membership checks."""

    def test_empty_table_returns_empty_set(self, store: DirectoryStore) -> None:
        assert store.all_emails() == set()

    def test_distinct_across_sources(self, store: DirectoryStore) -> None:
        store.upsert_pair(
            display_name="Bob", email="bob@example.com", source="gmail"
        )
        store.upsert_pair(
            display_name="Bob", email="bob@example.com", source="calendar"
        )
        store.upsert_pair(
            display_name="Alice", email="alice@example.com", source="gmail"
        )
        assert store.all_emails() == {"bob@example.com", "alice@example.com"}


class TestLoadPeopleYml:
    """File-system parsing — happy path + every documented failure mode."""

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_people_yml(tmp_path) == {}

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "_people.yml").write_text("")
        assert load_people_yml(tmp_path) == {}

    def test_valid_mapping(self, tmp_path: Path) -> None:
        (tmp_path / "_people.yml").write_text(
            "person-x last-a: person-a@example.com\nBob: bob@example.com\n"
        )
        result = load_people_yml(tmp_path)
        assert result == {
            "person-x last-a": "person-a@example.com",
            "bob": "bob@example.com",
        }

    def test_malformed_yaml_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "_people.yml").write_text("foo: bar:\n  - bad: : :")
        with caplog.at_level(logging.WARNING):
            result = load_people_yml(tmp_path)
        assert result == {}
        assert any(
            "malformed _people.yml" in r.message for r in caplog.records
        )

    def test_top_level_not_a_dict_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "_people.yml").write_text("- one\n- two\n")
        with caplog.at_level(logging.WARNING):
            result = load_people_yml(tmp_path)
        assert result == {}
        assert any("expected top-level mapping" in r.message for r in caplog.records)

    def test_non_string_value_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "_people.yml").write_text(
            "Bob: bob@example.com\nAlice: 12345\n"
        )
        with caplog.at_level(logging.WARNING):
            result = load_people_yml(tmp_path)
        assert result == {"bob": "bob@example.com"}
        assert any(
            "skipping non-string entry" in r.message for r in caplog.records
        )

    def test_display_names_normalized(self, tmp_path: Path) -> None:
        (tmp_path / "_people.yml").write_text(
            "  person-x last-a  : person-x@example.com\n"
        )
        result = load_people_yml(tmp_path)
        assert result == {"person-x last-a": "person-x@example.com"}

    def test_invalid_email_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "_people.yml").write_text(
            "Bob: not-an-email\nAlice: alice@example.com\n"
        )
        with caplog.at_level(logging.WARNING):
            result = load_people_yml(tmp_path)
        assert result == {"alice": "alice@example.com"}
        assert any("invalid email" in r.message for r in caplog.records)

    def test_oserror_reading_file_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Hitting the OSError branch: ``_people.yml`` exists but is a
        # directory, so opening it for reading raises IsADirectoryError
        # (a subclass of OSError).
        (tmp_path / "_people.yml").mkdir()
        with caplog.at_level(logging.WARNING):
            result = load_people_yml(tmp_path)
        assert result == {}
        assert any(
            "could not read _people.yml" in r.message for r in caplog.records
        )

    def test_unnormalizable_name_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Single-letter name fails normalize_participant's _MIN_NAME_LENGTH
        # filter and is dropped with a warning.
        (tmp_path / "_people.yml").write_text(
            "A: short@example.com\nBob: bob@example.com\n"
        )
        with caplog.at_level(logging.WARNING):
            result = load_people_yml(tmp_path)
        assert result == {"bob": "bob@example.com"}
        assert any(
            "unnormalizable name" in r.message for r in caplog.records
        )


class TestRefreshPeopleYml:
    """End-to-end ``_people.yml`` → ``directory_entries`` wiring (Task B.7)."""

    def test_missing_file_clears_existing_rows(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        """No file → 0 returned, any prior people_yml rows wiped.

        Authoritative semantics: removing the file is the way to clear.
        """
        DirectoryStore(test_db).upsert_pair(
            display_name="Stale", email="stale@x.com", source="people_yml"
        )
        assert _row_count(test_db, "source = 'people_yml'") == 1

        loaded = refresh_people_yml(test_db, tmp_path)
        assert loaded == 0
        assert _row_count(test_db, "source = 'people_yml'") == 0

    def test_valid_file_inserts_pairs(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        (tmp_path / "_people.yml").write_text(
            "person-person-luke: person-person-luke@example.com\n"
            "person-person-marc: person-person-marc@example.com\n"
        )
        loaded = refresh_people_yml(test_db, tmp_path)
        assert loaded == 2

        rows = test_db.execute(
            "SELECT display_name, email FROM directory_entries "
            "WHERE source = 'people_yml' ORDER BY display_name"
        ).fetchall()
        assert rows == [
            ("person-person-luke", "person-person-luke@example.com"),
            ("person-person-marc", "person-person-marc@example.com"),
        ]

    def test_replaces_existing_rows_on_re_refresh(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        """Regression: edits to ``_people.yml`` must drop old entries.

        Append-only would leave stale name→email mappings haunting the
        directory after the user removes a person from the file. The
        delete-first contract in :func:`refresh_people_yml` prevents this.
        """
        path = tmp_path / "_people.yml"
        path.write_text("Old Person: old@example.com\n")
        refresh_people_yml(test_db, tmp_path)
        assert _row_count(test_db, "source = 'people_yml'") == 1

        path.write_text("New Person: new@example.com\n")
        refresh_people_yml(test_db, tmp_path)

        rows = test_db.execute(
            "SELECT display_name, email FROM directory_entries "
            "WHERE source = 'people_yml'"
        ).fetchall()
        assert rows == [("new person", "new@example.com")]

    def test_malformed_file_clears_rows_and_returns_zero(
        self,
        test_db: psycopg.Connection,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Malformed YAML → 0 returned, prior rows wiped, warning logged.

        ``load_people_yml`` already logs the parse warning; we just verify
        the refresh follows the file's effective (empty) content.
        """
        DirectoryStore(test_db).upsert_pair(
            display_name="Stale", email="stale@x.com", source="people_yml"
        )
        (tmp_path / "_people.yml").write_text("foo: bar:\n  - bad: : :")

        with caplog.at_level(logging.WARNING):
            loaded = refresh_people_yml(test_db, tmp_path)

        assert loaded == 0
        assert _row_count(test_db, "source = 'people_yml'") == 0
        assert any(
            "malformed _people.yml" in r.message for r in caplog.records
        )

    def test_does_not_touch_other_sources(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        """The DELETE is scoped — gmail/calendar/contacts rows must survive."""
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Bob", email="bob@x.com", source="gmail"
        )
        store.upsert_pair(
            display_name="Bob", email="bob@x.com", source="calendar"
        )
        (tmp_path / "_people.yml").write_text("Bob: bob@x.com\n")

        refresh_people_yml(test_db, tmp_path)

        sources = {
            r[0]
            for r in test_db.execute(
                "SELECT DISTINCT source FROM directory_entries"
            ).fetchall()
        }
        assert sources == {"gmail", "calendar", "people_yml"}

    def test_writes_no_refresh_state_row(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        """``people_yml`` is excluded from ``_REFRESH_STATE_SOURCES`` by design.

        It has no refresh cadence — every refresh re-reads the file —
        so a refresh-state row would be misleading. Verify none is written.
        """
        (tmp_path / "_people.yml").write_text("Bob: bob@x.com\n")
        refresh_people_yml(test_db, tmp_path)

        assert _refresh_state_row(test_db, "people_yml") is None

    def test_resolution_prefers_people_yml(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        """End-to-end: a ``_people.yml`` entry beats a higher-count gmail row."""
        store = DirectoryStore(test_db)
        for _ in range(5):
            store.upsert_pair(
                display_name="person-person-luke",
                email="wrong@example.com",
                source="gmail",
            )
        (tmp_path / "_people.yml").write_text(
            "person-person-luke: person-person-luke@example.com\n"
        )
        refresh_people_yml(test_db, tmp_path)

        assert (
            store.resolve_name_to_email("person-person-luke") == "person-person-luke@example.com"
        )


# --------------------------------------------------------------------------
# Refresh helpers — driven via fake GwsRunner; no subprocess in tests.
# --------------------------------------------------------------------------


def _refresh_state_row(
    conn: psycopg.Connection, source: str
) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT source, last_refreshed_at, records_seen "
        "FROM directory_refresh_state WHERE source = %s",
        (source,),
    ).fetchone()


class TestRefreshCalendar:
    """Calendar refresh — happy path, errors, edge cases on attendees."""

    def test_one_event_two_attendees(self, test_db: psycopg.Connection) -> None:
        runner = FakeRunner(
            response=json.dumps(
                [
                    {
                        "summary": "Sync",
                        "attendees": [
                            {
                                "email": "bob@example.com",
                                "displayName": "Bob Smith",
                            },
                            {
                                "email": "alice@example.com",
                                "displayName": "Alice",
                            },
                        ],
                    }
                ]
            )
        )
        result = refresh_calendar(
            test_db,
            since=datetime.datetime(2026, 4, 1),
            until=datetime.datetime(2026, 5, 1),
            runner=runner,
        )
        assert result == 1
        emails = {
            r[0]
            for r in test_db.execute(
                "SELECT email FROM directory_entries WHERE source = 'calendar'"
            ).fetchall()
        }
        assert emails == {"bob@example.com", "alice@example.com"}
        # last_refreshed_at was bumped + records_seen advanced.
        row = _refresh_state_row(test_db, "calendar")
        assert row is not None
        assert row[0] == "calendar"
        assert row[2] == 1

    def test_runner_raises_returns_zero(
        self,
        test_db: psycopg.Connection,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        runner = FakeRunner(raises=RuntimeError("gws not on PATH"))
        with caplog.at_level(logging.WARNING):
            result = refresh_calendar(
                test_db,
                since=datetime.datetime(2026, 4, 1),
                until=datetime.datetime(2026, 5, 1),
                runner=runner,
            )
        assert result == 0
        assert _refresh_state_row(test_db, "calendar") is None
        assert _row_count(test_db) == 0
        assert any(
            "calendar refresh failed" in r.message for r in caplog.records
        )

    def test_empty_event_list_advances_state(
        self, test_db: psycopg.Connection
    ) -> None:
        runner = FakeRunner(response="[]")
        result = refresh_calendar(
            test_db,
            since=datetime.datetime(2026, 4, 1),
            until=datetime.datetime(2026, 5, 1),
            runner=runner,
        )
        assert result == 0
        row = _refresh_state_row(test_db, "calendar")
        assert row is not None
        assert row[2] == 0  # records_seen unchanged from default

    def test_attendee_without_display_name_upserted_with_empty(
        self, test_db: psycopg.Connection
    ) -> None:
        runner = FakeRunner(
            response=json.dumps(
                [
                    {
                        "attendees": [
                            {"email": "noname@example.com"},
                        ],
                    }
                ]
            )
        )
        result = refresh_calendar(
            test_db,
            since=datetime.datetime(2026, 4, 1),
            until=datetime.datetime(2026, 5, 1),
            runner=runner,
        )
        assert result == 1
        row = test_db.execute(
            "SELECT display_name FROM directory_entries "
            "WHERE email = 'noname@example.com'"
        ).fetchone()
        assert row is not None
        assert row[0] == ""

    def test_attendee_without_email_skipped(
        self, test_db: psycopg.Connection
    ) -> None:
        runner = FakeRunner(
            response=json.dumps(
                [
                    {
                        "attendees": [
                            {"displayName": "Anon"},
                            {"email": "real@example.com", "displayName": "Real"},
                        ],
                    }
                ]
            )
        )
        result = refresh_calendar(
            test_db,
            since=datetime.datetime(2026, 4, 1),
            until=datetime.datetime(2026, 5, 1),
            runner=runner,
        )
        assert result == 1
        emails = {
            r[0]
            for r in test_db.execute(
                "SELECT email FROM directory_entries"
            ).fetchall()
        }
        assert emails == {"real@example.com"}

    def test_malformed_json_returns_zero(
        self, test_db: psycopg.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner = FakeRunner(response="not-json{{{")
        with caplog.at_level(logging.WARNING):
            result = refresh_calendar(
                test_db,
                since=datetime.datetime(2026, 4, 1),
                until=datetime.datetime(2026, 5, 1),
                runner=runner,
            )
        assert result == 0
        assert _refresh_state_row(test_db, "calendar") is None
        assert any(
            "JSON parse failed" in r.message for r in caplog.records
        )

    def test_unrecognized_shape_returns_zero(
        self, test_db: psycopg.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A JSON object that's neither a top-level list nor a Calendar
        # response (no ``items`` key) is unrecoverable — the parser
        # returns None and we warn + abort with the same "JSON parse
        # failed" warning we emit on truly malformed input.
        runner = FakeRunner(response='{"unexpected": "shape"}')
        with caplog.at_level(logging.WARNING):
            result = refresh_calendar(
                test_db,
                since=datetime.datetime(2026, 4, 1),
                until=datetime.datetime(2026, 5, 1),
                runner=runner,
            )
        assert result == 0
        assert any(
            "JSON parse failed" in r.message
            and "could not normalize" in r.message
            for r in caplog.records
        )

    def test_non_dict_event_skipped(self, test_db: psycopg.Connection) -> None:
        # A stray non-dict element shouldn't bring the refresh down.
        runner = FakeRunner(
            response=json.dumps(
                [
                    "garbage-event-not-a-dict",
                    {
                        "attendees": [
                            {"email": "real@example.com", "displayName": "Real"},
                        ],
                    },
                ]
            )
        )
        result = refresh_calendar(
            test_db,
            since=datetime.datetime(2026, 4, 1),
            until=datetime.datetime(2026, 5, 1),
            runner=runner,
        )
        assert result == 1
        emails = {
            r[0]
            for r in test_db.execute(
                "SELECT email FROM directory_entries"
            ).fetchall()
        }
        assert emails == {"real@example.com"}

    def test_attendees_not_a_list_skipped(
        self, test_db: psycopg.Connection
    ) -> None:
        # ``attendees`` defensively allowed to be missing/non-list — event
        # is still counted but no upserts happen.
        runner = FakeRunner(
            response=json.dumps([{"attendees": "should-be-a-list"}])
        )
        result = refresh_calendar(
            test_db,
            since=datetime.datetime(2026, 4, 1),
            until=datetime.datetime(2026, 5, 1),
            runner=runner,
        )
        assert result == 1
        assert _row_count(test_db) == 0

    def test_non_dict_attendee_skipped(
        self, test_db: psycopg.Connection
    ) -> None:
        runner = FakeRunner(
            response=json.dumps(
                [
                    {
                        "attendees": [
                            "not-a-dict",
                            {"email": "real@example.com"},
                        ],
                    }
                ]
            )
        )
        result = refresh_calendar(
            test_db,
            since=datetime.datetime(2026, 4, 1),
            until=datetime.datetime(2026, 5, 1),
            runner=runner,
        )
        assert result == 1
        assert _row_count(test_db) == 1

    def test_runner_invoked_with_iso_time_range(
        self, test_db: psycopg.Connection
    ) -> None:
        # Real ``gws`` takes Calendar API params as a single ``--params``
        # JSON blob; the refresh helper pins ``calendarId``, ``timeMin``,
        # ``timeMax``, ``singleEvents`` and ``maxResults`` inside it.
        runner = FakeRunner(response="[]")
        since = datetime.datetime(2026, 4, 1, 12, 0, 0)
        until = datetime.datetime(2026, 5, 1, 12, 0, 0)
        refresh_calendar(test_db, since=since, until=until, runner=runner)
        assert len(runner.calls) == 1
        call = runner.calls[0]
        # Subcommand shape: gws calendar events list --params <JSON>
        # --format json --page-all
        assert call[:4] == ["gws", "calendar", "events", "list"]
        assert "--params" in call
        params = json.loads(call[call.index("--params") + 1])
        assert params["calendarId"] == "primary"
        assert params["timeMin"] == since.isoformat()
        assert params["timeMax"] == until.isoformat()
        assert params["singleEvents"] is True
        assert "--format" in call
        assert call[call.index("--format") + 1] == "json"
        assert "--page-all" in call

    def test_records_seen_accumulates_across_refreshes(
        self, test_db: psycopg.Connection
    ) -> None:
        """Two consecutive refreshes correctly sum records_seen via ON CONFLICT."""
        runner_one = FakeRunner(
            response=json.dumps(
                [
                    {
                        "summary": "Sync 1",
                        "attendees": [
                            {"email": "a@example.com", "displayName": "Alice"}
                        ],
                    }
                ]
            )
        )
        runner_two = FakeRunner(
            response=json.dumps(
                [
                    {
                        "summary": "Sync 2",
                        "attendees": [
                            {"email": "b@example.com", "displayName": "Bob"}
                        ],
                    }
                ]
            )
        )
        refresh_calendar(
            test_db,
            since=datetime.datetime(2026, 4, 1),
            until=datetime.datetime(2026, 4, 15),
            runner=runner_one,
        )
        refresh_calendar(
            test_db,
            since=datetime.datetime(2026, 4, 15),
            until=datetime.datetime(2026, 5, 1),
            runner=runner_two,
        )
        row = _refresh_state_row(test_db, "calendar")
        assert row is not None
        assert row[2] == 2  # 1 + 1, not overwritten

    def test_value_error_in_upsert_logs_debug_and_continues(
        self,
        test_db: psycopg.Connection,
        caplog: pytest.LogCaptureFixture,
        mocker: MockerFixture,
    ) -> None:
        """Defensive ValueError catch around upsert_pair logs at DEBUG and continues.

        The upstream ``email.strip()`` guard makes this unreachable in
        practice, so we force the path with a mocked side_effect to
        exercise the logging contract without monkey-patching production
        internals (standard pytest-mock usage; auto-restored after test).
        """
        mocker.patch.object(
            DirectoryStore,
            "upsert_pair",
            side_effect=[
                ValueError("synthetic"),  # first attendee
                None,  # second attendee — succeeds
            ],
        )
        runner = FakeRunner(
            response=json.dumps(
                [
                    {
                        "attendees": [
                            {"email": "bad@example.com", "displayName": "Bad"},
                            {"email": "good@example.com", "displayName": "Good"},
                        ],
                    }
                ]
            )
        )
        with caplog.at_level(logging.DEBUG, logger="brain.vault.derived_links.directory"):
            result = refresh_calendar(
                test_db,
                since=datetime.datetime(2026, 4, 1),
                until=datetime.datetime(2026, 5, 1),
                runner=runner,
            )
        assert result == 1
        assert any(
            "skipping attendee with invalid email" in r.message
            and "bad@example.com" in r.message
            for r in caplog.records
        )


class TestRefreshContacts:
    """Contacts refresh — same shape as calendar with People-API JSON."""

    def test_one_contact_one_email(self, test_db: psycopg.Connection) -> None:
        runner = FakeRunner(
            response=json.dumps(
                [
                    {
                        "names": [{"displayName": "person-x last-a"}],
                        "emailAddresses": [{"value": "person-a@example.com"}],
                    }
                ]
            )
        )
        result = refresh_contacts(test_db, runner=runner)
        assert result == 1
        rows = test_db.execute(
            "SELECT display_name, email FROM directory_entries"
        ).fetchall()
        assert rows == [("person-x last-a", "person-a@example.com")]

    def test_contact_with_multiple_emails(
        self, test_db: psycopg.Connection
    ) -> None:
        runner = FakeRunner(
            response=json.dumps(
                [
                    {
                        "names": [{"displayName": "Bob"}],
                        "emailAddresses": [
                            {"value": "bob@home.com"},
                            {"value": "bob@work.com"},
                        ],
                    }
                ]
            )
        )
        result = refresh_contacts(test_db, runner=runner)
        assert result == 1
        emails = {
            r[0]
            for r in test_db.execute(
                "SELECT email FROM directory_entries"
            ).fetchall()
        }
        assert emails == {"bob@home.com", "bob@work.com"}

    def test_contact_without_emails_skipped(
        self, test_db: psycopg.Connection
    ) -> None:
        runner = FakeRunner(
            response=json.dumps(
                [
                    {"names": [{"displayName": "Bob"}], "emailAddresses": []},
                    {
                        "names": [{"displayName": "Alice"}],
                        "emailAddresses": [{"value": "alice@example.com"}],
                    },
                ]
            )
        )
        result = refresh_contacts(test_db, runner=runner)
        assert result == 1
        rows = test_db.execute(
            "SELECT display_name, email FROM directory_entries"
        ).fetchall()
        assert rows == [("alice", "alice@example.com")]

    def test_contact_without_names_still_upserts(
        self, test_db: psycopg.Connection
    ) -> None:
        # A contact with an email but no display name should still seed
        # the directory; resolve_name_to_email won't find it (empty name)
        # but ``all_emails`` will, which is the point.
        runner = FakeRunner(
            response=json.dumps(
                [
                    {
                        "names": [],
                        "emailAddresses": [{"value": "anon@example.com"}],
                    }
                ]
            )
        )
        result = refresh_contacts(test_db, runner=runner)
        assert result == 1
        row = test_db.execute(
            "SELECT display_name, email FROM directory_entries"
        ).fetchone()
        assert row is not None
        assert row == ("", "anon@example.com")

    def test_runner_raises_returns_zero(
        self,
        test_db: psycopg.Connection,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        runner = FakeRunner(raises=RuntimeError("gws not on PATH"))
        with caplog.at_level(logging.WARNING):
            result = refresh_contacts(test_db, runner=runner)
        assert result == 0
        assert _refresh_state_row(test_db, "contacts") is None
        assert _row_count(test_db) == 0
        assert any(
            "people refresh failed" in r.message for r in caplog.records
        )

    def test_malformed_json_returns_zero(
        self, test_db: psycopg.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner = FakeRunner(response="garbage{")
        with caplog.at_level(logging.WARNING):
            result = refresh_contacts(test_db, runner=runner)
        assert result == 0
        assert _refresh_state_row(test_db, "contacts") is None
        assert any(
            "JSON parse failed" in r.message for r in caplog.records
        )

    def test_unrecognized_shape_returns_zero(
        self, test_db: psycopg.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        # See the matching calendar test for the rationale: a JSON object
        # without ``otherContacts`` is unrecoverable; we collapse all
        # parse-time failures under the same "JSON parse failed" warning.
        runner = FakeRunner(response='{"unexpected": "shape"}')
        with caplog.at_level(logging.WARNING):
            result = refresh_contacts(test_db, runner=runner)
        assert result == 0
        assert any(
            "JSON parse failed" in r.message
            and "could not normalize" in r.message
            for r in caplog.records
        )

    def test_non_dict_contact_skipped(self, test_db: psycopg.Connection) -> None:
        runner = FakeRunner(
            response=json.dumps(
                [
                    "garbage",
                    {
                        "names": [{"displayName": "Bob"}],
                        "emailAddresses": [{"value": "bob@example.com"}],
                    },
                ]
            )
        )
        result = refresh_contacts(test_db, runner=runner)
        assert result == 1
        assert _row_count(test_db) == 1

    def test_names_not_a_list_skipped(self, test_db: psycopg.Connection) -> None:
        # Defensive: malformed ``names`` shouldn't crash; the contact is
        # silently skipped (no upsert).
        runner = FakeRunner(
            response=json.dumps(
                [{"names": "not-a-list", "emailAddresses": []}]
            )
        )
        result = refresh_contacts(test_db, runner=runner)
        assert result == 0
        assert _row_count(test_db) == 0

    def test_non_dict_name_object_falls_through_to_no_display(
        self, test_db: psycopg.Connection
    ) -> None:
        # First name entry is malformed; the contact still upserts via the
        # email but with display_name=''.
        runner = FakeRunner(
            response=json.dumps(
                [
                    {
                        "names": ["not-a-dict", {"displayName": ""}],
                        "emailAddresses": [{"value": "x@example.com"}],
                    }
                ]
            )
        )
        result = refresh_contacts(test_db, runner=runner)
        assert result == 1
        row = test_db.execute(
            "SELECT display_name, email FROM directory_entries"
        ).fetchone()
        assert row is not None
        assert row == ("", "x@example.com")

    def test_email_object_without_value_skipped(
        self, test_db: psycopg.Connection
    ) -> None:
        runner = FakeRunner(
            response=json.dumps(
                [
                    {
                        "names": [{"displayName": "Bob"}],
                        "emailAddresses": [
                            "not-a-dict",
                            {"primary": True},  # missing 'value'
                            {"value": "bob@example.com"},
                        ],
                    }
                ]
            )
        )
        result = refresh_contacts(test_db, runner=runner)
        assert result == 1
        emails = {
            r[0]
            for r in test_db.execute(
                "SELECT email FROM directory_entries"
            ).fetchall()
        }
        assert emails == {"bob@example.com"}

    def test_records_seen_accumulates_across_refreshes(
        self, test_db: psycopg.Connection
    ) -> None:
        runner_one = FakeRunner(
            response=json.dumps(
                [
                    {
                        "names": [{"displayName": "Alice"}],
                        "emailAddresses": [{"value": "a@example.com"}],
                    }
                ]
            )
        )
        runner_two = FakeRunner(
            response=json.dumps(
                [
                    {
                        "names": [{"displayName": "Bob"}],
                        "emailAddresses": [{"value": "b@example.com"}],
                    }
                ]
            )
        )
        refresh_contacts(test_db, runner=runner_one)
        refresh_contacts(test_db, runner=runner_two)
        row = _refresh_state_row(test_db, "contacts")
        assert row is not None
        assert row[2] == 2

    def test_value_error_in_upsert_logs_debug_and_continues(
        self,
        test_db: psycopg.Connection,
        caplog: pytest.LogCaptureFixture,
        mocker: MockerFixture,
    ) -> None:
        """Defensive ValueError catch in contacts mirrors the calendar one."""
        mocker.patch.object(
            DirectoryStore,
            "upsert_pair",
            side_effect=[
                ValueError("synthetic"),
                None,
            ],
        )
        runner = FakeRunner(
            response=json.dumps(
                [
                    {
                        "names": [{"displayName": "Bob"}],
                        "emailAddresses": [
                            {"value": "bad@example.com"},
                            {"value": "good@example.com"},
                        ],
                    }
                ]
            )
        )
        with caplog.at_level(logging.DEBUG, logger="brain.vault.derived_links.directory"):
            result = refresh_contacts(test_db, runner=runner)
        # Contact had at least one successful upsert → counted as 1.
        assert result == 1
        assert any(
            "skipping email with invalid shape" in r.message
            and "bad@example.com" in r.message
            for r in caplog.records
        )


class TestUpdateRefreshState:
    """Direct guard test for ``_update_refresh_state`` (Fix 5)."""

    @pytest.mark.parametrize(
        "valid_source", ["gmail", "calendar", "contacts"]
    )
    def test_valid_sources_succeed(
        self, test_db: psycopg.Connection, valid_source: str
    ) -> None:
        _update_refresh_state(test_db, source=valid_source, records_seen=3)
        row = test_db.execute(
            "SELECT records_seen FROM directory_refresh_state WHERE source = %s",
            (valid_source,),
        ).fetchone()
        assert row is not None
        assert row[0] == 3

    @pytest.mark.parametrize(
        "invalid_source", ["people_yml", "slack", "", "GMAIL"]
    )
    def test_invalid_source_raises_value_error(
        self, test_db: psycopg.Connection, invalid_source: str
    ) -> None:
        with pytest.raises(ValueError, match="invalid refresh-state source"):
            _update_refresh_state(
                test_db, source=invalid_source, records_seen=1
            )


class TestScoreDirectoryRows:
    """Direct unit tests for the shared ``_score_directory_rows`` helper.

    Both :meth:`DirectoryStore.resolve_name_to_email` (skip-ambiguous) and
    the People Hub aggregator's primary-email picker (alpha tiebreak) call
    into this. Pinning its semantics directly here protects both surfaces
    from future drift without requiring a full DB round-trip.
    """

    def test_empty_input_returns_none(self) -> None:
        assert _score_directory_rows([]) is None
        assert _score_directory_rows([], skip_ambiguous=True) is None

    def test_single_row_wins(self) -> None:
        assert (
            _score_directory_rows([("alice@x.com", 1, False)])
            == "alice@x.com"
        )

    def test_people_yml_row_beats_higher_count(self) -> None:
        # Higher non-yml count must NOT override a single people_yml row.
        rows = [
            ("wrong@x.com", 99, False),
            ("right@x.com", 1, True),
        ]
        assert _score_directory_rows(rows) == "right@x.com"
        assert _score_directory_rows(rows, skip_ambiguous=True) == "right@x.com"

    def test_multiple_people_yml_rows_resolve_alphabetically(self) -> None:
        # Caller bug — both rows carry people_yml=True. Determinism wins:
        # alphabetically-first target.
        rows = [
            ("zebra@x.com", 5, True),
            ("alpha@x.com", 1, True),
        ]
        assert _score_directory_rows(rows) == "alpha@x.com"

    def test_highest_count_wins_when_no_people_yml(self) -> None:
        rows = [
            ("a@x.com", 1, False),
            ("b@x.com", 5, False),
            ("c@x.com", 3, False),
        ]
        assert _score_directory_rows(rows) == "b@x.com"

    def test_tie_alpha_default_picks_alpha_first(self) -> None:
        rows = [
            ("zeta@x.com", 5, False),
            ("alpha@x.com", 5, False),
        ]
        assert _score_directory_rows(rows, skip_ambiguous=False) == "alpha@x.com"

    def test_tie_skip_ambiguous_returns_none(self) -> None:
        rows = [
            ("zeta@x.com", 5, False),
            ("alpha@x.com", 5, False),
        ]
        assert _score_directory_rows(rows, skip_ambiguous=True) is None

    def test_skip_ambiguous_unique_top_returns_winner(self) -> None:
        rows = [
            ("a@x.com", 5, False),
            ("b@x.com", 4, False),
            ("c@x.com", 4, False),
        ]
        # Top is unique even though the runners-up tie — winner survives.
        assert _score_directory_rows(rows, skip_ambiguous=True) == "a@x.com"

    def test_input_order_independent(self) -> None:
        rows_one = [("a", 1, False), ("b", 5, False)]
        rows_two = [("b", 5, False), ("a", 1, False)]
        assert _score_directory_rows(rows_one) == _score_directory_rows(rows_two)
