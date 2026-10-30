"""Code-aware chunker: one definition per chunk, each headed by ``path:start-end symbol`` (RM-42).

Python goes through the stdlib :mod:`ast`; every other language, and Python
that does not parse, through a small table of top-level declaration patterns.
No tree-sitter (RM-42.D1). A segment over the budget is packed by blank-line
blocks, then lines, then characters, and every emitted chunk is checked with
the exact ``count_tokens``, so the budget is a guarantee rather than an estimate.
"""
import ast
import re
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from itertools import groupby
from pathlib import PurePosixPath

from ..ollama_budget import INGEST_CHUNK_OVERLAP_TOKENS, INGEST_CHUNK_TARGET_TOKENS
from .chunker import Chunk
from .text_safety import one_line

__all__ = [
    "CODE_CHUNK_BUDGET_TOKENS",
    "MODULE_SYMBOL",
    "NO_PATH",
    "chunk_code",
    "language_of",
]

#: Every code chunk, header included, stays within the prose chunker's ceiling:
#: the bound the embedder's ``num_ctx`` floor is validated against. No overlap —
#: a code chunk is a whole definition (RM-42.D-B).
CODE_CHUNK_BUDGET_TOKENS = INGEST_CHUNK_TARGET_TOKENS + INGEST_CHUNK_OVERLAP_TOKENS

#: The header's path when the caller supplied none.
NO_PATH = "<code>"

#: The symbol of lines outside any definition (the header chunk included).
MODULE_SYMBOL = "<module>"

# The path in a header is metadata (RM-43 sets it), so it is capped twice: by
# characters, and by tokens so a path of expensive characters cannot eat the
# budget. With the symbol cap, a header stays well under the budget, which is
# what lets the character splitter always place at least one character.
_MAX_PATH_CHARS = 200
_MAX_PATH_TOKENS = CODE_CHUNK_BUDGET_TOKENS // 4
_MAX_SYMBOL_CHARS = 80
_ELLIPSIS = "…"

# The character splitter's first probe: about four characters per token, so the
# answer is usually within one doubling of it. Galloping from here keeps every
# probe within twice the chunk it finds, rather than spanning the whole line.
_GALLOP_START_CHARS = 4 * CODE_CHUNK_BUDGET_TOKENS

TokenCounter = Callable[[str], int]


@dataclass(frozen=True)
class _Span:
    """Lines ``start..end`` (1-based, inclusive) of one symbol.

    ``parts`` is set only for a Python class: its own partition, used when the
    whole class is over the budget.
    """

    start: int
    end: int
    symbol: str
    parts: tuple["_Span", ...] = ()


@dataclass(frozen=True)
class _Language:
    """Top-level declaration patterns, matched at column 0, and the prefixes of
    the comment / attribute / decorator lines that belong to the declaration
    directly below them. Every pattern has a ``kw`` and a ``name`` group."""

    declarations: tuple[re.Pattern[str], ...]
    attached: tuple[str, ...]


def _patterns(*sources: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(source) for source in sources)


_JS_PREFIX = r"(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
_JS_NAME = r"(?P<name>[A-Za-z_$][\w$]*)"
_RUST_QUALIFIERS = r'(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|unsafe|const|extern(?:\s+"[^"]*")?)\s+)*'
_SHELL_NAME = r"(?P<name>[A-Za-z_][\w.:-]*)"

