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
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

__all__ = ["html_to_markdown"]

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

    # --- lines ------------------------------------------------------------------------

    def flush(self, *, blank: bool = False) -> None:
        """End the current line; ``blank`` keeps an empty one (a ``<br>`` paragraph gap)."""
        text = _SPACES.sub(" ", "".join(self.inline)).strip()
        if text or self.prefix.strip():
            self.lines.append(f"{'> ' * self.quote}{self.prefix}{text}".rstrip())
        elif blank:
            self.lines.append("")
        self.inline = []
        self.prefix = ""
        self.marks = []

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
            href = attr.get("href", "").strip()
            safe = href.lower().startswith(_SAFE_SCHEMES)
            self.links.append((href if safe else None, len(self.inline)))
        elif tag in _EMPHASIS:
            self.marks.append((tag, len(self.inline)))
            self.inline.append(_EMPHASIS[tag])
        elif tag == "br":
            self.flush(blank=True)
        elif tag == "blockquote":
            self.flush()
            self.quote += 1
        elif tag == "pre":
            self.flush()
            self.lines.append("```")
            self.pre = True
        elif tag == "hr":
            self.flush()
            self.lines.append("---")
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
                self.lines.extend("".join(self.inline).strip("\n").split("\n"))
                self.inline = []
                self.lines.append("```")
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
        if not self.skip:
            self.inline.append(data)

    # --- helpers ----------------------------------------------------------------------

    def _start_item(self, attr: dict[str, str]) -> None:
        self.flush()
        current = self.lists[-1] if self.lists else _List(ordered=False, checklist=False)
        current.count += 1
        indent = "  " * max(0, len(self.lists) - 1)
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
        text = _SPACES.sub(" ", "".join(self.inline[start:])).strip()
        if href is None:
            return  # an unsafe or missing target: the text stays, the target goes
        target = href.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
        self.inline[start:] = [f"[{text}]({target})" if text else f"<{target}>"]

    def _end_mark(self, tag: str) -> None:
        for i in range(len(self.marks) - 1, -1, -1):
            if self.marks[i][0] == tag:
                _tag, start = self.marks.pop(i)
                break
        else:
            return
        if start >= len(self.inline):
            return  # the line was flushed under the open mark
        inner = "".join(self.inline[start + 1:])
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
    converter = _Converter()
    converter.feed(body)
    converter.close()
    if converter.pre:
        converter.handle_endtag("pre")
    converter.flush()
    return _BLANK_RUN.sub("\n\n", "\n".join(converter.lines)).strip()
