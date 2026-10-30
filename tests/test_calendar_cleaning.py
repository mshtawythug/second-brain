"""Adversarial cleaning of calendar text (RM-30 security round).

Every string here is synthetic and is placed in each free-text field of an event:
the title, the location, the description and an attendee's display name. The
document built from it must keep no URL, no address, no phone-like digit run and
no dial-in keyword, in any field. Everything goes through the public
``build_documents``, so the test exercises the fields as they are stored.
"""
from __future__ import annotations

import re
from typing import Any

import pytest

from brain.ingest import calendar as cal
from tests import calendar_fixtures as fx

PLACEHOLDER = "(calendar event)"

ADVERSARIAL = [
    # titles that carried a join link, an address, a phone and a PIN
    "Sync https://example.zoom.us/j/5550100?pwd=SECRETPWD avery@example.com +1 555 010 0199 PIN 77",
    # Zoom one-tap and Teams dial-in lines
    "+16695550100,,85512345678#,,,,*123456# US (San Jose)",
    "+1 323-555-0100,,123456789#   United States, Los Angeles",
    # keyword lines
    "Meeting-ID: 123",
    "PIN 4321",
    "Code: 998877",
    "Bridge: 1-800-555-0100 x 4433",
    "Passwort: geheim",
    "Kennwort: geheim",
    "Passcode 4242",
    "Conference ID: 884",
    "Access code 7781",
    "Dial-in: see below",
    "One tap mobile",
    "Join by phone",
    # scheme-less join links, tel: and mixed dial-ins (the location leaks)
    "zoom.us/j/5550100?pwd=XYZ",
    "meet.google.com/abc-defg-hij",
    "tel:+15550101234",
    "+1 555 123 4567,,998877#",
    # caught by the one-tap rule alone (no keyword, under 7 digits)
    "Then press ,,4242#",
    "Meeting ID: 123 Passcode: 456",
    # conference hosts the first list missed, and ordinary links
    "https://example.zoomgov.com/j/1612345",
    "teams.microsoft.us/l/meetup-join/xyz",
    "meet.jit.si/SampleRoom",
    "whereby.com/sample-room",
    "https://docs.example.com/d/abc/edit?usp=sharing",
    "https://forms.example.com/?email=bob%40example.com",
    "www.example.com/agenda",
    # caught by the www. rule alone (no scheme, no path)
    "see www.example.com",
    "mailto:bob@example.com",
    # addresses: full-width, obfuscated, no dot
    "ｂｏｂ＠example.com",
    "bob [at] example [dot] com",
    "bob(at)example.com",
    "bob at example dot com",
    "bob@localhost",
]

_KEYWORDS = re.compile(
    r"\bpin\b|pass\s*-?(?:code|word|wort)|kennwort|meeting\s*-?\s*id|conference\s*-?\s*id|"
    r"access\s*code|\bcode\s*[:#]|\bbridge\b|dial\s*-?\s*in|one\s*tap|join by phone|"
    r"\btel:|mailto:",
    re.IGNORECASE,
)


def _assert_clean(text: str) -> None:
    assert not re.search(r"\d{3,}", text), text  # no digit run
    assert "@" not in text and "＠" not in text, text  # no address
    assert not re.search(r"\[at\]|\(at\)|\bat example\b", text, re.IGNORECASE), text
    assert not re.search(r"://|www\.|\b[\w-]+\.[a-z]{2,}[/?]", text, re.IGNORECASE), text
    assert not _KEYWORDS.search(text), text


def _event_with(text: str, *, description: str | None = None) -> dict[str, Any]:
    return fx.event(
        "evtadversarial001",
        text,
        when=fx.timed("2026-10-06T10:00:00-07:00", "2026-10-06T10:30:00-07:00"),
        attendees=[fx.owner(), {"displayName": text, "email": fx.AVERY[1]}],
        location=text,
        description=(
            description if description is not None else f"Agenda first.\n{text}\nAgenda last."
        ),
    )


