"""Apple Notes' HTML bodies as Markdown, with the stdlib ``html.parser`` (RM-36).

Notes hands a note's body back as HTML: one ``<div>`` per line, ``<h1>``–``<h3>``
for the title and headings, ``<b>``/``<i>``, ``<ul>``/``<ol>`` lists, checklists
and links. A small converter is enough for that vocabulary, so no Markdown
library is added (RM-36.D1). What it keeps:

- **headings** as ``#`` lines, **bold** and *italic*, inline ``code``;
- **lists**, nested two spaces per level, ``<ol>`` numbered;
- **checklists** as ``- [ ]`` / ``- [x]``: a list whose class names a checklist
  (``checklist``, ``Apple-checklist``…) with ``checked`` in an item's class or a
  ``data-checked="true"`` attribute, or an ``<input type="checkbox">`` in the item;
- **links** as ``[text](href)``, for ``http``, ``https`` and ``mailto`` only; any
  other scheme (``javascript:``, ``data:``, ``file:``, a Notes-internal link)
  keeps its text and loses its target;
- block quotes, ``<pre>`` as a fenced block, ``<hr>``, and table rows as
  ``a | b`` lines.

What it drops: attachments and embedded media (``img``, ``object``, ``embed``,
``video``, ``audio``, ``iframe``, ``svg``), which the ticket scopes out, and
``script``/``style``/``head`` content. Text is not Markdown-escaped: the result
is for search and reading, and an escape would only add noise to both.

**Bounded on hostile input** (the #103 security review). A body is the note
author's HTML, so its shape is not trusted:

- list indentation and quote markers stop growing at :data:`MAX_NESTING` levels,
  because every line repeats them and deep nesting would otherwise grow the
  output quadratically;
- at most :data:`MAX_OPEN_INLINE` links and emphasis marks are tracked at once
  (deeper ones keep their text and lose their markup), and both stacks are
  cleared at every line end, so closing tags cost linear work in all;
- the output stops at :data:`MAX_OUTPUT_CHARS`, with :data:`TRUNCATION_MARKER`;
- a numeric character reference with :data:`_MAX_CHARREF_DIGITS` or more digits
  becomes U+FFFD before parsing, because ``html.unescape`` turns a decimal one
  into an ``int`` and Python refuses more than 4,300 digits with a
  ``ValueError``. No valid code point needs more than seven decimal digits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

__all__ = [
    "MAX_NESTING",
    "MAX_OPEN_INLINE",
    "MAX_OUTPUT_CHARS",
    "TRUNCATION_MARKER",
    "html_to_markdown",
]

#: List and quote depth past which the indent and the ``>`` markers stop growing.
MAX_NESTING = 8
#: Open links and emphasis marks tracked at once; deeper ones are kept as plain text.
MAX_OPEN_INLINE = 64
#: The longest Markdown one body yields; the rest is cut and marked.
MAX_OUTPUT_CHARS = 1_000_000
TRUNCATION_MARKER = f"[note truncated: over {MAX_OUTPUT_CHARS} characters]"
_MAX_CHARREF_DIGITS = 16
_HUGE_CHARREF = re.compile(
    rf"&#(?:[xX][0-9a-fA-F]{{{_MAX_CHARREF_DIGITS},}}|[0-9]{{{_MAX_CHARREF_DIGITS},}});?"
)

_HEADINGS = {f"h{n}": n for n in range(1, 7)}
_BLOCKS = frozenset(
    {"div", "p", "section", "article", "header", "footer", "body", "table", "tbody", "thead"}
)
#: Tags whose whole content is dropped: attachments, media, and non-text. Void
#: elements (``img``, ``embed``) have no content and so need no entry; listing one
#: would open a skip that no end tag closes.
_SKIPPED = frozenset(
    {"script", "style", "head", "title", "object", "video", "audio", "iframe", "svg"}
)
_EMPHASIS = {"b": "**", "strong": "**", "i": "*", "em": "*", "tt": "`", "code": "`"}
_SAFE_SCHEMES = ("http://", "https://", "mailto:")
_TARGET_ESCAPES = str.maketrans(
    {" ": "%20", "(": "%28", ")": "%29", "<": "%3C", ">": "%3E"}
)
#: An inline ``data:`` payload (an image's base64), dropped from any output line:
#: attachments are out of scope, and the JXA script's own ``<img>`` strip is the
#: first line of defence, not the only one (#103 review).
_DATA_URI = re.compile(r"\bdata:[\w.+-]+/[\w.+-]+(?:;[\w.+=-]+)*,[^\s\"'<>]*", re.IGNORECASE)
_SPACES = re.compile(r"[ \t\r\n\f\v ]+")
_BLANK_RUN = re.compile(r"\n{3,}")


@dataclass
class _List:
    ordered: bool
    checklist: bool
    count: int = 0


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.inline: list[str] = []
        self.prefix = ""
        self.lists: list[_List] = []
        self.quote = 0
        self.skip = 0
        self.pre = False
        self.marks: list[tuple[str, int]] = []
        self.links: list[tuple[str | None, int]] = []
        self.cells = 0
        self.size = 0
        self.truncated = False
        #: Characters joined and stack entries scanned: the linearity tests read it.
        self.work = 0

    # --- lines ------------------------------------------------------------------------

    def flush(self, *, blank: bool = False) -> None:
        """End the current line; ``blank`` keeps an empty one (a ``<br>`` paragraph gap)."""
        text = _SPACES.sub(" ", self._join(self.inline)).strip()
        if text or self.prefix.strip():
            quote = "> " * min(self.quote, MAX_NESTING)
            self.emit(f"{quote}{self.prefix}{text}".rstrip())
        elif blank:
            self.emit("")
        self.inline = []
        self.prefix = ""
        self.marks = []
        self.links = []

    def emit(self, line: str) -> None:
        """Append one output line, or stop for good once :data:`MAX_OUTPUT_CHARS` is passed."""
        if self.truncated:
            return
        line = _DATA_URI.sub("", line)
        room = MAX_OUTPUT_CHARS - self.size
        if len(line) + 1 > room:
            self.truncated = True
            line = line[: max(0, room - 1)]  # the part that fits; then nothing more
        self.size += len(line) + 1
        self.lines.append(line)

    def _join(self, parts: list[str]) -> str:
        joined = "".join(parts)
        self.work += len(joined)
        return joined

    # --- tags -------------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skip or tag in _SKIPPED:
            self.skip += tag in _SKIPPED
            return
        attr = {name: value or "" for name, value in attrs}
        if self.pre:
            return
        if tag in _HEADINGS:
            self.flush()
            self.prefix = "#" * _HEADINGS[tag] + " "
        elif tag in ("ul", "ol"):
            self.flush()
            self.lists.append(_List(tag == "ol", "checklist" in attr.get("class", "").lower()))
        elif tag == "li":
            self._start_item(attr)
        elif tag == "input" and attr.get("type", "").lower() == "checkbox":
            self._box("checked" in attr)
        elif tag == "a":
            if len(self.links) < MAX_OPEN_INLINE:
                href = attr.get("href", "").strip()
                safe = href.lower().startswith(_SAFE_SCHEMES)
                self.links.append((href if safe else None, len(self.inline)))
        elif tag in _EMPHASIS:
            if len(self.marks) < MAX_OPEN_INLINE:
                self.marks.append((tag, len(self.inline)))
                self.inline.append(_EMPHASIS[tag])
        elif tag == "br":
            self.flush(blank=True)
        elif tag == "blockquote":
            self.flush()
            self.quote += 1
        elif tag == "pre":
            self.flush()
            self.emit("```")
            self.pre = True
        elif tag == "hr":
            self.flush()
            self.emit("---")
        elif tag == "tr":
            self.flush()
            self.cells = 0
        elif tag in ("td", "th"):
            if self.cells:
                self.inline.append(" | ")
            self.cells += 1
        elif tag in _BLOCKS:
            self.flush()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in ("br", "hr", "input"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.skip:
            self.skip -= tag in _SKIPPED
            return
        if tag == "pre":
            if self.pre:
                for line in self._join(self.inline).strip("\n").split("\n"):
                    self.emit(line)
                self.inline = []
                self.emit("```")
                self.pre = False
            return
        if self.pre:
            return
        if tag in ("ul", "ol"):
            self.flush()
            if self.lists:
                self.lists.pop()
        elif tag == "a":
            self._end_link()
        elif tag in _EMPHASIS:
            self._end_mark(tag)
        elif tag == "blockquote":
            self.flush()
            self.quote = max(0, self.quote - 1)
        elif tag in _HEADINGS or tag in _BLOCKS or tag in ("li", "tr"):
            self.flush()

    def handle_data(self, data: str) -> None:
        if not self.skip and not self.truncated:
            self.inline.append(data)

    def markdown(self) -> str:
        text = _BLANK_RUN.sub("\n\n", "\n".join(self.lines)).strip()
        if self.truncated:
            return f"{text}\n\n{TRUNCATION_MARKER}".strip()
        return text

    # --- helpers ----------------------------------------------------------------------

    def _start_item(self, attr: dict[str, str]) -> None:
        self.flush()
        current = self.lists[-1] if self.lists else _List(ordered=False, checklist=False)
        current.count += 1
        indent = "  " * min(max(0, len(self.lists) - 1), MAX_NESTING)
        bullet = f"{current.count}. " if current.ordered else "- "
        self.prefix = indent + bullet
        if current.checklist:
            checked = "checked" in attr.get("class", "").lower().split() or (
                attr.get("data-checked", "").lower() == "true"
            )
            self._box(checked)

    def _box(self, checked: bool) -> None:
        if "[" in self.prefix:
            return  # the item already has its box
        box = "[x] " if checked else "[ ] "
        if self.prefix.endswith(("- ", ". ")):
            self.prefix += box
        else:
            self.prefix = f"{self.prefix}- {box}"

    def _end_link(self) -> None:
        if not self.links:
            return
        href, start = self.links.pop()
        text = _SPACES.sub(" ", self._join(self.inline[start:])).strip()
        if href is None:
            return  # an unsafe or missing target: the text stays, the target goes
        # A target never closes its own ``(…)`` or ``<…>``, and the text never its ``[…]``.
        target = href.translate(_TARGET_ESCAPES)
        label = text.replace("[", "\\[").replace("]", "\\]")
        self.inline[start:] = [f"[{label}]({target})" if label else f"<{target}>"]

    def _end_mark(self, tag: str) -> None:
        for i in range(len(self.marks) - 1, -1, -1):
            self.work += 1
            if self.marks[i][0] == tag:
                _tag, start = self.marks.pop(i)
                break
        else:
            return
        if start >= len(self.inline):
            return  # the line was flushed under the open mark
        inner = self._join(self.inline[start + 1:])
        if not inner.strip():
            if self.inline[start] == _EMPHASIS[tag]:
                del self.inline[start]  # an empty or blank emphasis: no stray markers
            return
        # Markdown emphasis must hug the text, so surrounding spaces move outside it.
        mark = _EMPHASIS[tag]
        lead = inner[: len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()):]
        self.inline[start:] = [f"{lead}{mark}{inner.strip()}{mark}{trail}"]


def html_to_markdown(body: str) -> str:
    """``body`` (a Notes HTML body) as Markdown; ``""`` when nothing readable is left."""
    return _convert(body).markdown()


def _convert(body: str) -> _Converter:
    converter = _Converter()
    converter.feed(_HUGE_CHARREF.sub("\ufffd", body))
    converter.close()
    if converter.pre:
        converter.handle_endtag("pre")
    converter.flush()
    return converter
