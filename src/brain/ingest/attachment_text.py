"""Receipt-attachment text rules for ``brain ingest-gmail --with-attachments`` (RM-45).

Pure logic: no I/O, no logging. An attachment's text and its filename are a third
party's (anyone can mail the owner a PDF), and both end up in the thread document's
body, which ``brain ui`` renders and the vault link parser reads. So before a section
is built:

- **Card numbers** are redacted whatever ``BRAIN_SECRET_GUARD`` says (the guard has no
  card pattern): a maximal run of 13 to 19 ASCII digits, single spaces or hyphens
  allowed between digits, standing as its own word (so a token's digits are left for
  the guard), that passes the Luhn check. Masked forms (``**** 0000``)
  are kept: they say which card without being one.
- **Markup is neutralized**, so the text cannot pose as thread structure (a stray
  ``</details>``, a fake ``## <date> — <sender>`` heading, a setext underline, a code
  fence that swallows the sections after it) or pull a remote image or a wikilink into
  the owner's graph. Control and format characters go (newline and tab stay); the text
  is HTML-escaped; backslashes and ``[`` are backslash-escaped; a line whose first
  non-blank character is markdown block punctuation gets a backslash before it.
- **Names** become one escaped line, capped before escaping.

Text is redacted, then cut, then neutralized, so a cut can never leave half a card
number behind, and an escape sequence is never cut in two.
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

#: A maximal digit run that stands as a word: no digit (or digit-and-separator) before
#: or after it, and no letter or underscore touching it. The word rule keeps the
#: redaction out of tokens (``ghp_…0000000000000000``): cutting digits out of a
#: credential would leave the rest of it for the secret guard to miss.
_CARD_RE = re.compile(
    r"(?<![0-9A-Za-z_])(?<![0-9][ -])[0-9](?:[ -]?[0-9]){12,18}(?![ -]?[0-9]|[A-Za-z_])"
)
_SEPARATORS_RE = re.compile(r"[ -]")
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


def _redact_match(match: re.Match[str]) -> str:
    digits = _SEPARATORS_RE.sub("", match.group(0))
    return REDACTED_CARD if _luhn_ok(digits) else match.group(0)


def redact_card_numbers(text: str) -> str:
    """``text`` with every Luhn-valid card-number run replaced by :data:`REDACTED_CARD`."""
    return _CARD_RE.sub(_redact_match, text)


def _drop_controls(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        ch
        for ch in text
        if ch in _KEPT_CONTROLS or not unicodedata.category(ch).startswith("C")
    )


def _escape_inline(text: str) -> str:
    """HTML-escape, then backslash-escape backslashes and ``[`` (links, images, wikilinks)."""
    escaped = html.escape(text, quote=False)
    return escaped.replace("\\", "\\\\").replace("[", "\\[")


def neutralize_markup(text: str) -> str:
    """``text`` made inert as markdown and HTML (module docstring), trimmed."""
    escaped = _escape_inline(_drop_controls(text))
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
    body, cut = _cut(redact_card_numbers(text), max_chars)
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
