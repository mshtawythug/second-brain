"""Notion "Markdown & CSV" export: page ids from file names, the export plan, pages (RM-35).

Pure: this module never opens a file. :mod:`brain.ingest.notion_archive` lists
and reads the export (a folder or a zip, untrusted), and
:mod:`brain.cli_ingest_notion` writes the documents.

**Ids (protocol point 3).** Notion names every page ``<Title> <32-hex id>.md``
and the folder holding its sub-pages ``<Title> <id>/``. The id is the dedup
key, ``(notion, <id>)``, so a re-export to a new folder updates each page in
place; the documents carry ``source_path=None``. A ``.md`` whose stem does not
end in a space and exactly 32 hex digits is skipped and counted, never guessed
(AC4), and so is every copy of an id that occurs twice in one export.

**Databases (plan ruling R1).** A database is exported as ``<Name> <id>.csv``
(and, in newer exports, ``<Name> <id>_all.csv``) beside a folder
``<Name> <id>/`` holding one row page, with its own id, per row. A CSV row has
no page id, so rows are ingested from their row pages, and only with
``--with-databases`` (AC3). The CSVs themselves are never stored.

**Pages (rulings R2, R3).** The title is the page's ``# `` first line, else the
file name without its id (AC1), else :data:`UNTITLED`; control and format
characters are removed and it is cut at :data:`MAX_TITLE_CHARS`. The body is the text as
exported, NULs removed. A database row's ``Last edited time`` property, when it
parses, becomes ``metadata.last_edited`` and ``metadata.date`` (``sent_at``).
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath

from . import ExtractedDoc
from .github_docs import one_line

SOURCE_KIND = "notion"
CONTENT_TYPE = "notion_page"
UNTITLED = "Untitled"
#: A title is someone else's text and is stored as ``documents.title``; an
#: 8 MiB heading must not become one.
MAX_TITLE_CHARS = 300

PAGE_SUFFIX = ".md"
TABLE_SUFFIX = ".csv"
ARCHIVE_SUFFIX = ".zip"
#: Newer exports write a second CSV of every row beside the view's own.
ALL_ROWS_MARKER = "_all"
MACOS_METADATA_DIR = "__MACOSX"

#: What the export plan skips, by reason; the command prints each count.
SKIP_ATTACHMENT = "attachment"
SKIP_NO_ID = "no_id"
SKIP_DUPLICATE_ID = "duplicate_id"
SKIP_DATABASE_ROW = "database_row"
SKIP_ARCHIVE = "archive"

_NAME_RE = re.compile(r"^(?:(?P<title>.*) )?(?P<id>[0-9A-Fa-f]{32})$")
_TITLE_RE = re.compile(r"^# (?P<title>.*)$")
_PROPERTY_RE = re.compile(r"^(?P<key>[^:\n]{1,64}): (?P<value>.*)$")
_LAST_EDITED_KEYS = frozenset({"last edited time", "last edited"})
_DATE_FORMATS = ("%B %d, %Y %I:%M %p", "%B %d, %Y")


@dataclass(frozen=True)
class ExportFile:
    """One regular file of an export: its path under the export root, and its size."""

    path: PurePosixPath
    size: int


@dataclass(frozen=True)
class PageRef:
    """A page the plan will ingest, before its text is read."""

    file: ExportFile
    page_id: str
    name_title: str
    parent_id: str | None
    database_id: str | None


@dataclass(frozen=True)
class DatabaseRef:
    """A database: the CSV to count its rows from, and how many row pages it has."""

    database_id: str
    table: ExportFile
    row_pages: int


@dataclass(frozen=True)
class ExportPlan:
    """What one export yields: the pages to ingest, its databases, and the skip counts."""

    pages: tuple[PageRef, ...]
    databases: tuple[DatabaseRef, ...]
    skipped: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class NotionPage:
    """One page, ready for ``ingest_document``."""

    page_id: str
    doc: ExtractedDoc
    has_last_edited: bool
    last_edited: datetime | None


def split_name(stem: str) -> tuple[str, str] | None:
    """``(title, lowercase id)`` from ``<Title> <32-hex id>``, or ``None``: never a guess."""
    match = _NAME_RE.match(stem)
    if match is None:
        return None
    return (match["title"] or "").strip(), match["id"].lower()


def _id_of(name: str) -> str | None:
    parsed = split_name(name)
    return None if parsed is None else parsed[1]


def _table_key(file: ExportFile) -> tuple[PurePosixPath, str] | None:
    """``(the database folder, its id)`` that a CSV names, or ``None``."""
    stem = file.path.stem
    if stem.endswith(ALL_ROWS_MARKER):
        stem = stem[: -len(ALL_ROWS_MARKER)]
    db_id = _id_of(stem)
    return None if db_id is None else (file.path.parent / stem, db_id)


def _is_resource_fork(path: PurePosixPath) -> bool:
    """macOS metadata a Finder-made zip adds (``__MACOSX/``, ``._<name>``), never a page.

    ``._Title <id>.md`` carries the real page's id, so reading it as a page
    would make both copies a counted duplicate and drop the real one.
    """
    return path.name.startswith("._") or MACOS_METADATA_DIR in path.parts


def _parent_id(path: PurePosixPath) -> str | None:
    for folder in reversed(path.parent.parts):
        folder_id = _id_of(folder)
        if folder_id is not None:
            return folder_id
    return None


def plan_export(files: Iterable[ExportFile], *, with_databases: bool) -> ExportPlan:
    """Sort an export's files into pages, databases and counted skips (parents first)."""
    ordered = sorted(files, key=lambda f: str(f.path))
    skipped: Counter[str] = Counter()
    pages: list[tuple[ExportFile, str, str]] = []
    tables: dict[PurePosixPath, tuple[str, ExportFile]] = {}
    for file in ordered:
        suffix = file.path.suffix.lower()
        if _is_resource_fork(file.path):
            skipped[SKIP_ATTACHMENT] += 1
        elif suffix == PAGE_SUFFIX:
            parsed = split_name(file.path.stem)
            if parsed is None:
                skipped[SKIP_NO_ID] += 1
            else:
                pages.append((file, *parsed))
        elif suffix == TABLE_SUFFIX:
            key = _table_key(file)
            if key is None:
                skipped[SKIP_NO_ID] += 1
            else:
                tables[key[0]] = (key[1], file)  # ``_all`` sorts after, so it wins
        elif suffix == ARCHIVE_SUFFIX:
            skipped[SKIP_ARCHIVE] += 1
        else:
            skipped[SKIP_ATTACHMENT] += 1
    copies = Counter(page_id for _f, _t, page_id in pages)
    rows: Counter[PurePosixPath] = Counter()
    refs: list[PageRef] = []
    for file, title, page_id in pages:
        if copies[page_id] > 1:
            skipped[SKIP_DUPLICATE_ID] += 1
            continue
        folder = file.path.parent
        database_id = tables[folder][0] if folder in tables else None
        if database_id is not None:
            rows[folder] += 1
            if not with_databases:
                skipped[SKIP_DATABASE_ROW] += 1
                continue
        refs.append(PageRef(file, page_id, title, _parent_id(file.path), database_id))
    databases = tuple(
        DatabaseRef(db_id, table, rows[folder]) for folder, (db_id, table) in tables.items()
    )
    return ExportPlan(tuple(refs), databases, dict(skipped))


