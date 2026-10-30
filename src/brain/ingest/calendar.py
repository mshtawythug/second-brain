"""Calendar ingester — Google Calendar events as documents, through the `gws` CLI (RM-30).

Reuses the ``gws calendar events list`` call shape and the injectable
:class:`~brain.vault.derived_links.directory.GwsRunner` of
``directory.refresh_calendar`` (which reads the same events only to mine the
people directory). This module is pure apart from the runner: it turns event
resources into :class:`CalendarDoc` values and writes nothing.

What a document holds (spec RM-30, protocol points 3 and 6):

- **One document per single event**, keyed by its event id, and **one per
  recurring series**, keyed by ``recurringEventId`` (RM-30.D1). A series reads
  its title, description, location, attendees and visibility from the series
  **master** (``gws calendar events get``), so its body does not depend on the
  run's window. If the master cannot be read, the series' first instance in the
  window stands in.
- **The body** is the title, a ``When:`` line (the event's time, or the
  master's ``RRULE`` lines and its time of day), the location with URLs
  removed, ``Visibility: private`` for a private event, the attendees' display
  names, and the cleaned description. ``EXDATE`` lines and occurrence dates are
  left out of the body because they change often, and a body change re-chunks,
  re-embeds and re-extracts. The ``Visibility`` line exists so that an event
  that turns private changes the body, and the re-ingest then goes through the
  pipeline's escalate-only sensitivity update.
- **Metadata**: ``date`` (promoted to ``sent_at``: the start, or for a series
  its latest occurrence in the window), ``participants`` (the attendees'
  addresses, lowercased; the field ``--person`` matches), and for a series
  ``occurrences`` (``count`` in the window and the ``last`` ten start times).
- **Never stored**: attendee addresses in the body (every address-shaped string
  is removed from the description and the location), ``conferenceData`` and
  ``hangoutLink``, conference-host URLs, Google's inserted invitation block, and
  dial-in, PIN and passcode lines (they carry meeting PINs).
- **Skipped**: cancelled events, events the owner declined, events with no
  title, and working-location and out-of-office entries.
"""
from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from brain.errors import DirectoryRefreshError

# ``_parse_gws_pages`` is private to ``directory.py``, but it is exactly the
# parser for this call's output (NDJSON pages under ``items``). Reusing it keeps
# the two readers of ``gws calendar events list`` from drifting apart, the same
# trade ``gmail.py`` makes for ``_strip_re_fwd_prefixes``.
from brain.vault.derived_links.directory import GwsRunner, _parse_gws_pages

from . import ExtractedDoc

__all__ = [
    "CONTENT_TYPE",
    "SOURCE_KIND",
    "CalendarBatch",
    "CalendarDoc",
    "CalendarError",
    "build_documents",
    "clean_description",
    "clean_location",
    "get_event",
    "list_events",
]

SOURCE_KIND = "calendar"
CONTENT_TYPE = "calendar_event"

#: Page size per ``events.list`` request, as ``refresh_calendar`` uses.
_MAX_RESULTS = 250
#: How many occurrence start times a series keeps in ``metadata.occurrences``.
_LAST_OCCURRENCES = 10
_SKIPPED_EVENT_TYPES = frozenset({"workingLocation", "outOfOffice"})
#: The failures a ``GwsRunner`` may raise (its Protocol docstring).
_RUNNER_FAILURES = (OSError, DirectoryRefreshError, RuntimeError)

