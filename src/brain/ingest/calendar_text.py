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
   ``sip:`` / ``callto:`` / ``skype:`` links. This runs before step 3, so the digits inside
   a join link do not count as a phone number.
3. **Dial-in material** is found by:
   - a keyword: passcode, password, Passwort, Kennwort, Kennung, meeting ID,
     conference ID, access code, dial-in, "join by phone", "one tap", or a join
     instruction ("Join Zoom Meeting"). Hyphens and spaces inside a keyword are
     optional. ``pwd`` and ``pw`` are credential keywords too. The short words
     ``pin``, ``code``, ``bridge`` and ``id`` count only with a value: after
     ``:``, ``#`` or ``=``, anything; after an optional ``is``, ``-``, ``–``,
     ``—`` or ``->``, a digit. So "code review" survives;
   - a phone-like run of 7 or more digits, with spaces, dots, slashes,
     brackets, ``+``, ``x`` and any hyphen or dash (U+2010 to U+2015) between
     them. Dates (``2026-10-06``, ``10/06/2026``, ``6.10.26``, ``2026.10.06``,
     ``2026/10/06``) and times (``10:30``) are masked first;
   - the ``,,digits`` / ``digits#`` forms of a one-tap number.

   In a description, a location or a display name **the whole line is
   dropped**. A **title** is what the event is found by, so it keeps the words
   before the secret. The title's lines are joined into one, then:

   - a credential keyword (passcode, password, Passwort, Kennwort, Kennung,
     access code, meeting ID, conference ID, ``pwd``, ``pw``) **truncates the
     title** from the keyword to the end, whatever follows: "Weekly sync,
     passcode is 7Hk2pQ" keeps "Weekly sync". The one exception is a keyword
     with no value: one that ends the title, or is followed only by "rollout",
     "manager", "reset", "policy" or "card" ("Password manager rollout",
     "Access code rollout"), stays;
   - a short word with a value (``PIN 4321``, ``PIN-4321``, ``PIN4321``,
     ``P.I.N. 4321``, ``code is 4321``, ``code — 4321``, ``ID: ENG-123``)
     truncates the title the same way: "Project ID: ENG-123 kickoff" keeps
     "Project";
   - a dial-in keyword loses its value, and a digit run and a one-tap form are
     removed in place, so "Q4 budget: 1 250 000 USD" keeps "Q4 budget: USD".
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

