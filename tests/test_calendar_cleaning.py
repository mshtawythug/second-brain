"""Adversarial cleaning of calendar text (RM-30 security round).

Every string here is synthetic and is placed in each free-text field of an event:
the title, the location, the description and an attendee's display name. The
document built from it must keep no URL, no address, no phone-like digit run and
no dial-in keyword, in any field. Everything goes through the public
``build_documents``, so the test exercises the fields as they are stored.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any

import pytest

from brain.ingest import calendar as cal
from brain.ingest.calendar_text import clean_description, clean_line, clean_title
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
    # caught by the digit-run rule alone (no keyword, no one-tap form)
    "+1 323 555 0100 (US toll free)",
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
    # caught by the mailto: rule alone (the email rule cannot read %40)
    "mailto:bob%40example.com?subject=Hi",
    # addresses: full-width, obfuscated, no dot
    "ｂｏｂ＠example.com",
    "bob [at] example [dot] com",
    "bob(at)example.com",
    "bob at example dot com",
    "bob@localhost",
    # sec-96 re-check: format characters, Unicode dashes, %40, spaces, entities
    "zoom\u200b.us/j/1?pwd=x",
    "alice\u200b@example.com",
    "+1\u2011555\u2011123\u20114567",
    "alice%40example.com",
    "alice @ example . com",
    "alice&#64;example.com",
    # sec-96 re-check: pwd / pw with a value, and Kennung
    "pwd: hunter2",
    "pw 1234",
    "Kennung: 998",
]

_KEYWORDS = re.compile(
    r"\bpin\b|pass\s*-?(?:code|word|wort)|kennwort|kennung|meeting\s*-?\s*id|"
    r"conference\s*-?\s*id|access\s*code|\bcode\s*[:#]|\bbridge\b|dial\s*-?\s*in|one\s*tap|"
    r"join by phone|\bpwd\b|\bpw\b|\btel:|mailto:",
    re.IGNORECASE,
)
# A title may keep a keyword with no value next to it ("Password manager
# rollout"); it must not keep a keyword WITH a value.
_KEYWORD_WITH_VALUE = re.compile(rf"(?:{_KEYWORDS.pattern})\s*(?:[:#=]|\d)", re.IGNORECASE)


def _assert_no_secret(text: str) -> None:
    assert not re.search(r"\d{3,}", text), text  # no digit run
    assert "@" not in text and "＠" not in text and "%40" not in text, text  # no address
    # Every address and link in the table is on an example domain, so this also
    # catches one that survives encoded (an HTML entity for "@").
    assert "example" not in text.lower(), text
    assert not re.search(r"\[at\]|\(at\)|\bat example\b", text, re.IGNORECASE), text
    assert not re.search(r"://|www\.|\b[\w-]+\.[a-z]{2,}[/?]", text, re.IGNORECASE), text
    assert not _KEYWORD_WITH_VALUE.search(text), text
    assert not any(unicodedata.category(ch) == "Cf" for ch in text), text


def _assert_clean(text: str) -> None:
    _assert_no_secret(text)
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

    _assert_no_secret(doc.doc.title)  # title mode: spans removed, see clean_title
    title_line, when_line, *rest = doc.doc.content.splitlines()
    assert title_line == doc.doc.title
    assert when_line.startswith("When:")  # the When line is ours, not theirs
    body = "\n".join(rest)
    _assert_clean(body)
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


@pytest.mark.parametrize(
    "line",
    ["Join Zoom Meeting", "Join with Google Meet", "join the Teams call", "Join Webex meeting"],
)
def test_a_join_instruction_line_is_dropped(line: str) -> None:
    doc = _only_doc(_event_with("Planning", description=f"Agenda first.\n{line}\nAgenda last."))

    assert line not in doc.doc.content
    assert "Agenda first." in doc.doc.content and "Agenda last." in doc.doc.content


def test_a_line_that_only_mentions_joining_survives() -> None:
    doc = _only_doc(_event_with("Planning", description="Join us for cake afterwards"))

    assert "Join us for cake afterwards" in doc.doc.content


# --- titles keep their words (rev-96 re-review) ----------------------------------

ORDINARY_TITLES = [
    ("Team offsite 10/06/2026 - 10/08/2026", "Team offsite 10/06/2026 - 10/08/2026"),
    ("Release 2026.10.06 go/no-go", "Release 2026.10.06 go/no-go"),
    ("Planning 6.10.26 to 2026/10/30", "Planning 6.10.26 to 2026/10/30"),
    ("Q4 budget: 1 250 000 USD", "Q4 budget: USD"),
    ("Password manager rollout", "Password manager rollout"),
    ("Join the teams offsite planning", "Join the teams offsite planning"),
    ("Project ID: ENG-123 kickoff", "Project"),
    ("Interview round 2 (ID 4471)", "Interview round 2"),
    ("Code review: parser", "Code review: parser"),
    ("1:1 with Avery at 10:30", "1:1 with Avery at 10:30"),
    ("C# guild sync", "C# guild sync"),
    ("Weekly sync PIN 4321", "Weekly sync"),
    ("Standup, dial-in: +1 555 010 0199", "Standup"),
    # what a removed dial-in leaves behind is tidied away
    ("Sync (PIN 4321) weekly", "Sync"),
    # a credential keyword with no value stays (sec-96 T1 allowlist)
    ("Access code rollout", "Access code rollout"),
    ("Reset password", "Reset password"),
    ("Meeting ID card", "Meeting ID card"),
    # short words without a value, and words that merely contain one
    ("ID card renewal", "ID card renewal"),
    ("Covid 19 update", "Covid 19 update"),
    ("pinned agenda and codebase tour", "pinned agenda and codebase tour"),
    ("Call +1 323-555-0100,,123456789# US", "Call US"),
    ("Call (+1 323 555 0100) team", "Call team"),
]


@pytest.mark.parametrize(("title", "kept"), ORDINARY_TITLES)
def test_an_ordinary_title_keeps_its_words(title: str, kept: str) -> None:
    doc = _only_doc(_event_with(title, description="Agenda."))

    assert doc.doc.title == kept


def test_a_title_keeps_words_a_description_line_would_lose() -> None:
    """Title mode removes spans; the same text as a description line goes whole."""
    doc = _only_doc(_event_with("Project ID: ENG-123 kickoff", description="Agenda."))

    assert doc.doc.title == "Project"
    assert "Location" not in doc.doc.content  # the location is line mode: dropped


@pytest.mark.parametrize(
    "line",
    [
        "Discuss the access code rollout",
        "password reset policy discussion",
        "Meeting ID card",
        "Year 2026 2027 2028 planning",
    ],
)
def test_a_description_line_is_over_removed_by_design(line: str) -> None:
    """S6, the documented trade: privacy over recall outside a title."""
    doc = _only_doc(_event_with("Planning", description=f"Keep.\n{line}"))

    assert line not in doc.doc.content
    assert doc.doc.content.endswith("Keep.")


# --- title truncation (sec-96 T1-T4) ---------------------------------------------

CREDENTIAL_PROBES = [
    ("Sync - passcode abcXYZ", "abcXYZ"),
    ("Weekly sync, passcode is 7Hk2pQ", "7Hk2pQ"),
    ("access code ABCD-EFGH", "ABCD-EFGH"),
    ("Kennwort geheim", "geheim"),
    ("passcode: abc XYZ 123", "XYZ"),
    ("passcode=a b", "=a"),
    ("Team PIN-4321", "4321"),
    ("Team PIN4321", "4321"),
    ("Team P.I.N. 4321", "4321"),
    ("Team code is 4321", "4321"),
    ("Team code \u2014 4321", "4321"),
    ("Team code -> 4321", "4321"),
    ("Sync passcode\n4321", "4321"),
    ("Call skype:live:samplename", "samplename"),
    ("pwd hunter2 for the share", "hunter2"),
]


@pytest.mark.parametrize(("text", "secret"), CREDENTIAL_PROBES)
def test_a_credential_value_survives_in_no_field(text: str, secret: str) -> None:
    doc = _only_doc(_event_with(text))

    assert secret not in doc.doc.title
    assert secret not in doc.doc.content
    assert all(secret not in (name or "") for name, _email in doc.attendees)
    _assert_no_secret(doc.doc.title)


@pytest.mark.parametrize(
    ("title", "kept"),
    [
        ("Sync - passcode abcXYZ", "Sync"),
        ("Weekly sync, passcode is 7Hk2pQ", "Weekly sync"),
        ("Team PIN4321", "Team"),
        ("Sync passcode\n4321", "Sync"),
        ("Call skype:live:samplename", "Call"),
    ],
)
def test_a_title_keeps_the_words_before_a_credential(title: str, kept: str) -> None:
    assert _only_doc(_event_with(title, description="Agenda.")).doc.title == kept


def test_a_bare_value_on_the_line_after_a_dial_in_line_is_dropped() -> None:
    text = "Passcode:\n424242\nNext item here\nMeeting ID\n884 112\n\nAfter a blank line"

    body = _only_doc(_event_with("Planning", description=text)).doc.content

    assert "424242" not in body and "884" not in body
    assert "Next item here" in body and "After a blank line" in body


def test_a_longer_line_after_a_dial_in_line_is_kept() -> None:
    text = "Discuss the access code rollout\nNext topic: budget planning"

    body = _only_doc(_event_with("Planning", description=text)).doc.content

    assert "Next topic: budget planning" in body


# --- the date mask cannot hide a phone number (rev-96 HIGH at 6402da9) --------------

PHONES_THAT_LOOK_LIKE_DATES = [
    ("France: 01.70.37.22.46", "70.37"),
    ("+33 1.70.37.22.46", "70.37"),
    ("+49 30 12-34-5678", "5678"),
    ("Italy 02-87-33-62-39", "87-33"),
    ("Germany 069 12-34-56", "12-34"),
    ("2026.10.06 and 01.70.37.22.46", "70.37"),
    ("Line 12:99 12345", "12345"),  # not a valid time, so not masked
    ("Tel 9123.12.10", "9123"),  # a valid date slice with a digit glued before
    ("Tel 12.10.26.5555", "5555"),  # ... and with digits glued after
    ("Fax 01.12-26 3344", "3344"),  # mixed separators: not one date
]
DATES_THAT_STAY = [
    "Team offsite 10/06/2026 - 10/08/2026",
    "Release 2026.10.06 go/no-go",
    "Planning 6.10.26 to 2026/10/30",
    "Due 10/30/2026",  # M/D/Y: only the month-first reading is a date
    "Due 30.10.2026",  # D.M.Y: only the day-first reading is a date
    "Kickoff 31.12.00 4455",  # a two-digit year, then a room number
    "Planning 2026-2028",  # a year range
    "Deadline 2026-10-06 at 10:30",
]


@pytest.mark.parametrize(("text", "phone"), PHONES_THAT_LOOK_LIKE_DATES)
@pytest.mark.parametrize("clean", [clean_line, clean_title, clean_description])
def test_a_phone_number_shaped_like_a_date_is_removed(
    clean: Callable[[object], str], text: str, phone: str
) -> None:
    assert phone not in clean(text)


def test_a_phone_line_between_description_lines_is_dropped() -> None:
    assert clean_description("Agenda\nFrance: 01.70.37.22.46\nNotes") == "Agenda\nNotes"


@pytest.mark.parametrize("text", DATES_THAT_STAY)
@pytest.mark.parametrize("clean", [clean_line, clean_title, clean_description])
def test_a_date_is_not_a_phone_number(clean: Callable[[object], str], text: str) -> None:
    assert clean(text) == text