def _first_line_title(lines: list[str]) -> str | None:
    match = _TITLE_RE.match(lines[0]) if lines else None
    return match["title"].strip() if match else None


def _parse_date(value: str) -> datetime | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def last_edited(text: str) -> tuple[bool, datetime | None]:
    """Whether the property block under the title has a last-edited line, and its value.

    The block is the run of ``Key: value`` lines after the ``# `` title and its
    blank lines; a value that does not parse is ``(True, None)``.
    """
    lines = text.strip().splitlines()
    if _first_line_title(lines) is None:
        return False, None
    rest = lines[1:]
    while rest and not rest[0].strip():
        rest = rest[1:]
    for line in rest:
        match = _PROPERTY_RE.match(line)
        if match is None:
            break
        if match["key"].strip().lower() in _LAST_EDITED_KEYS:
            return True, _parse_date(match["value"].strip())
    return False, None


def _clean_title(raw: str) -> str:
    """``raw`` on one line, control and format characters removed, cut to the cap.

    An ESC or OSC sequence in a title could drive the terminal of anything that
    prints it (the secret guard's notice did); ``""`` when nothing is left.
    """
    return one_line(raw)[:MAX_TITLE_CHARS].strip()


def build_page(ref: PageRef, text: str) -> NotionPage:
    """The document for one page's text (rulings R2, R3)."""
    body = text.replace("\x00", "").strip()
    heading = _first_line_title(body.splitlines()) or ""
    title = _clean_title(heading) or _clean_title(ref.name_title) or UNTITLED
    has_edited, edited = last_edited(body)
    metadata: dict[str, str] = {"notion_page_id": ref.page_id}
    if ref.parent_id is not None:
        metadata["notion_parent_id"] = ref.parent_id
    if ref.database_id is not None:
        metadata["notion_database_id"] = ref.database_id
    if edited is not None:
        metadata["last_edited"] = metadata["date"] = edited.isoformat()
    doc = ExtractedDoc(
        title=title,
        content=body,
        content_type=CONTENT_TYPE,
        source_path=None,
        metadata=metadata,
    )
    return NotionPage(ref.page_id, doc, has_edited, edited)


def csv_row_count(text: str) -> int | None:
    """Data rows in a database CSV (blank rows ignored), or ``None`` if it does not parse."""
    try:
        records = list(csv.reader(io.StringIO(text), strict=True))
    except csv.Error:
        return None
    return sum(1 for record in records[1:] if any(cell.strip() for cell in record))
