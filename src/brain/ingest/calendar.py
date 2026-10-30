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
  master's ``RRULE`` lines and its time of day), the location,
  ``Visibility: private`` for a private event (Google's ``private`` or legacy
  ``confidential`` visibility), the attendees' display names, and the
  description. Every free-text field (title, location, description, display
  names) goes through the one cleaner in :mod:`brain.ingest.calendar_text`,
  which removes every URL and address and drops dial-in lines. ``EXDATE``
  lines and occurrence dates are left out of the body because they change
  often, and a body change re-chunks, re-embeds and re-extracts. The
  ``Visibility`` line exists so that an event that turns private changes the
  body, and the re-ingest then goes through the pipeline's escalate-only
  sensitivity update.
- **Metadata**: ``date`` (promoted to ``sent_at``: the start, or for a series
  its latest known occurrence), ``participants`` (the attendees' addresses,
  lowercased; the field ``--person`` matches), and for a series
  ``occurrences``: ``last``, the latest ten start times known, and
  ``run_window_count``, how many occurrences the most recent run's window held.
  :func:`merge_volatile` merges a run into what is stored, so an earlier window
  never moves a series' ``date`` or ``last`` backwards.
- **Series without a readable master.** :attr:`CalendarDoc.body_from_fallback`
  marks a series whose body came from an instance; for a document that already
  exists, the command then keeps the stored body and refreshes only the
  metadata, so one failed ``events get`` does not rewrite (re-chunk, re-embed)
  the body. :attr:`CalendarBatch.fallbacks` counts them.
- **Attendees** count only with an address ``is_email_like`` accepts (one
  ``@``, a dotted domain, no spaces); resource rooms are left out.
- **Never stored**: attendee addresses in the body, any URL (join links carry
  passcodes in their query strings), ``conferenceData`` and ``hangoutLink``,
  Google's inserted invitation block, and dial-in, PIN and passcode lines.
- **Skipped**: cancelled events, events the owner declined, events with no
  title, and working-location and out-of-office entries. A title that the
  cleaner empties (it held only a link or a dial-in) is NOT a reason to skip:
  the event existed and has attendees and a time, so it is stored under
  :data:`PLACEHOLDER_TITLE`.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from brain.errors import DirectoryRefreshError, describe_failure

# ``_parse_gws_pages`` is private to ``directory.py``, but it is exactly the
# parser for this call's output (NDJSON pages under ``items``). Reusing it keeps
# the two readers of ``gws calendar events list`` from drifting apart, the same
# trade ``gmail.py`` makes for ``_strip_re_fwd_prefixes``.
from brain.vault.derived_links.directory import GwsRunner, _parse_gws_pages
from brain.vault.derived_links.participants import is_email_like

from . import ExtractedDoc
from .calendar_text import clean_description, clean_line

__all__ = [
    "CONTENT_TYPE",
    "PLACEHOLDER_TITLE",
    "SOURCE_KIND",
    "CalendarBatch",
    "CalendarDoc",
    "CalendarError",
    "build_documents",
    "merge_volatile",
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

#: Google's ``private`` and its legacy ``confidential`` both mean "only me".
_PRIVATE_VISIBILITIES = frozenset({"private", "confidential"})
#: The title of an event whose own title held nothing the cleaner keeps.
PLACEHOLDER_TITLE = "(calendar event)"


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
    #: A series whose master could not be read or used: its body came from its
    #: first instance in the window (module docstring).
    body_from_fallback: bool = False


@dataclass(frozen=True)
class CalendarBatch:
    """The documents built from one window of events, and how many were skipped."""

    documents: tuple[CalendarDoc, ...]
    skipped: int
    #: Series whose body came from an instance, not the master.
    fallbacks: int = 0


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
        # Class only: the runner's message can quote gws's stderr (account data).
        raise CalendarError(
            f"gws calendar events list failed ({describe_failure(exc)})"
        ) from exc
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
    return CalendarBatch(
        documents=tuple(documents),
        skipped=skipped,
        fallbacks=sum(doc.body_from_fallback for doc in documents),
    )


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
    from_master = master is not None and not _skip(master) and _start(master) is not None
    if from_master and master is not None:
        base = master
    rules = [
        rule
        for rule in base.get("recurrence") or []
        if isinstance(rule, str) and rule.startswith("RRULE:")
    ]
    prefix = f"recurring, {', '.join(rules)}" if rules else "recurring series"
    starts = [start[0] for start, _instance in kept]
    doc = _document(
        series_id,
        base,
        when=f"{prefix}, {_time_of_day(base)}",
        metadata={
            "date": starts[-1],
            "series_id": series_id,
            "occurrences": {
                "run_window_count": len(starts),
                "last": starts[-_LAST_OCCURRENCES:],
            },
        },
    )
    return replace(doc, body_from_fallback=not from_master)


def merge_volatile(stored: Mapping[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """``incoming`` metadata merged with what a series' stored document already knows.

    A run over an earlier window must not move a series backwards, so for a
    series ``occurrences.last`` becomes the sorted union of the stored and the
    incoming start times, trimmed to the latest ten, and ``date`` is the latest
    of them. ``run_window_count`` stays the incoming run's own count. A single
    event is returned unchanged: its ``date`` is its start, and a rescheduled
    event's start may legitimately move earlier.
    """
    occurrences = incoming.get("occurrences")
    if not isinstance(occurrences, dict):
        return incoming
    previous = stored.get("occurrences")
    stored_last = previous.get("last") if isinstance(previous, dict) else None
    parsed = {
        start: moment
        for start in [*(stored_last or []), *occurrences["last"]]
        if (moment := _parse_stored_start(start)) is not None
    }
    last = sorted(parsed, key=parsed.__getitem__)[-_LAST_OCCURRENCES:]
    return {**incoming, "date": last[-1], "occurrences": {**occurrences, "last": last}}


def _parse_stored_start(start: object) -> datetime | None:
    """A start time in ``_start``'s ISO form as a UTC datetime; ``None`` if it is not one.

    A stored value that does not parse (hand-edited metadata) is dropped from the
    merge rather than ordered arbitrarily.
    """
    if not isinstance(start, str):
        return None
    try:
        return _aware(datetime.fromisoformat(start))
    except ValueError:
        return None


def _document(
    external_id: str, event: dict[str, Any], *, when: str, metadata: dict[str, Any]
) -> CalendarDoc:
    title = clean_line(event.get("summary")) or PLACEHOLDER_TITLE
    attendees = _attendees(event)
    private = event.get("visibility") in _PRIVATE_VISIBILITIES
    lines = [title, f"When: {when}"]
    location = clean_line(event.get("location"))
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
    if isinstance(organizer, str) and is_email_like(organizer.strip()):
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
        if not isinstance(email, str) or not is_email_like(email.strip()):
            continue
        out.append((clean_line(a.get("displayName")) or None, email.strip().lower()))
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