_LANGUAGES: dict[str, _Language] = {
    # Only for Python that ``ast`` cannot parse (Python 2, syntax errors).
    "python": _Language(
        _patterns(r"(?:async\s+)?(?P<kw>def|class)\s+(?P<name>[A-Za-z_]\w*)"),
        ("#", "@"),
    ),
    "javascript": _Language(
        _patterns(
            _JS_PREFIX + r"(?P<kw>function)\b\s*\*?\s*" + _JS_NAME + "?",
            _JS_PREFIX + r"(?P<kw>class|interface|type|enum|const|let|var)\s+" + _JS_NAME,
        ),
        ("//", "/*", "*", "@"),
    ),
    "go": _Language(
        _patterns(
            r"(?P<kw>func|type|var|const)\b\s*(?:\([^)]*\)\s*)?(?P<name>[A-Za-z_]\w*)?"
        ),
        ("//", "/*", "*"),
    ),
    "rust": _Language(
        _patterns(
            _RUST_QUALIFIERS
            + r"(?P<kw>fn|struct|enum|trait|union|mod|type|static|const)\s+"
            + r"(?P<name>[A-Za-z_]\w*)",
            r"(?:unsafe\s+)?(?P<kw>impl)\b(?P<name>[^{]*)",
            r"(?P<kw>macro_rules!)\s*(?P<name>[A-Za-z_]\w*)",
        ),
        ("//", "/*", "*", "#["),
    ),
    "java": _Language(
        _patterns(
            r"(?:(?:public|protected|private|abstract|final|static|sealed|non-sealed"
            r"|strictfp)\s+)*(?P<kw>class|interface|enum|record|@interface)\s+"
            r"(?P<name>[A-Za-z_$][\w$]*)"
        ),
        ("//", "/*", "*", "@"),
    ),
    "shell": _Language(
        _patterns(
            r"(?P<kw>function)\s+" + _SHELL_NAME,
            _SHELL_NAME + r"\s*\(\s*\)(?P<kw>)",
        ),
        ("#",),
    ),
}

# Hint spellings and file extensions, both lower-case, to a ``_LANGUAGES`` key.
_ALIASES: dict[str, str] = {
    "python": "python",
    "py": "python",
    "pyi": "python",
    "javascript": "javascript",
    "typescript": "javascript",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "ts": "javascript",
    "tsx": "javascript",
    "go": "go",
    "golang": "go",
    "rust": "rust",
    "rs": "rust",
    "java": "java",
    "shell": "shell",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
}

_PYTHON_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
# "@" too: a decorator whose expression starts on a later line (``@(`` /
# ``dec`` / ``)``) reports that later line, so ``ast`` alone would leave the
# ``@(`` line in the chunk above.
_PYTHON_ATTACHED = ("#", "@")
# What a failed ``ast.parse`` raises on 3.11+: syntax errors (NUL bytes too),
# ValueError on older interpreters for NUL, and the two nesting-depth failures.
_PARSE_FAILURES = (SyntaxError, ValueError, RecursionError, MemoryError)


def language_of(path: str | None, language: str | None) -> str | None:
    """The language to chunk as: a known ``language`` hint, else ``path``'s extension.

    ``None`` means no declaration table applies, and the file is packed as one
    ``<module>`` segment.
    """
    if language is not None:
        resolved = _ALIASES.get(language.strip().lower())
        if resolved is not None:
            return resolved
    if path is None:
        return None
    return _ALIASES.get(PurePosixPath(path).suffix.lower().lstrip("."))


def chunk_code(
    content: str,
    *,
    count_tokens: TokenCounter,
    path: str | None = None,
    language: str | None = None,
) -> list[Chunk]:
    """Split source code at definition boundaries, one definition per chunk.

    Each chunk's content is ``f"{path}:{start}-{end} {symbol}\\n{body}"``, where
    ``body`` is exactly lines ``start..end`` of ``content`` (line endings
    normalised to ``\\n``), and ``count_tokens(content) <=
    CODE_CHUNK_BUDGET_TOKENS``. Separate definitions are never merged
    (RM-42.D-C). Never raises on source that does not parse.
    """
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []
    lines = text.split("\n")
    resolved = language_of(path, language)
    spans = None
    if resolved == "python":
        spans = _python_spans(text, lines)
    if spans is None:
        spans = _structural_spans(lines, _LANGUAGES.get(resolved or ""))
    writer = _Writer(lines, _header_path(path, count_tokens), count_tokens)
    for span in spans:
        writer.emit(span)
    return [Chunk(index=i, content=c) for i, c in enumerate(writer.chunks)]


# --- segmentation -------------------------------------------------------------


def _is_attached(line: str, prefixes: tuple[str, ...]) -> bool:
    stripped = line.strip()
    return bool(stripped) and stripped.startswith(prefixes)


def _pull_up(start: int, floor: int, lines: Sequence[str], prefixes: tuple[str, ...]) -> int:
    """Move ``start`` up over the attached lines directly above it, not above ``floor``."""
    while start > floor and _is_attached(lines[start - 2], prefixes):
        start -= 1
    return start


