"""The one text cleaner for calendar events: title, location, description, names (RM-30).

Calendar text is mostly written by other people, and the parts a search needs
(what the meeting is about) are mixed with parts that must never be stored:
join links that carry a passcode in their query string, dial-in numbers with a
PIN, and addresses. Every free-text field of an event goes through this one
cleaner, so the rules cannot drift between fields.

Per line, in this order:

1. **Normalisation**: HTML entities and ``%40`` are decoded, NFKC folds
   full-width forms (``＠``, ``：``, full-width digits) to ASCII, and Unicode
   format characters (category Cf: zero-width space, joiners) are removed, so
   ``alice​@example.com`` meets the address rule.
2. **Every link is removed**: with a scheme, ``www.``, scheme-less
   ``host.tld/path`` or ``host.tld?query``, and ``mailto:`` / ``tel:`` /
   ``sip:`` / ``callto:`` links. This runs before step 3, so the digits inside
   a join link do not count as a phone number.
3. **Dial-in material** is found by:
   - a keyword: passcode, password, Passwort, Kennwort, Kennung, meeting ID,
     conference ID, access code, dial-in, "join by phone", "one tap", or a join
     instruction ("Join Zoom Meeting"). Hyphens and spaces inside a keyword are
     optional. The short words ``pin``, ``code``, ``bridge``, ``id``, ``pwd``
     and ``pw`` count only when a ``:``, ``#``, ``=`` or a digit follows, so
     "code review" survives;
   - a phone-like run of 7 or more digits, with spaces, dots, slashes,
     brackets, ``+``, ``x`` and any hyphen or dash (U+2010 to U+2015) between
     them. Dates (``2026-10-06``, ``10/06/2026``, ``6.10.26``, ``2026.10.06``,
     ``2026/10/06``) and times (``10:30``) are masked first;
   - the ``,,digits`` / ``digits#`` forms of a one-tap number.

   In a description, a location or a display name **the whole line is
   dropped**. In a **title** only the matched spans are removed (the keyword
   with its value, the one-tap token, the digit run), because a title is what
   the event is found by: "Project ID: ENG-123 kickoff" keeps "Project
   kickoff", and a keyword with no value next to it ("Password manager
   rollout") is not a secret and stays.
4. **Every email address is removed**: plain, with spaces around ``@`` and the
   dots, ``user@host`` with no dot, and the obfuscated ``user [at] host [dot]
   tld`` / ``user (at) host.tld`` / ``user at host dot tld`` forms.

A line left empty is dropped. Runs of spaces are collapsed.

**The trade: privacy over recall.** These rules remove more than the secrets
they target, and that is accepted (RM-30 security review, S6). Outside a title,
"password reset policy discussion", "Discuss the access code rollout" and
"Meeting ID card" are dropped as dial-in lines, and "Year 2026 2027 2028
planning" as a digit run. In every field, "node.js/react" and "README.md/notes"
are removed as scheme-less links, and "Team at Acme dot com launch" as a
spelled-out address. Narrowing those rules (a TLD allowlist, a stricter
keyword grammar) would let real join links and addresses through, so it is not
done.
"""
from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable

__all__ = ["clean_description", "clean_line", "clean_title"]

_LONG_KEYWORD = (
    r"pass[\s-]*(?:code|word|wort)|kenn[\s-]*(?:wort|ung)|"
    r"meeting[\s-]*(?:id|number|code)|conference[\s-]*(?:id|code)|access[\s-]*code|"
    r"dial[\s-]*in|join\s+by\s+phone|one[\s-]*tap"
)
_SHORT_KEYWORD = r"\b(?:pin|code|bridge|id|pwd|pw)\b"
# Line mode: any long keyword, a join instruction, or a short word with a value.
_KEYWORD = re.compile(
    rf"{_LONG_KEYWORD}|^\s*join\b.*\b(?:meeting|meet|zoom|teams|webex|call)\b|"
    rf"{_SHORT_KEYWORD}\s*(?:[:#=]|\d)",
    re.IGNORECASE,
)
# Title mode: a keyword WITH its value: the token after a separator, or a token
# holding a digit, then any further digit tokens ("Bridge: 1-800-... x 4433").
# A value token stops at a closing bracket, so "(PIN 4321)" leaves "()" to tidy.
_VALUE_TOKEN = r"[^\s)\]}]+"
_KEYWORD_WITH_VALUE = re.compile(
    rf"(?:{_LONG_KEYWORD}|{_SHORT_KEYWORD})"
    rf"(?:\s*[:#=]\s*{_VALUE_TOKEN}|\s+(?=[^\s)\]}}]*\d){_VALUE_TOKEN})"
    rf"(?:\s+(?:x\s*|ext\.?\s*)?(?=[^\s)\]}}]*\d){_VALUE_TOKEN})*",
    re.IGNORECASE,
)
# Masked before the digit-run check: a date or a time is not a phone number.
_DATE_OR_TIME = re.compile(
    r"\b\d{4}[-./]\d{1,2}[-./]\d{1,2}\b|\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|\b\d{1,2}:\d{2}\b"
)
_SEPARATOR = r"\s().\-/+‐-―"
_DIGIT_RUN = re.compile(rf"\+?\d(?:[{_SEPARATOR}x]*\d){{6,}}", re.IGNORECASE)
_ONE_TAP = re.compile(r",,\s*\d|\d{3,}\s*#")
_ONE_TAP_TOKEN = re.compile(r"\S*,,\S*|\S*\d{3,}\s*#\S*")

