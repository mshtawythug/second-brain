"""Receipt-attachment text rules for ``brain ingest-gmail --with-attachments`` (RM-45).

Pure logic: no I/O, no logging. An attachment's text and its filename are a third
party's (anyone can mail the owner a PDF), and both end up in the thread document's
body, which ``brain ui`` renders and the vault link parser reads. So before a section
is built:

- **The text is normalized first** (sec-117 F4, F7): every line break ``str.splitlines``
  honours (``\r``, ``\v``, ``\f``, U+0085, U+2028, U+2029) becomes ``\n``, so the line
  escapes below apply to every line any later reader sees; every space separator
  (NBSP, U+2007, U+202F, …) becomes a space; every other control and format character
  (soft hyphen, zero-width space) goes. Doing this before redaction is what keeps a
  ``4111<soft hyphen>1111…`` from turning into a bare number after it was checked.
- **Card numbers** are redacted whatever ``BRAIN_SECRET_GUARD`` says (the guard has no
  card pattern), in the **strict layouts only** (team-lead's ruling on rev-117: the
  first, permissive rule redacted itemized amounts and phone numbers, which are most of
  what a receipt says). Unicode decimal digits are folded to ASCII first. A window of
  whole digit groups is a card when it passes the Luhn check and is one of:

  1. one unseparated run of 13 to 19 digits;
  2. 4-digit groups, the last one 1 to 4 digits, 13 to 19 digits in all, joined by
     **one** separator used throughout (one to three spaces, a tab, ``-``, ``.``, ``/``,
     an en dash, an em dash or U+2212), with at most one line wrap and only between two
     4-digit groups;
  3. Amex 4-6-5 or Diners 4-6-4, with one separator throughout.

  Windows are group-aligned inside a longer run, so a card printed before an expiry
  (``12/28``), an amount or a quantity is still found. The run must stand as its own
  word (no letter, digit or underscore touching it), which leaves a token's digits to
  the secret guard. **Accepted residuals** (sec-117 R2): a number glued to letters
  (``VISA4111…``, ``…1111EXP``), four or more spaces, a blank line or ``,`` between
  groups, and mixed separators are not redacted. Masked forms (``**** 0000``) are kept:
  they say which card without being one.
- **Markup is neutralized**, so the text cannot pose as thread structure (a stray
  ``</details>``, a fake ``## <date> — <sender>`` heading, a setext underline, a code
  fence that swallows the sections after it) or pull a remote image or a wikilink into
  the owner's graph. Control and format characters go (newline and tab stay); the text
  is HTML-escaped; backslashes and ``[`` are backslash-escaped; a line whose first
  non-blank character is markdown block punctuation gets a backslash before it.
- **Names** become one escaped line, capped before escaping.

Text is normalized, redacted, then cut, then neutralized, so a cut can never leave half
a card number behind, and an escape sequence is never cut in two.
"""
from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from . import ExtractedDoc
from .text_safety import one_line

__all__ = [
    "MAX_NAME_CHARS",
    "REDACTED_CARD",
    "TRUNCATED_LINE",
    "UNNAMED",
    "neutralize_markup",
    "normalize_text",
    "redact_card_numbers",
    "render_section",
    "safe_name",
    "with_sections",
]

#: What a card number becomes.
REDACTED_CARD = "[REDACTED:card-number]"
#: The line that ends a section cut at its character cap.
TRUNCATED_LINE = "[attachment text truncated]"
#: The heading name of an attachment whose filename is empty or not a string.
UNNAMED = "(unnamed attachment)"
#: The longest name, in characters before escaping, the ellipsis included.
MAX_NAME_CHARS = 120