def _partition(
    definitions: Sequence[_Span], lo: int, hi: int, gap_symbol: str, lines: Sequence[str]
) -> list[_Span]:
    """Cover lines ``lo..hi`` with ``definitions`` and the gaps between them.

    A comment block directly above a definition joins it. A class's first part
    (the one holding its ``class`` line) is stretched to the class's new start,
    so a split class loses no line.
    """
    out: list[_Span] = []
    cursor = lo
    for definition in definitions:
        start = _pull_up(definition.start, cursor, lines, _PYTHON_ATTACHED)
        if start > cursor:
            out.append(_Span(cursor, start - 1, gap_symbol))
        parts = definition.parts
        if parts:
            parts = (replace(parts[0], start=start), *parts[1:])
        out.append(_Span(start, definition.end, definition.symbol, parts))
        cursor = definition.end + 1
    if cursor <= hi:
        out.append(_Span(cursor, hi, gap_symbol))
    return out


_PythonDefinition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _python_definition(node: _PythonDefinition, symbol: str, lines: Sequence[str]) -> _Span:
    start = min([d.lineno for d in node.decorator_list] + [node.lineno])
    end = node.end_lineno or node.lineno
    parts: tuple[_Span, ...] = ()
    if isinstance(node, ast.ClassDef):
        members = [
            _python_definition(child, f"{symbol}.{child.name}", lines)
            for child in node.body
            if isinstance(child, _PYTHON_DEFINITIONS)
        ]
        # One level only: a member is never split by its own members.
        members = [replace(m, parts=()) for m in members]
        parts = tuple(_partition(members, start, end, symbol, lines))
    return _Span(start, end, symbol, parts)


def _python_spans(text: str, lines: Sequence[str]) -> list[_Span] | None:
    """Top-level spans from :mod:`ast`, or ``None`` when the source does not parse."""
    try:
        with warnings.catch_warnings():
            # Invalid escape sequences warn at parse time; the file is data here.
            warnings.simplefilter("ignore")
            tree = ast.parse(text.removeprefix("﻿"))
    except _PARSE_FAILURES:
        return None
    definitions = [
        _python_definition(node, node.name, lines)
        for node in tree.body
        if isinstance(node, _PYTHON_DEFINITIONS)
    ]
    return _partition(definitions, 1, len(lines), MODULE_SYMBOL, lines)


def _symbol(match: re.Match[str]) -> str:
    name = (match.group("name") or "").strip()
    return name or match.group("kw")


def _structural_spans(lines: Sequence[str], language: _Language | None) -> list[_Span]:
    """Spans from a language's declaration patterns; one ``<module>`` span without one.

    A declaration runs to the line before the next one starts, so the lines
    between two declarations belong to the first.
    """
    starts: list[tuple[int, str]] = []
    if language is not None:
        floor = 1
        for number, line in enumerate(lines, start=1):
            match = next((m for p in language.declarations if (m := p.match(line))), None)
            if match is None:
                continue
            starts.append((_pull_up(number, floor, lines, language.attached), _symbol(match)))
            floor = number + 1
    out: list[_Span] = []
    first = starts[0][0] if starts else len(lines) + 1
    if first > 1:
        out.append(_Span(1, first - 1, MODULE_SYMBOL))
    for i, (start, symbol) in enumerate(starts):
        end = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(lines)
        out.append(_Span(start, end, symbol))
    return out


# --- rendering and packing ----------------------------------------------------


