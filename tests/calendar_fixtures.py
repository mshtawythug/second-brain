"""Synthetic Google Calendar ``events`` resources for the RM-30 tests.

Built from named constants only; every address is on ``example.com`` and every
title is made up. ``gws_runner`` answers the two ``gws calendar events`` calls
the ingester makes, and records every call so tests can assert the call shape.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

OWNER = ("Owner Sample", "owner@example.com")
AVERY = ("Avery Sample", "avery@example.com")
BLAKE = ("Blake Sample", "blake@example.com")
ROOM = ("Board Room", "c_room-1@resource.calendar.example.com")

SINGLE_ID = "evtsingle001"
SERIES_ID = "evtseries001"
PRIVATE_ID = "evtprivate001"
ALLDAY_ID = "evtallday001"
DECLINED_ID = "evtdeclined001"
FUTURE_ID = "evtfuture001"

SINGLE_TITLE = "Vendor intro with Acme Widgets"
SERIES_TITLE = "Weekly platform sync"
PRIVATE_TITLE = "Dentist appointment"
ALLDAY_TITLE = "Offsite planning day"
DECLINED_TITLE = "Optional lunch talk"
FUTURE_TITLE = "Roadmap review"

SINGLE_DESCRIPTION = (
    "Agenda: pricing and rollout.<br>Contact avery@example.com for the deck."
    "<br><br>-::~:~::~:~:~:~:~:~:~:~:~:~:~::~:~::-<br>"
    "Join with Google Meet: https://meet.google.com/abc-defg-hij<br>"
    "Join by phone (US) +1 555-010-0199 PIN: 123456#<br>"
    "-::~:~::~:~:~:~:~:~:~:~:~:~:~::~:~::-"
)
SERIES_DESCRIPTION = (
    "Status round.\n"
    "Join Zoom Meeting\n"
    "https://example.zoom.us/j/5550100?pwd=SECRETPWD\n"
    "Meeting ID: 555 0100\n"
    "Passcode: 424242\n"
    "Notes doc: https://docs.example.com/notes"
)


def attendee(person: tuple[str, str], **extra: Any) -> dict[str, Any]:
    return {"displayName": person[0], "email": person[1], **extra}


def owner(response: str = "accepted") -> dict[str, Any]:
    return attendee(OWNER, self=True, responseStatus=response)


def timed(start: str, end: str) -> dict[str, Any]:
    return {"start": {"dateTime": start}, "end": {"dateTime": end}}


def event(
    event_id: str,
    title: str | None,
    *,
    when: dict[str, Any],
    attendees: Sequence[dict[str, Any]] = (),
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": event_id,
        "status": "confirmed",
        "eventType": "default",
        "organizer": {"email": OWNER[1], "self": True},
        "attendees": list(attendees),
        **when,
        **extra,
    }
    if title is not None:
        body["summary"] = title
    return body


def single() -> dict[str, Any]:
    return event(
        SINGLE_ID,
        SINGLE_TITLE,
        when=timed("2026-10-06T10:00:00-07:00", "2026-10-06T10:45:00-07:00"),
        attendees=[owner(), attendee(AVERY), attendee(ROOM, resource=True)],
        description=SINGLE_DESCRIPTION,
        location="Acme HQ, 1 Example Way https://maps.example.com/acme",
        hangoutLink="https://meet.google.com/abc-defg-hij",
        conferenceData={"entryPoints": [{"uri": "https://meet.google.com/abc-defg-hij"}]},
    )


def series_master(**overrides: Any) -> dict[str, Any]:
    master = event(
        SERIES_ID,
        SERIES_TITLE,
        when=timed("2026-09-01T09:00:00-07:00", "2026-09-01T09:30:00-07:00"),
        attendees=[owner(), attendee(AVERY), attendee(BLAKE)],
        description=SERIES_DESCRIPTION,
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=TU", "EXDATE:20260915T160000Z"],
    )
    return {**master, **overrides}


def series_instance(
    day: str, *, response: str = "accepted", hour: str = "09", title: str = SERIES_TITLE
) -> dict[str, Any]:
    return event(
        f"{SERIES_ID}_{day.replace('-', '')}",
        title,
        when=timed(f"{day}T{hour}:00:00-07:00", f"{day}T{hour}:30:00-07:00"),
        attendees=[owner(response), attendee(AVERY), attendee(BLAKE)],
        description=SERIES_DESCRIPTION,
        recurringEventId=SERIES_ID,
        originalStartTime={"dateTime": f"{day}T09:00:00-07:00"},
    )


def series_instances() -> list[dict[str, Any]]:
    """Three kept instances, one moved (an exception), and one the owner declined."""
    return [
        series_instance("2026-10-06"),
        series_instance("2026-10-13", hour="11", title="Weekly platform sync (moved)"),
        series_instance("2026-10-20", response="declined"),
        series_instance("2026-10-27"),
    ]


def private() -> dict[str, Any]:
    return event(
        PRIVATE_ID,
        PRIVATE_TITLE,
        when=timed("2026-10-08T15:00:00-07:00", "2026-10-08T16:00:00-07:00"),
        visibility="private",
    )


def all_day() -> dict[str, Any]:
    return event(
        ALLDAY_ID,
        ALLDAY_TITLE,
        when={"start": {"date": "2026-10-14"}, "end": {"date": "2026-10-16"}},
        attendees=[owner(), attendee(BLAKE)],
    )


def declined() -> dict[str, Any]:
    return event(
        DECLINED_ID,
        DECLINED_TITLE,
        when=timed("2026-10-09T12:00:00-07:00", "2026-10-09T13:00:00-07:00"),
        attendees=[owner("declined"), attendee(AVERY)],
    )


def future(start: str, end: str) -> dict[str, Any]:
    return event(FUTURE_ID, FUTURE_TITLE, when=timed(start, end), attendees=[owner()])


def noise() -> list[dict[str, Any]]:
    """Events that are always skipped: untitled, working location, OOO, cancelled."""
    when = timed("2026-10-07T08:00:00-07:00", "2026-10-07T17:00:00-07:00")
    return [
        event("evtuntitled001", None, when=when),
        event("evtuntitled002", "   ", when=when),
        event("evtwork001", "Home", when=when, eventType="workingLocation"),
        event("evtooo001", "Out of office", when=when, eventType="outOfOffice"),
        event("evtcancel001", "Cancelled sync", when=when, status="cancelled"),
    ]


class FakeGws:
    """An injectable ``gws`` runner over canned events; records every call."""

    def __init__(
        self,
        events: Sequence[dict[str, Any]],
        masters: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.events = list(events)
        self.masters = dict(masters or {})
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        params = json.loads(args[args.index("--params") + 1])
        if args[:4] == ["gws", "calendar", "events", "list"]:
            # NDJSON, one page per line, as ``--page-all`` produces.
            half = len(self.events) // 2
            pages = [{"items": self.events[:half]}, {"items": self.events[half:]}]
            return "\n".join(json.dumps(page) for page in pages)
        if args[:4] == ["gws", "calendar", "events", "get"]:
            master = self.masters.get(params["eventId"])
            if master is None:
                raise RuntimeError("gws: event not found")
            return json.dumps(master)
        raise AssertionError(f"unexpected gws call: {args}")