#: A separator a card layout may use between its groups (one kind per card).
_SEPARATOR = "(?: {1,3}|[\t\\-./–—−])"
#: A line wrap between two groups, with blanks around it.
_WRAP = "[ \t]{0,3}\n[ \t]{0,3}"
#: A maximal digit-group run that stands as a word: no letter, digit or underscore
#: touching either end. The word rule keeps the redaction out of tokens
#: (``ghp_…0000000000000000``): cutting digits out of a credential would leave the rest
#: of it for the secret guard to miss. Which windows of the run are cards is
#: :func:`_is_card_layout`'s call.
_DIGIT_RUN_RE = re.compile(
    rf"(?<![0-9A-Za-z_])[0-9]+(?:(?:{_WRAP}|{_SEPARATOR})[0-9]+)*(?![0-9A-Za-z_])"
)
_GROUP_RE = re.compile(r"[0-9]+")
_MIN_CARD_DIGITS = 13
_MAX_CARD_DIGITS = 19
#: The most groups a card layout has (4-4-4-4-3).
_MAX_CARD_GROUPS = 5
#: Amex 4-6-5 and Diners 4-6-4.
_FIXED_LAYOUTS = frozenset({(4, 6, 5), (4, 6, 4)})
#: Line breaks ``str.splitlines`` honours that are not ``\n``.
_LINE_BREAK_RE = re.compile("\r\n|[\r\x0b\x0c\x85\u2028\u2029]")
#: Markdown block punctuation that can open a heading, rule, fence, list or table row.
_BLOCK_START_RE = re.compile(r"^([ \t]*)([#=\-_*~`+|])", re.MULTILINE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_KEPT_CONTROLS = frozenset("\n\t")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _is_card_layout(lengths: Sequence[int], separators: Sequence[str]) -> bool:
    """Whether groups of ``lengths`` joined by ``separators`` are a strict card layout."""
    total = sum(lengths)
    if len(lengths) == 1:
        return _MIN_CARD_DIGITS <= total <= _MAX_CARD_DIGITS
    wraps = [index for index, separator in enumerate(separators) if "\n" in separator]
    if len(set(separators) - {separators[index] for index in wraps}) > 1:
        return False  # more than one kind of separator
    if tuple(lengths) in _FIXED_LAYOUTS:
        return not wraps
    if len(wraps) > 1 or any(lengths[i] != 4 or lengths[i + 1] != 4 for i in wraps):
        return False
    return (
        all(length == 4 for length in lengths[:-1])
        and 1 <= lengths[-1] <= 4
        and _MIN_CARD_DIGITS <= total <= _MAX_CARD_DIGITS
    )


def _card_window(groups: list[re.Match[str]], run: str, start: int) -> int | None:
    """The last group of the longest card window starting at ``start``, or ``None``."""
    best = None
    for end in range(start, min(start + _MAX_CARD_GROUPS, len(groups))):
        window = groups[start : end + 1]
        separators = [run[a.end() : b.start()] for a, b in zip(window, window[1:], strict=False)]
        digits = "".join(group.group(0) for group in window)
        if len(digits) > _MAX_CARD_DIGITS:
            break
        if _is_card_layout([len(g.group(0)) for g in window], separators) and _luhn_ok(digits):
            best = end
    return best


def _fold_digits(text: str) -> str:
    """Every Unicode decimal digit (Arabic-Indic, fullwidth, …) as its ASCII digit."""
    return "".join(
        str(unicodedata.decimal(ch)) if not ch.isascii() and ch.isdecimal() else ch
        for ch in text
    )


def _redact_run(match: re.Match[str]) -> str:
    run = match.group(0)
    groups = list(_GROUP_RE.finditer(run))
    pieces: list[str] = []
    kept_from = 0
    index = 0
    while index < len(groups):
        end = _card_window(groups, run, index)
        if end is None:
            index += 1
            continue
        pieces.extend([run[kept_from : groups[index].start()], REDACTED_CARD])
        kept_from = groups[end].end()
        index = end + 1
    pieces.append(run[kept_from:])
    return "".join(pieces)


def normalize_text(text: str) -> str:
    """Line breaks to ``\n``, space separators to a space, other C* characters dropped."""
    text = _LINE_BREAK_RE.sub("\n", text)
    kept: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if ch in _KEPT_CONTROLS:
            kept.append(ch)
        elif category == "Zs":
            kept.append(" ")
        elif not category.startswith("C"):
            kept.append(ch)
    return "".join(kept)


def redact_card_numbers(text: str) -> str:
    """``text``, normalized and digit-folded, each card replaced by :data:`REDACTED_CARD`."""
    return _DIGIT_RUN_RE.sub(_redact_run, _fold_digits(normalize_text(text)))


def _escape_inline(text: str) -> str:
    """HTML-escape, then backslash-escape backslashes and ``[`` (links, images, wikilinks)."""
    escaped = html.escape(text, quote=False)
    return escaped.replace("\\", "\\\\").replace("[", "\\[")


def neutralize_markup(text: str) -> str:
    """``text`` made inert as markdown and HTML (module docstring), trimmed."""
    escaped = _escape_inline(normalize_text(text))
    escaped = _BLOCK_START_RE.sub(r"\1\\\2", escaped)
    lines = [line.rstrip() for line in escaped.split("\n")]
    return _BLANK_RUN_RE.sub("\n\n", "\n".join(lines)).strip()


def safe_name(raw: object) -> str:
    """A filename as one escaped line: capped at :data:`MAX_NAME_CHARS` before escaping.

    ``#`` is escaped too, so a name cannot close or start a heading.
    """
    name = one_line(raw) if isinstance(raw, str) else ""
    if not name:
        return UNNAMED
    if len(name) > MAX_NAME_CHARS:
        name = f"{name[: MAX_NAME_CHARS - 1]}…"
    return _escape_inline(name).replace("#", "\\#")


def _cut(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    window = text[:max_chars]
    newline = window.rfind("\n")
    return (window[:newline] if newline > 0 else window), True


def render_section(raw_name: object, text: str, *, max_chars: int) -> str | None:
    """The ``## Attachment: <name>`` section for one attachment, or ``None`` if empty.

    ``max_chars`` caps the text (counted after redaction, before escaping); a cut text
    ends with :data:`TRUNCATED_LINE`.
    """
    body, cut = _cut(redact_card_numbers(text), max_chars)  # redaction normalizes first
    clean = neutralize_markup(body)
    if not clean:
        return None
    if cut:
        clean = f"{clean}\n{TRUNCATED_LINE}"
    return f"## Attachment: {safe_name(raw_name)}\n\n{clean}"


def with_sections(
    doc: ExtractedDoc, sections: Sequence[str], metadata: Mapping[str, Any]
) -> ExtractedDoc:
    """A new document: ``doc``'s body with ``sections`` appended, ``metadata`` merged in."""
    content = "\n\n".join([doc.content, *sections]) if sections else doc.content
    return replace(doc, content=content, metadata={**doc.metadata, **metadata})