def _header_path(path: str | None, count_tokens: TokenCounter) -> str:
    """``path`` on one line, without control characters, capped from the left."""
    shown = one_line(path) if path is not None else ""
    if not shown:
        return NO_PATH
    if len(shown) > _MAX_PATH_CHARS:
        shown = _ELLIPSIS + shown[-(_MAX_PATH_CHARS - 1) :]
    while count_tokens(shown) > _MAX_PATH_TOKENS:
        rest = shown.removeprefix(_ELLIPSIS)
        if not rest:  # only a counter that prices "…" alone over the cap gets here
            break
        shown = _ELLIPSIS + rest[max(1, len(rest) // 4) :]
    return shown


def _label(symbol: str) -> str:
    return one_line(symbol)[:_MAX_SYMBOL_CHARS]


class _Writer:
    """Renders spans into chunk strings, packing any span over the budget."""

    def __init__(self, lines: Sequence[str], path: str, count_tokens: TokenCounter) -> None:
        self._lines = lines
        self._path = path
        self._count = count_tokens
        self.chunks: list[str] = []

    def emit(self, span: _Span) -> None:
        trimmed = self._trim(span.start, span.end)
        if trimmed is None:
            return
        start, end = trimmed
        if self._fits(start, end, span.symbol):
            self.chunks.append(self._render(start, end, span.symbol))
        elif span.parts:
            for part in span.parts:
                self.emit(part)
        else:
            self._pack(self._blocks(start, end), span.symbol, by_lines=True)

    def _trim(self, start: int, end: int) -> tuple[int, int] | None:
        while start <= end and not self._lines[start - 1].strip():
            start += 1
        while end >= start and not self._lines[end - 1].strip():
            end -= 1
        return (start, end) if start <= end else None

    def _blocks(self, start: int, end: int) -> list[tuple[int, int]]:
        """The runs of non-blank lines in ``start..end``."""
        blocks: list[tuple[int, int]] = []
        runs = groupby(range(start, end + 1), key=lambda n: bool(self._lines[n - 1].strip()))
        for is_text, run in runs:
            if is_text:
                numbers = list(run)
                blocks.append((numbers[0], numbers[-1]))
        return blocks

    def _head(self, start: int, end: int, symbol: str) -> str:
        return f"{self._path}:{start}-{end} {_label(symbol)}\n"

    def _render(self, start: int, end: int, symbol: str) -> str:
        return self._head(start, end, symbol) + "\n".join(self._lines[start - 1 : end])

    def _fits(self, start: int, end: int, symbol: str) -> bool:
        return self._count(self._render(start, end, symbol)) <= CODE_CHUNK_BUDGET_TOKENS

    def _pack(self, units: list[tuple[int, int]], symbol: str, *, by_lines: bool) -> None:
        """Greedily join consecutive ``units`` while the rendered chunk fits.

        A unit too big alone is split finer: a block into its lines
        (``by_lines``), a line into characters.
        """
        current: tuple[int, int] | None = None
        for start, end in units:
            if current is not None and self._fits(current[0], end, symbol):
                current = (current[0], end)
                continue
            if current is not None:
                self.chunks.append(self._render(*current, symbol))
                current = None
            if self._fits(start, end, symbol):
                current = (start, end)
            elif by_lines:
                self._pack([(n, n) for n in range(start, end + 1)], symbol, by_lines=False)
            else:
                self._split_chars(start, symbol)
        if current is not None:
            self.chunks.append(self._render(*current, symbol))

    def _split_chars(self, number: int, symbol: str) -> None:
        """Split one over-budget line into the longest prefixes that fit.

        At least one character per chunk, so the loop always advances; the
        path and symbol caps keep a header small enough that one always fits.
        """
        head = self._head(number, number, symbol)
        line = self._lines[number - 1]
        i = 0
        hint = _GALLOP_START_CHARS
        while i < len(line):
            take = self._longest_fit(head, line, i, hint)
            self.chunks.append(head + line[i : i + take])
            i += take
            hint = take  # the next piece of the same line is usually the same size

    def _longest_fit(self, head: str, line: str, i: int, hint: int) -> int:
        """The longest ``line[i:i + n]`` that fits after ``head``; at least 1.

        Gallops up from ``hint`` and binary-searches the last bracket, so no
        probe is longer than twice the answer (or the hint): a line of length L
        costs O(L log L) characters counted, not O(L^2) as a search bounded by
        the whole remaining line did. Slices only what it counts.
        """
        remaining = len(line) - i

        def fits(size: int) -> bool:
            return self._count(head + line[i : i + size]) <= CODE_CHUNK_BUDGET_TOKENS

        low = 1  # taken even if it does not fit, so the caller always advances
        probe = min(remaining, hint)
        while fits(probe):
            low = probe
            if probe == remaining:
                return probe
            probe = min(remaining, probe * 2)
        high = probe - 1  # ``probe`` does not fit
        while low < high:
            mid = (low + high + 1) // 2
            if fits(mid):
                low = mid
            else:
                high = mid - 1
        return low
