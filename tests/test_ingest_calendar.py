"""Unit tests for :mod:`brain.ingest.calendar` (RM-30): ``gws`` JSON to documents.

Pure: no database, and the ``gws`` runner is the synthetic ``FakeGws``.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from brain.ingest import calendar as cal
from brain.ingest.calendar import CalendarDoc, CalendarError
from tests import calendar_fixtures as fx

_MIN = datetime(2026, 10, 1, tzinfo=UTC)
_MAX = datetime(2026, 11, 1, tzinfo=UTC)


def _build(events: list[dict], masters: dict | None = None) -> cal.CalendarBatch:
    gws = fx.FakeGws(events, masters)
    return cal.build_documents(
        events, master_lookup=lambda sid: cal.get_event("primary", sid, runner=gws)
    )


def _by_id(batch: cal.CalendarBatch) -> dict[str, CalendarDoc]:
    return {d.external_id: d for d in batch.documents}


# --- the gws calls ------------------------------------------------------------


def test_list_events_uses_the_refresh_calendar_call_shape() -> None:
    gws = fx.FakeGws([fx.single(), fx.private()])

    events = cal.list_events("team@example.com", _MIN, _MAX, runner=gws)

    assert [e["id"] for e in events] == [fx.SINGLE_ID, fx.PRIVATE_ID]
    args = gws.calls[0]
    assert args[:5] == ["gws", "calendar", "events", "list", "--params"]
    assert args[6:] == ["--format", "json", "--page-all"]
    assert json.loads(args[5]) == {
        "calendarId": "team@example.com",
        "timeMin": _MIN.isoformat(),
        "timeMax": _MAX.isoformat(),
        "singleEvents": True,
        "maxResults": 250,
    }


def test_list_events_raises_on_unparseable_output() -> None:
    with pytest.raises(CalendarError, match="could not parse"):
        cal.list_events("primary", _MIN, _MAX, runner=lambda args: "not json {")


def test_list_events_wraps_a_runner_failure() -> None:
    def failing(args: list[str]) -> str:
        raise RuntimeError("gws exploded")

    with pytest.raises(CalendarError, match="gws calendar events list failed"):
        cal.list_events("primary", _MIN, _MAX, runner=failing)


def test_get_event_returns_none_when_the_master_cannot_be_read() -> None:
    gws = fx.FakeGws([], masters={})

    assert cal.get_event("primary", "missing", runner=gws) is None
    assert gws.calls[0][:4] == ["gws", "calendar", "events", "get"]
    assert json.loads(gws.calls[0][5]) == {"calendarId": "primary", "eventId": "missing"}


def test_get_event_returns_none_on_bad_json() -> None:
    assert cal.get_event("primary", "x", runner=lambda args: "[1, 2]") is None
    assert cal.get_event("primary", "x", runner=lambda args: "{nope") is None


# --- fixture classes ------------------------------------------------------------


def test_a_single_event_lands_as_one_document() -> None:
    doc = _by_id(_build([fx.single()]))[fx.SINGLE_ID]

    assert doc.doc.title == fx.SINGLE_TITLE
    assert doc.doc.content_type == "calendar_event"
    assert doc.confidential is False
    body = doc.doc.content
    assert body.splitlines()[0] == fx.SINGLE_TITLE
    assert "When: 2026-10-06 10:00–10:45 (UTC-07:00)" in body
    assert "Location: Acme HQ, 1 Example Way" in body
    assert "Attendees: Owner Sample, Avery Sample" in body
    assert "Board Room" not in body  # resource rooms are not people
    assert "Agenda: pricing and rollout." in body
    meta = doc.doc.metadata
    assert meta["date"] == "2026-10-06T10:00:00-07:00"
    assert meta["participants"] == ["avery@example.com", "owner@example.com"]
    assert meta["event_id"] == fx.SINGLE_ID
    assert "occurrences" not in meta
    assert doc.attendees == (
        ("Owner Sample", "owner@example.com"),
        ("Avery Sample", "avery@example.com"),
    )


def test_conference_links_dial_ins_urls_and_addresses_never_reach_the_body() -> None:
    batch = _build([fx.single(), *fx.series_instances()], {fx.SERIES_ID: fx.series_master()})

    for doc in batch.documents:
        body = doc.doc.content
        for forbidden in (
            "meet.google.com",
            "zoom.us",
            "SECRETPWD",
            "424242",
            "123456",
            "555-010-0199",
            "Meeting ID",
            "maps.example.com",
            "-::~",
            "@",
        ):
            assert forbidden not in body, (forbidden, body)


def test_a_recurring_series_is_one_document_with_occurrences_in_metadata() -> None:
    batch = _build(fx.series_instances(), {fx.SERIES_ID: fx.series_master()})

    doc = _by_id(batch)[fx.SERIES_ID]
    assert len(batch.documents) == 1
    body = doc.doc.content
    assert doc.doc.title == fx.SERIES_TITLE  # the master's title, not the exception's
    assert "When: recurring, RRULE:FREQ=WEEKLY;BYDAY=TU, 09:00–09:30 (UTC-07:00)" in body
    assert "EXDATE" not in body  # a cancelled instance must not change the body
    assert "Status round." in body
    assert "Notes doc: https://docs.example.com/notes" in body
    for day in ("2026-10-06", "2026-10-13", "2026-10-27"):
        assert day not in body  # occurrence dates are volatile: metadata only
    meta = doc.doc.metadata
    assert meta["series_id"] == fx.SERIES_ID
    assert meta["occurrences"] == {
        "count": 3,  # the declined instance is not an occurrence
        "last": [
            "2026-10-06T09:00:00-07:00",
            "2026-10-13T11:00:00-07:00",
            "2026-10-27T09:00:00-07:00",
        ],
    }
    assert meta["date"] == "2026-10-27T09:00:00-07:00"  # the latest occurrence


def test_occurrences_keep_only_the_last_ten() -> None:
    days = [f"2026-10-{d:02d}" for d in range(1, 16)]
    instances = [fx.series_instance(day) for day in days]

    doc = _by_id(_build(instances, {fx.SERIES_ID: fx.series_master()}))[fx.SERIES_ID]

    occ = doc.doc.metadata["occurrences"]
    assert occ["count"] == 15
    assert occ["last"] == [f"{day}T09:00:00-07:00" for day in days[-10:]]


def test_a_series_body_does_not_depend_on_the_window() -> None:
    master = {fx.SERIES_ID: fx.series_master()}
    early = _by_id(_build(fx.series_instances()[:1], master))[fx.SERIES_ID]
    late = _by_id(_build(fx.series_instances(), master))[fx.SERIES_ID]

    assert early.doc.content == late.doc.content
    assert early.doc.metadata["occurrences"] != late.doc.metadata["occurrences"]


def test_a_series_whose_master_cannot_be_read_falls_back_to_its_first_instance() -> None:
    doc = _by_id(_build(fx.series_instances(), masters={}))[fx.SERIES_ID]

    assert doc.doc.title == fx.SERIES_TITLE
    assert "When: recurring series, 09:00–09:30 (UTC-07:00)" in doc.doc.content


def test_a_series_the_owner_declined_is_skipped() -> None:
    master = fx.series_master(attendees=[fx.owner("declined"), fx.attendee(fx.AVERY)])

    batch = _build(fx.series_instances(), {fx.SERIES_ID: master})

    assert batch.documents == ()
    assert batch.skipped == 1


def test_a_series_with_every_instance_declined_is_skipped() -> None:
    instances = [fx.series_instance("2026-10-06", response="declined")]

    batch = _build(instances, {fx.SERIES_ID: fx.series_master()})

    assert batch.documents == ()
    assert batch.skipped == 1


def test_a_declined_event_is_skipped() -> None:
    batch = _build([fx.declined(), fx.single()])

    assert [d.external_id for d in batch.documents] == [fx.SINGLE_ID]
    assert batch.skipped == 1


def test_untitled_working_location_out_of_office_and_cancelled_are_skipped() -> None:
    batch = _build(fx.noise())

    assert batch.documents == ()
    assert batch.skipped == len(fx.noise())


def test_a_private_event_is_confidential_and_says_so_in_its_body() -> None:
    doc = _by_id(_build([fx.private()]))[fx.PRIVATE_ID]

    assert doc.confidential is True
    assert "Visibility: private" in doc.doc.content


def test_an_all_day_event_is_dated_at_utc_midnight() -> None:
    doc = _by_id(_build([fx.all_day()]))[fx.ALLDAY_ID]

    assert "When: 2026-10-14 – 2026-10-15 (all day)" in doc.doc.content
    assert doc.doc.metadata["date"] == "2026-10-14T00:00:00+00:00"


def test_a_one_day_all_day_event_names_one_date() -> None:
    one_day = fx.event(
        fx.ALLDAY_ID,
        fx.ALLDAY_TITLE,
        when={"start": {"date": "2026-10-14"}, "end": {"date": "2026-10-15"}},
    )

    doc = _by_id(_build([one_day]))[fx.ALLDAY_ID]

    assert "When: 2026-10-14 (all day)" in doc.doc.content


def test_an_event_spanning_midnight_names_both_dates() -> None:
    late = fx.event(
        fx.FUTURE_ID,
        fx.FUTURE_TITLE,
        when=fx.timed("2026-10-06T22:00:00-07:00", "2026-10-07T01:00:00-07:00"),
    )

    doc = _by_id(_build([late]))[fx.FUTURE_ID]

    assert "When: 2026-10-06 22:00 – 2026-10-07 01:00 (UTC-07:00)" in doc.doc.content


def test_a_future_event_is_dated_by_its_start() -> None:
    doc = _by_id(_build([fx.future("2026-11-05T10:00:00Z", "2026-11-05T11:00:00Z")]))

    assert doc[fx.FUTURE_ID].doc.metadata["date"] == "2026-11-05T10:00:00Z"
    assert "When: 2026-11-05 10:00–11:00 (UTC)" in doc[fx.FUTURE_ID].doc.content


def test_an_event_with_no_parseable_start_is_skipped() -> None:
    broken = fx.event("evtbroken001", "Broken", when={"start": {}, "end": {}})

    batch = _build([broken])

    assert batch.documents == ()
    assert batch.skipped == 1


def test_non_dict_records_are_ignored() -> None:
    batch = cal.build_documents(["junk", 3, fx.single()], master_lookup=lambda sid: None)  # type: ignore[list-item]

    assert [d.external_id for d in batch.documents] == [fx.SINGLE_ID]


# --- text cleaning ------------------------------------------------------------


def test_clean_description_drops_the_invitation_block_even_when_unterminated() -> None:
    text = "Keep this.\n-::~:~::~:~::-\nJoin: https://meet.google.com/x\nPIN: 1#"

    assert cal.clean_description(text) == "Keep this."


def test_clean_location_with_only_a_link_is_empty() -> None:
    assert cal.clean_location("https://example.zoom.us/j/1") == ""
    assert cal.clean_location(None) == ""