_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# Any URL on a conference host: the link itself carries the meeting's PIN.
_CONFERENCE_URL = re.compile(
    r"\S*(?:zoom\.us|meet\.google\.com|teams\.microsoft\.com|teams\.live\.com|webex\.com|"
    r"gotomeeting\.com|goto\.com|chime\.aws|bluejeans\.com)\S*",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HTML_BREAK = re.compile(r"<\s*(?:br|/p|/div|/li|/h\d)\s*/?\s*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
# Lines that carry a dial-in, a PIN or a join instruction. Matched per line, and
# the whole line is dropped.
_DIAL_IN_LINE = re.compile(
    r"meeting id|passcode|password|\bpin\s*[:#]|dial[- ]?in|join by phone|"
    r"one tap mobile|phone numbers?|access code|conference id|\btel:|"
    r"^\s*join\b.*\b(?:meeting|meet|zoom|teams|webex|call)\b",
    re.IGNORECASE,
)
# A line that is only a phone number (with separators, commas and a `#`).
_PHONE_LINE = re.compile(r"^[\s+(]*\d[\d\s().,#*-]{6,}$")
_INVITATION_DELIMITER = "-::~"
_BLANK_RUN = re.compile(r"\n{3,}")
_SPACE_RUN = re.compile(r" {2,}")


class CalendarError(RuntimeError):
    """The ``gws calendar events list`` call failed, or its output could not be parsed."""


@dataclass(frozen=True)
class CalendarDoc:
    """One document to ingest: a single event, or one recurring series."""

    external_id: str
    doc: ExtractedDoc
    #: ``(display name, address)`` for each person attending, for the people
    #: directory that ``--person`` resolves through. Never written to the body.
    attendees: tuple[tuple[str | None, str], ...]
    confidential: bool


@dataclass(frozen=True)
class CalendarBatch:
    """The documents built from one window of events, and how many were skipped."""

    documents: tuple[CalendarDoc, ...]
    skipped: int


def list_events(
    calendar_id: str, time_min: datetime, time_max: datetime, *, runner: GwsRunner
) -> list[dict[str, Any]]:
    """Every event instance in ``[time_min, time_max)``, recurring series expanded."""
    params = json.dumps(
        {
            "calendarId": calendar_id,
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": True,
            "maxResults": _MAX_RESULTS,
        }
    )
    args = ["gws", "calendar", "events", "list", "--params", params]
    try:
        raw = runner([*args, "--format", "json", "--page-all"])
    except _RUNNER_FAILURES as exc:
        raise CalendarError(f"gws calendar events list failed: {exc}") from exc
    events = _parse_gws_pages(raw, key="items")
    if events is None:
        raise CalendarError("could not parse the gws calendar events list output")
    return [e for e in events if isinstance(e, dict)]


def get_event(calendar_id: str, event_id: str, *, runner: GwsRunner) -> dict[str, Any] | None:
    """The event resource for ``event_id`` (a series master), or ``None`` on any failure."""
    params = json.dumps({"calendarId": calendar_id, "eventId": event_id})
    try:
        raw = runner(
            ["gws", "calendar", "events", "get", "--params", params, "--format", "json"]
        )
        parsed = json.loads(raw)
    except (*_RUNNER_FAILURES, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def build_documents(
    events: Iterable[dict[str, Any]],
    *,
    master_lookup: Callable[[str], dict[str, Any] | None],
) -> CalendarBatch:
    """Group instances into series, apply the skip rules, and build each document."""
    singles: list[dict[str, Any]] = []
    series: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        series_id = event.get("recurringEventId")
        if isinstance(series_id, str) and series_id:
            series.setdefault(series_id, []).append(event)
        else:
            singles.append(event)

    documents: list[CalendarDoc] = []
    skipped = 0
    for event in singles:
        doc = _single_document(event)
        if doc is None:
            skipped += 1
        else:
            documents.append(doc)
    for series_id, instances in series.items():
        doc = _series_document(series_id, instances, master_lookup(series_id))
        if doc is None:
            skipped += 1
        else:
            documents.append(doc)
    return CalendarBatch(documents=tuple(documents), skipped=skipped)


def _single_document(event: dict[str, Any]) -> CalendarDoc | None:
    start = _start(event)
    if _skip(event) or start is None:
        return None
    return _document(
        str(event.get("id") or ""),
        event,
        when=_when(event),
        metadata={"date": start[0], "event_id": event.get("id")},
    )


def _series_document(
    series_id: str, instances: Sequence[dict[str, Any]], master: dict[str, Any] | None
) -> CalendarDoc | None:
    kept: list[tuple[tuple[str, datetime], dict[str, Any]]] = []
    for instance in instances:
        start = _start(instance)
        if start is not None and not _skip(instance):
            kept.append((start, instance))
    if not kept or (master is not None and _declined(master)):
        return None
    kept.sort(key=lambda pair: pair[0][1])
    # The master makes the body independent of the window; the first kept
    # instance stands in when it cannot be read or is not a usable event.
    base = kept[0][1]
    if master is not None and not _skip(master) and _start(master) is not None:
        base = master
    rules = [
        rule
        for rule in base.get("recurrence") or []
        if isinstance(rule, str) and rule.startswith("RRULE:")
    ]
    prefix = f"recurring, {', '.join(rules)}" if rules else "recurring series"
    starts = [start[0] for start, _instance in kept]
    return _document(
        series_id,
        base,
        when=f"{prefix}, {_time_of_day(base)}",
        metadata={
            "date": starts[-1],
            "series_id": series_id,
            "occurrences": {"count": len(starts), "last": starts[-_LAST_OCCURRENCES:]},
        },
    )


def _document(
    external_id: str, event: dict[str, Any], *, when: str, metadata: dict[str, Any]
) -> CalendarDoc:
    title = str(event.get("summary") or "").strip()
    attendees = _attendees(event)
    private = event.get("visibility") == "private"
    lines = [title, f"When: {when}"]
    location = clean_location(event.get("location"))
    if location:
        lines.append(f"Location: {location}")
    if private:
        lines.append("Visibility: private")
    names = [name for name, _email in attendees if name]
    if names:
        lines.append(f"Attendees: {', '.join(names)}")
    description = clean_description(event.get("description"))
    if description:
        lines += ["", description]
    participants = {email for _name, email in attendees}
    organizer = (event.get("organizer") or {}).get("email")
    if isinstance(organizer, str) and "@" in organizer:
        participants.add(organizer.strip().lower())
    doc = ExtractedDoc(
        title=title,
        content="\n".join(lines),
        content_type=CONTENT_TYPE,
        source_path=None,
        metadata={**metadata, "participants": sorted(participants)},
    )
    return CalendarDoc(
        external_id=external_id, doc=doc, attendees=attendees, confidential=private
    )


def _skip(event: dict[str, Any]) -> bool:
    return (
        event.get("status") == "cancelled"
        or event.get("eventType") in _SKIPPED_EVENT_TYPES
        or not str(event.get("summary") or "").strip()
        or _declined(event)
    )


def _declined(event: dict[str, Any]) -> bool:
    return any(
        isinstance(a, dict) and a.get("self") is True and a.get("responseStatus") == "declined"
        for a in event.get("attendees") or []
    )


def _attendees(event: dict[str, Any]) -> tuple[tuple[str | None, str], ...]:
    """``(display name, address)`` per person, in invitation order; rooms left out."""
    out: list[tuple[str | None, str]] = []
    for a in event.get("attendees") or []:
        if not isinstance(a, dict) or a.get("resource") is True:
            continue
        email = a.get("email")
        if not isinstance(email, str) or "@" not in email:
            continue
        name = a.get("displayName")
        display = name.strip() if isinstance(name, str) and "@" not in name else None
        out.append((display or None, email.strip().lower()))
    return tuple(out)


def _start(event: dict[str, Any]) -> tuple[str, datetime] | None:
    """The start as stored in metadata (ISO) and as a UTC datetime; all-day at UTC midnight."""
    when = event.get("start") or {}
    raw = when.get("dateTime")
    if isinstance(raw, str):
        try:
            return raw, _aware(datetime.fromisoformat(raw))
        except ValueError:
            return None
    day = when.get("date")
    if isinstance(day, str):
        try:
            midnight = datetime.combine(date.fromisoformat(day), datetime.min.time(), UTC)
        except ValueError:
            return None
        return midnight.isoformat(), midnight
    return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _when(event: dict[str, Any]) -> str:
    """``2026-10-06 10:00–10:45 (UTC-07:00)``, or a date range ``(all day)``."""
    start, end = event.get("start") or {}, event.get("end") or {}
    if "date" in start:
        first = date.fromisoformat(start["date"])
        last = _all_day_last(end, first)
        days = first.isoformat() if last == first else f"{first} – {last}"
        return f"{days} (all day)"
    begin = _aware(datetime.fromisoformat(start["dateTime"]))
    finish = _end_datetime(end, begin)
    if finish.date() == begin.date():
        return f"{begin:%Y-%m-%d %H:%M}–{finish:%H:%M} ({_offset(begin)})"
    return f"{begin:%Y-%m-%d %H:%M} – {finish:%Y-%m-%d %H:%M} ({_offset(begin)})"


def _time_of_day(event: dict[str, Any]) -> str:
    """``09:00–09:30 (UTC-07:00)`` for a series, with no date; ``(all day)``."""
    start = event.get("start") or {}
    if "date" in start:
        return "(all day)"
    begin = _aware(datetime.fromisoformat(start["dateTime"]))
    finish = _end_datetime(event.get("end") or {}, begin)
    return f"{begin:%H:%M}–{finish:%H:%M} ({_offset(begin)})"


def _end_datetime(end: dict[str, Any], begin: datetime) -> datetime:
    raw = end.get("dateTime")
    if not isinstance(raw, str):
        return begin
    try:
        return _aware(datetime.fromisoformat(raw)).astimezone(begin.tzinfo)
    except ValueError:
        return begin


def _all_day_last(end: dict[str, Any], first: date) -> date:
    """The last day of an all-day event: Calendar's ``end.date`` is exclusive."""
    try:
        return max(first, date.fromisoformat(str(end.get("date"))) - timedelta(days=1))
    except ValueError:
        return first


def _offset(moment: datetime) -> str:
    delta = moment.utcoffset() or timedelta(0)
    if not delta:
        return "UTC"
    sign = "-" if delta < timedelta(0) else "+"
    minutes = abs(int(delta.total_seconds())) // 60
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def clean_location(location: Any) -> str:
    """The location with URLs and addresses removed, whitespace collapsed."""
    if not isinstance(location, str):
        return ""
    text = _EMAIL.sub("", _URL.sub("", location))
    return " ".join(text.split()).strip(" ,;")


def clean_description(description: Any) -> str:
    """The description as plain text, with conference and dial-in content dropped.

    HTML is turned into text; Google's inserted invitation block (between two
    ``-::~`` delimiter lines, or from one to the end) is dropped; so are lines
    that carry a dial-in, a PIN or a join instruction, lines that are only a
    phone number, every URL on a conference host (the line's other text stays),
    and every address-shaped string.
    """
    if not isinstance(description, str):
        return ""
    text = html.unescape(_HTML_TAG.sub("", _HTML_BREAK.sub("\n", description)))
    kept: list[str] = []
    in_invitation = False
    for line in text.splitlines():
        if line.strip().startswith(_INVITATION_DELIMITER):
            in_invitation = not in_invitation
            continue
        if in_invitation or _DIAL_IN_LINE.search(line) or _PHONE_LINE.match(line):
            continue
        cleaned, removed = _EMAIL.subn("", _CONFERENCE_URL.sub("", line))
        if removed:
            cleaned = _SPACE_RUN.sub(" ", cleaned)
        if cleaned.strip() or not line.strip():
            kept.append(cleaned.rstrip())
    return _BLANK_RUN.sub("\n\n", "\n".join(kept)).strip()