def _only_doc(event: dict[str, Any]) -> cal.CalendarDoc:
    batch = cal.build_documents([event], master_lookup=lambda sid: None)
    assert len(batch.documents) == 1
    return batch.documents[0]


@pytest.mark.parametrize("text", ADVERSARIAL)
def test_no_field_keeps_a_link_address_digit_run_or_keyword(text: str) -> None:
    doc = _only_doc(_event_with(text))

    _assert_clean(doc.doc.title)
    body = doc.doc.content
    when_line = next(line for line in body.splitlines() if line.startswith("When:"))
    _assert_clean(body.replace(when_line, ""))  # the When line is ours, not theirs
    for name, _email in doc.attendees:
        _assert_clean(name or "")
    assert "Agenda first." in body and "Agenda last." in body


def test_a_title_the_cleaner_empties_gets_the_placeholder_not_a_skip() -> None:
    doc = _only_doc(_event_with("PIN 4321"))

    assert doc.doc.title == PLACEHOLDER
    assert doc.doc.content.splitlines()[0] == PLACEHOLDER


def test_a_title_keeps_its_words_around_a_removed_link() -> None:
    event = _event_with("Weekly sync https://example.zoom.us/j/5550100?pwd=SECRETPWD")

    assert _only_doc(event).doc.title == "Weekly sync"


def test_a_display_name_the_cleaner_empties_is_dropped() -> None:
    doc = _only_doc(_event_with("bob at example dot com"))

    assert doc.attendees[1] == (None, fx.AVERY[1])
    attendees_line = next(
        line for line in doc.doc.content.splitlines() if line.startswith("Attendees:")
    )
    assert attendees_line == "Attendees: Owner Sample"


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("Notes: https://docs.example.com/d/abc/edit?usp=sharing", "Notes"),
        ("Contact ｂｏｂ＠example.com for the deck", "Contact for the deck"),
        ("Lunch at Cafe.Bistro afterwards", "Lunch at Cafe.Bistro afterwards"),
        ("Code review of the parser", "Code review of the parser"),
        ("Deadline 2026-10-06 at 10:30", "Deadline 2026-10-06 at 10:30"),
        ("Room 4, floor 2", "Room 4, floor 2"),
        ("- first item", "- first item"),
    ],
)
def test_ordinary_text_survives(text: str, kept: str) -> None:
    doc = _only_doc(_event_with("Planning", description=text))

    assert kept in doc.doc.content.splitlines()


@pytest.mark.parametrize("visibility", ["private", "confidential"])
def test_private_and_legacy_confidential_visibility_are_confidential(visibility: str) -> None:
    doc = _only_doc(fx.event(
        fx.PRIVATE_ID,
        fx.PRIVATE_TITLE,
        when=fx.timed("2026-10-08T15:00:00-07:00", "2026-10-08T16:00:00-07:00"),
        visibility=visibility,
    ))

    assert doc.confidential is True
    assert "Visibility: private" in doc.doc.content


@pytest.mark.parametrize("visibility", ["default", "public", None])
def test_other_visibilities_are_not_confidential(visibility: str | None) -> None:
    extra = {} if visibility is None else {"visibility": visibility}
    doc = _only_doc(fx.event(
        fx.SINGLE_ID,
        fx.SINGLE_TITLE,
        when=fx.timed("2026-10-08T15:00:00-07:00", "2026-10-08T16:00:00-07:00"),
        **extra,
    ))

    assert doc.confidential is False
    assert "Visibility" not in doc.doc.content


def test_the_invitation_block_is_dropped_even_when_unterminated() -> None:
    text = "Keep this.\n-::~:~::~:~::-\nJoin: https://meet.google.com/x\nPIN: 1#"

    doc = _only_doc(_event_with("Planning", description=text))

    assert doc.doc.content.endswith("Keep this.")