_URL = re.compile(
    r"\b[a-z][a-z0-9+.-]*://\S*|\bwww\.\S+|\b(?:mailto|tel|sip|callto):\S+|"
    r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?[/?]\S*",
    re.IGNORECASE,
)
_AT = r"(?:\s*(?:\[at\]|\(at\)|\{at\})\s*)"
_DOT = r"(?:\s*(?:\[dot\]|\(dot\)|\{dot\})\s*|\s+dot\s+)"
_EMAIL = re.compile(
    # a plain or no-dot address, spaces allowed around "@" and the dots
    r"[\w.+-]+\s*@\s*[\w-]+(?:\s*\.\s*[\w-]+)*"
    # user [at] host, with real or spelled dots
    rf"|[\w.+-]+{_AT}[\w-]+(?:(?:{_DOT}|\.)[\w-]+)*"
    # user at host dot tld: the plain-word form needs a spelled dot
    rf"|[\w.+-]+\s+at\s+[\w-]+(?:{_DOT}[\w-]+)+",
    re.IGNORECASE,
)
_PERCENT_AT = re.compile(r"%40", re.IGNORECASE)
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]")
# What a removed dial-in leaves behind in a title: a lone "#", "+", "*" or ",,".
_ORPHAN_SYMBOLS = re.compile(r"(?<!\S)[#*+,]+(?!\S)")
_HTML_BREAK = re.compile(r"<\s*(?:br|/p|/div|/li|/h\d)\s*/?\s*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t]{2,}")
_BLANK_RUN = re.compile(r"\n{3,}")
_INVITATION_DELIMITER = "-::~"


def clean_line(text: object) -> str:
    """One line of calendar text, cleaned; ``""`` when nothing safe is left.

    Used for the location and each display name: a dial-in line is dropped
    whole. Embedded newlines are cleaned line by line and joined with a space.
    """
    return _clean_lines(text, _clean)


def clean_title(text: object) -> str:
    """An event title, cleaned span by span (module docstring, step 3)."""
    return _clean_lines(text, _clean_title_line)


def clean_description(description: object) -> str:
    """The description as plain text, cleaned line by line.

    HTML is turned into text first, and Google's inserted invitation block
    (between two ``-::~`` delimiter lines, or from one to the end) is dropped
    whole. Blank lines between paragraphs are kept, at most one in a row.
    """
    if not isinstance(description, str):
        return ""
    text = _HTML_TAG.sub("", _HTML_BREAK.sub("\n", description))
    kept: list[str] = []
    in_invitation = False
    for line in _normalize(text).splitlines():
        if line.strip().startswith(_INVITATION_DELIMITER):
            in_invitation = not in_invitation
            continue
        if in_invitation:
            continue
        cleaned = _clean(line)
        if cleaned or not line.strip():
            kept.append(cleaned)
    return _BLANK_RUN.sub("\n\n", "\n".join(kept)).strip()


def _clean_lines(text: object, clean: Callable[[str], str]) -> str:
    """Normalise ``text`` once, then clean each of its lines with ``clean``."""
    if not isinstance(text, str):
        return ""
    kept = (clean(line) for line in _normalize(text).splitlines())
    return " ".join(part for part in kept if part)


def _normalize(text: str) -> str:
    decoded = _PERCENT_AT.sub("@", html.unescape(text))
    folded = unicodedata.normalize("NFKC", decoded)
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


def _clean(line: str) -> str:
    """Line mode, on a normalised line: links removed, dropped if dial-in material."""
    without_links = _URL.sub(" ", line)
    if _is_dial_in(without_links):
        return ""
    return _tidy(_EMAIL.sub(" ", without_links))


def _clean_title_line(line: str) -> str:
    """Title mode, on a normalised line: links, each dial-in span, then addresses go."""
    # Keyword spans first, so a keyword's digit value goes with it; then digit
    # runs (dates and times masked in place, so positions still line up); then
    # what is left of a one-tap form.
    text = _KEYWORD_WITH_VALUE.sub(" ", _URL.sub(" ", line))
    masked = _DATE_OR_TIME.sub(lambda m: "§" * len(m.group()), text)
    for match in reversed(list(_DIGIT_RUN.finditer(masked))):
        text = f"{text[:match.start()]} {text[match.end():]}"
    text = _EMAIL.sub(" ", _ONE_TAP_TOKEN.sub(" ", text))
    text = _ORPHAN_SYMBOLS.sub(" ", _EMPTY_BRACKETS.sub(" ", text))
    return _tidy(text).rstrip("([{").rstrip()


def _tidy(text: str) -> str:
    # Trailing separators only ("Notes doc: <link>" -> "Notes doc"), so a
    # leading list marker survives.
    return _SPACES.sub(" ", text).strip().rstrip(",;:|-").rstrip()


def _is_dial_in(line: str) -> bool:
    if _KEYWORD.search(line) or _ONE_TAP.search(line):
        return True
    masked = _DATE_OR_TIME.sub(" ", line)
    return _DIGIT_RUN.search(masked) is not None
