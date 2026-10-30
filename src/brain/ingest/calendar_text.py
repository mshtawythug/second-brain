"""The one text cleaner for calendar events: title, location, description, names (RM-30).

Calendar text is mostly written by other people, and the parts a search needs
(what the meeting is about) are mixed with parts that must never be stored:
join links that carry a passcode in their query string, dial-in numbers with a
PIN, and addresses. Every free-text field of an event goes through this one
cleaner, so the rules cannot drift between fields.

Per line, in this order:

1. **NFKC normalisation**, so full-width forms (``＠``, ``：``, full-width digits)
   meet the patterns below as their ASCII equivalents.
2. **Every URL is removed**: with a scheme, ``www.``, scheme-less
   ``host.tld/path`` or ``host.tld?query``, and ``mailto:`` links. This runs
   before step 3, so the digits inside a join link do not drop the words
   around it ("Weekly sync <link>" keeps "Weekly sync").
3. **The whole line is dropped** when what is left looks like dial-in material:
   - a keyword: PIN, passcode, password, Passwort, Kennwort, code, meeting ID,
     conference ID, access code, bridge, dial-in, "join by phone", "one tap",
     a join instruction ("Join Zoom Meeting", "Join with Google Meet"), or a
     ``tel:`` / ``sip:`` link. Hyphens and spaces inside a keyword are
     optional. The short words ``pin``, ``code``, ``bridge`` and ``id`` count
     only when a ``:``, ``#``, ``=`` or a digit follows, so "code review"
     survives;
   - a phone-like run of 7 or more digits, separators allowed. Dates
     (``2026-10-06``) and times (``10:30``) are ignored by this check;
   - the ``,,digits`` / ``digits#`` forms of a one-tap number.
4. **Every email address is removed**, including ``user@host`` with no dot
   and the obfuscated ``user [at] host [dot] tld`` / ``user (at) host.tld`` /
   ``user at host dot tld`` forms.

A line left empty is dropped. Runs of spaces are collapsed.
"""
from __future__ import annotations

import html
import re
import unicodedata

__all__ = ["clean_description", "clean_line"]

_KEYWORD = re.compile(
    r"pass[\s-]*(?:code|word|wort)|kenn[\s-]*wort|"
    r"meeting[\s-]*(?:id|number|code)|conference[\s-]*(?:id|code)|access[\s-]*code|"
    r"dial[\s-]*in|join\s+by\s+phone|one[\s-]*tap|\b(?:tel|sip|callto):|"
    r"^\s*join\b.*\b(?:meeting|meet|zoom|teams|webex|call)\b|"
    r"\b(?:pin|code|bridge|id)\b\s*(?:[:#=]|\d)",
    re.IGNORECASE,
)
# Masked before the digit-run check only: a date or a time is not a phone number.
_DATE_OR_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}:\d{2}\b")
_DIGIT_RUN = re.compile(r"\d(?:[\s().\-/x+]*\d){6,}", re.IGNORECASE)
_ONE_TAP = re.compile(r",,\s*\d|\d{3,}\s*#")

_URL = re.compile(
    r"\b[a-z][a-z0-9+.-]*://\S*|\bwww\.\S+|\bmailto:\S+|"
    r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?[/?]\S*",
    re.IGNORECASE,
)
_AT = r"(?:\s*(?:\[at\]|\(at\)|\{at\})\s*)"
_DOT = r"(?:\s*(?:\[dot\]|\(dot\)|\{dot\})\s*|\s+dot\s+)"
_EMAIL = re.compile(
    # a plain or no-dot address
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*"
    # user [at] host, with real or spelled dots
    rf"|[\w.+-]+{_AT}[\w-]+(?:(?:{_DOT}|\.)[\w-]+)*"
    # user at host dot tld: the plain-word form needs a spelled dot
    rf"|[\w.+-]+\s+at\s+[\w-]+(?:{_DOT}[\w-]+)+",
    re.IGNORECASE,
)
_HTML_BREAK = re.compile(r"<\s*(?:br|/p|/div|/li|/h\d)\s*/?\s*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t]{2,}")
_BLANK_RUN = re.compile(r"\n{3,}")
_INVITATION_DELIMITER = "-::~"


def clean_line(text: object) -> str:
    """One line of calendar text, cleaned; ``""`` when nothing safe is left.

    Used for the title, the location and each display name. Embedded newlines
    are cleaned line by line and joined with a space.
    """
    if not isinstance(text, str):
        return ""
    kept = (_clean(line) for line in text.splitlines())
    return " ".join(part for part in kept if part)


def clean_description(description: object) -> str:
    """The description as plain text, cleaned line by line.

    HTML is turned into text first, and Google's inserted invitation block
    (between two ``-::~`` delimiter lines, or from one to the end) is dropped
    whole. Blank lines between paragraphs are kept, at most one in a row.
    """
    if not isinstance(description, str):
        return ""
    text = html.unescape(_HTML_TAG.sub("", _HTML_BREAK.sub("\n", description)))
    kept: list[str] = []
    in_invitation = False
    for line in unicodedata.normalize("NFKC", text).splitlines():
        if line.strip().startswith(_INVITATION_DELIMITER):
            in_invitation = not in_invitation
            continue
        if in_invitation:
            continue
        cleaned = _clean(line)
        if cleaned or not line.strip():
            kept.append(cleaned)
    return _BLANK_RUN.sub("\n\n", "\n".join(kept)).strip()


def _clean(line: str) -> str:
    without_links = _URL.sub(" ", unicodedata.normalize("NFKC", line))
    if _is_dial_in(without_links):
        return ""
    stripped = _SPACES.sub(" ", _EMAIL.sub(" ", without_links)).strip()
    # Trailing separators only ("Notes doc: <link>" -> "Notes doc"), so a
    # leading list marker survives.
    return stripped.rstrip(",;:|-").rstrip()


def _is_dial_in(line: str) -> bool:
    if _KEYWORD.search(line) or _ONE_TAP.search(line):
        return True
    return _DIGIT_RUN.search(_DATE_OR_TIME.sub(" ", line)) is not None