**Accepted residual risk** (RM-30 security review): a bare value with no
keyword and under seven digits ("Sync 123456") and digits spelled out as words
("PIN four three two one") are not recognised in any field.
"""
from __future__ import annotations

import html
import re
import unicodedata

__all__ = ["clean_description", "clean_line", "clean_title"]

# Credential keywords. In a title, one truncates the line (module docstring).
_CREDENTIAL_KEYWORD = (
    r"pass[\s-]*(?:code|word|wort)|kenn[\s-]*(?:wort|ung)|access[\s-]*code|"
    r"meeting[\s-]*(?:id|number|code)|conference[\s-]*(?:id|code)|\bpwd\b|\bpw\b"
)
_DIAL_KEYWORD = r"dial[\s-]*in|join\s+by\s+phone|one[\s-]*tap"
# A short word with a value: after an explicit ":", "=" or "#", anything; after
# an optional "is", "-", "–", "—" or "->", a digit ("PIN-4321", "PIN4321",
# "P.I.N. 4321", "code is 4321", "code — 4321").
# "Not followed by a letter" rather than "\b": "PIN4321" counts, "pinned" not.
_SHORT_WORD = r"(?:\bp\.?\s?i\.?\s?n\.?|\bcode|\bid|\bbridge)(?![^\W\d_])"
_SHORT_WITH_VALUE = (
    rf"{_SHORT_WORD}\s*[:=#]\s*\S|{_SHORT_WORD}\s*(?:is\b|->|[-–—])?\s*\d"
)
# Line mode: any keyword, a join instruction, or a short word with a value.
_KEYWORD = re.compile(
    rf"{_CREDENTIAL_KEYWORD}|{_DIAL_KEYWORD}|"
    rf"^\s*join\b.*\b(?:meeting|meet|zoom|teams|webex|call)\b|{_SHORT_WITH_VALUE}",
    re.IGNORECASE,
)
# Title mode (module docstring): a credential keyword or a short word with a
# value truncates the title there; a dial keyword loses its value only.
_CREDENTIAL = re.compile(_CREDENTIAL_KEYWORD, re.IGNORECASE)
# A line whose credential's value is not on it: it ends with the keyword.
_ENDS_WITH_CREDENTIAL = re.compile(
    rf"(?:{_CREDENTIAL_KEYWORD}|{_SHORT_WORD})\s*[:#=\-]?\s*$", re.IGNORECASE
)
_SHORT_VALUE = re.compile(_SHORT_WITH_VALUE, re.IGNORECASE)
#: Words that may follow a credential keyword without making it a value
#: ("Password manager rollout", "Access code rollout").
_VALUELESS_FOLLOWERS = frozenset({"rollout", "manager", "reset", "policy", "card"})
_VALUE_TOKEN = r"[^\s)\]}]+"
_DIAL_WITH_VALUE = re.compile(
    rf"(?:{_DIAL_KEYWORD})"
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
    r"\b[a-z][a-z0-9+.-]*://\S*|\bwww\.\S+|\b(?:mailto|tel|sip|callto|skype):\S+|"
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
#: A line of at most this many tokens right after a line that ends with a
#: credential is taken to be its value (clean_description).
_BARE_VALUE_TOKENS = 3


def clean_line(text: object) -> str:
    """One line of calendar text, cleaned; ``""`` when nothing safe is left.

    Used for the location and each display name: a dial-in line is dropped
    whole. Embedded newlines are joined first, as for a title, so a keyword and
    its value on two lines ("Room 4 passcode\\n4321") are judged together.
    """
    if not isinstance(text, str):
        return ""
    return _clean(" ".join(_normalize(text).splitlines()))


def clean_title(text: object) -> str:
    """An event title, cleaned as ONE line (module docstring, step 3).

    The title's lines are joined first, so a keyword on one line and its value
    on the next ("Sync passcode\\n4321") are seen together.
    """
    if not isinstance(text, str):
        return ""
    return _clean_title_line(" ".join(_normalize(text).splitlines()))


def clean_description(description: object) -> str:
    """The description as plain text, cleaned line by line.

    HTML is turned into text first, and Google's inserted invitation block
    (between two ``-::~`` delimiter lines, or from one to the end) is dropped
    whole. Blank lines between paragraphs are kept, at most one in a row.

    A credential's value can sit on the next line ("Passcode:" then "424242"),
    so when a line ENDS with a credential keyword or short word (a separator
    allowed), the next line is dropped too if it is a bare value: at most
    :data:`_BARE_VALUE_TOKENS` tokens. A blank line in between ends that.
    """
    if not isinstance(description, str):
        return ""
    text = _HTML_TAG.sub("", _HTML_BREAK.sub("\n", description))
    kept: list[str] = []
    in_invitation = False
    value_follows = False
    for line in _normalize(text).splitlines():
        if line.strip().startswith(_INVITATION_DELIMITER):
            in_invitation = not in_invitation
            continue
        if in_invitation:
            continue
        if value_follows and 0 < len(line.split()) <= _BARE_VALUE_TOKENS:
            value_follows = False
            continue  # the previous line's credential value, on its own line
        value_follows = _ENDS_WITH_CREDENTIAL.search(_URL.sub(" ", line)) is not None
        cleaned = _clean(line)
        if cleaned or not line.strip():
            kept.append(cleaned)
    return _BLANK_RUN.sub("\n\n", "\n".join(kept)).strip()


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
    """Title mode, on a normalised line (module docstring, step 3)."""
    text = _truncate_at_secret(_URL.sub(" ", line))
    text = _DIAL_WITH_VALUE.sub(" ", text)
    # Digit runs, with dates and times masked in place so positions line up;
    # then what is left of a one-tap form.
    masked = _DATE_OR_TIME.sub(lambda m: "§" * len(m.group()), text)
    for match in reversed(list(_DIGIT_RUN.finditer(masked))):
        text = f"{text[:match.start()]} {text[match.end():]}"
    text = _EMAIL.sub(" ", _ONE_TAP_TOKEN.sub(" ", text))
    text = _ORPHAN_SYMBOLS.sub(" ", _EMPTY_BRACKETS.sub(" ", text))
    return _tidy(text).rstrip("([{").rstrip()


def _truncate_at_secret(text: str) -> str:
    """``text`` up to the first credential keyword or short word with a value.

    A credential keyword that ends the line, or is followed only by
    :data:`_VALUELESS_FOLLOWERS`, carries no value and is kept.
    """
    cut = len(text)
    short = _SHORT_VALUE.search(text)
    if short is not None:
        cut = short.start()
    for match in _CREDENTIAL.finditer(text, 0, cut):
        following = [word.strip(".,;:!?()[]{}").lower() for word in text[match.end():].split()]
        if not all(word in _VALUELESS_FOLLOWERS for word in following):
            return text[: match.start()]
    return text[:cut]


def _tidy(text: str) -> str:
    # Trailing separators only ("Notes doc: <link>" -> "Notes doc"), so a
    # leading list marker survives.
    return _SPACES.sub(" ", text).strip().rstrip(",;:|-").rstrip()


def _is_dial_in(line: str) -> bool:
    if _KEYWORD.search(line) or _ONE_TAP.search(line):
        return True
    masked = _DATE_OR_TIME.sub(" ", line)
    return _DIGIT_RUN.search(masked) is not None
