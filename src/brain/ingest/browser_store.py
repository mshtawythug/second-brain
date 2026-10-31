"""Read Chrome's and Safari's local history and bookmarks for `brain ingest-web` (RM-41).

Path (c) of the new-source protocol: the browser's own files, read with the
stdlib (``sqlite3``, ``json``, ``plistlib``). This module only reads and
returns rows; URL normalization, the allowlist and the qualification rule live
in :mod:`brain.ingest.browser`.

**The live database is never opened by SQLite.** :func:`snapshot` byte-copies
the store and its ``-wal`` (never the ``-shm``) into a private ``mkdtemp``
directory (mode 0700), removed in ``finally``. The COPY is opened
``mode=ro`` with ``query_only`` and ``trusted_schema=OFF``. Nothing in the
profile directory is created, locked or modified.

**Why ``mode=ro`` and not ``immutable=1`` (RM-41.D2).** The browser keeps its
newest visits in the WAL until a checkpoint. ``immutable=1`` tells SQLite the
file cannot change, and it then skips the WAL, which silently drops exactly those
visits (measured 2026-10-31 on SQLite 3.54: a committed, uncheckpointed row was
invisible under ``immutable=1`` and visible under ``mode=ro``). ``mode=ro`` on a
private copy lets SQLite rebuild the ``-shm`` inside the temporary directory.

**A torn copy.** The browser may write while the copy runs. SQLite discards a
torn WAL tail by its frame checksums; a torn main file raises
``sqlite3.DatabaseError``, so a read is retried once on a fresh snapshot, then
fails as :class:`StoreUnreadableError`.

**Schema drift fails loudly.** Every table and column read is checked first
(``PRAGMA table_info``); one missing raises :class:`UnsupportedSchemaError`.

**Errors** are :class:`BrowserStoreError` subclasses with fixed messages: no
path, no file content. A ``PermissionError`` on Safari's files is
:class:`FullDiskAccessError` (macOS keeps ``~/Library/Safari`` behind Full Disk
Access); on Chrome's it is :class:`StoreUnreadableError`.
"""
from __future__ import annotations

import json
import plistlib
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from ..errors import BrainError

__all__ = [
    "BookmarkRow",
    "Browser",
    "BrowserRows",
    "BrowserStoreError",
    "FullDiskAccessError",
    "HistoryNotFoundError",
    "HistoryRow",
    "StoreTooLargeError",
    "StoreUnreadableError",
    "UnsupportedSchemaError",
    "default_profile",
    "read_browser",
    "snapshot",
]

Browser = Literal["chrome", "safari"]
BROWSERS: tuple[Browser, ...] = ("chrome", "safari")

#: The largest history store copied (main file plus WAL).
MAX_STORE_BYTES = 1024 * 1024 * 1024
#: The largest bookmarks file read.
MAX_BOOKMARKS_BYTES = 64 * 1024 * 1024
#: The most history entries read in one run; past it the read stops and says so.
MAX_ROWS = 200_000
#: Bookmark folders nested deeper than this are skipped and counted.
MAX_BOOKMARK_DEPTH = 64
_SQLITE_TIMEOUT_SECONDS = 5.0

_CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)
_SAFARI_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


class BrowserStoreError(BrainError):
    """A browser store could not be read; the message is fixed and names no path."""


class HistoryNotFoundError(BrowserStoreError):
    def __init__(self) -> None:
        super().__init__("no browser history found in the profile directory")


class FullDiskAccessError(BrowserStoreError):
    def __init__(self) -> None:
        super().__init__(
            "Safari's history needs Full Disk Access: grant it to your terminal in System "
            "Settings > Privacy & Security > Full Disk Access (docs/guides/browser-history.md)"
        )


class UnsupportedSchemaError(BrowserStoreError):
    def __init__(self, browser: str) -> None:
        super().__init__(f"unsupported {browser} history schema: a table or column is missing")


class StoreUnreadableError(BrowserStoreError):
    def __init__(self, what: str = "the history store") -> None:
        super().__init__(f"{what} could not be read; close the browser and retry")


class StoreTooLargeError(BrowserStoreError):
    def __init__(self, what: str = "the history store") -> None:
        super().__init__(f"{what} is larger than this command reads")


@dataclass(frozen=True)
class HistoryRow:
    """One history entry's visits inside the window."""

    url: str
    title: str
    visits: int
    first_visit: datetime
    last_visit: datetime


@dataclass(frozen=True)
class BookmarkRow:
    url: str
    title: str


@dataclass(frozen=True)
class BrowserRows:
    history: tuple[HistoryRow, ...]
    bookmarks: tuple[BookmarkRow, ...]
    truncated: bool = False
    malformed: int = 0


@dataclass(frozen=True)
class _Layout:
    history: str
    bookmarks: str
    columns: dict[str, tuple[str, ...]]


_LAYOUTS: dict[str, _Layout] = {
    "chrome": _Layout(
        history="History",
        bookmarks="Bookmarks",
        columns={"urls": ("id", "url", "title"), "visits": ("url", "visit_time")},
    ),
    "safari": _Layout(
        history="History.db",
        bookmarks="Bookmarks.plist",
        columns={
            "history_items": ("id", "url"),
            "history_visits": ("history_item", "visit_time", "title"),
        },
    ),
}

_CHROME_QUERY = """
SELECT u.url, u.title, COUNT(*), MIN(v.visit_time), MAX(v.visit_time)
FROM urls u JOIN visits v ON v.url = u.id
WHERE v.visit_time >= ?
GROUP BY u.id
LIMIT ?
"""
_SAFARI_QUERY = """
SELECT i.url,
       (SELECT t.title FROM history_visits t
        WHERE t.history_item = i.id AND t.title IS NOT NULL AND t.title != ''
        ORDER BY t.visit_time DESC LIMIT 1),
       COUNT(*), MIN(v.visit_time), MAX(v.visit_time)
FROM history_items i JOIN history_visits v ON v.history_item = i.id
WHERE v.visit_time >= ?
GROUP BY i.id
LIMIT ?
"""


def default_profile(browser: Browser, home: Path) -> Path:
    """The macOS location of ``browser``'s profile under ``home`` (RM-41.D9)."""
    if browser == "chrome":
        return home / "Library" / "Application Support" / "Google" / "Chrome" / "Default"
    return home / "Library" / "Safari"


def to_chrome_time(moment: datetime) -> int:
    return (moment - _CHROME_EPOCH) // _MICROSECOND


def from_chrome_time(value: int) -> datetime:
    return _CHROME_EPOCH + timedelta(microseconds=value)


def to_safari_time(moment: datetime) -> float:
    return (moment - _SAFARI_EPOCH).total_seconds()


def from_safari_time(value: float) -> datetime:
    return _SAFARI_EPOCH + timedelta(seconds=value)


def read_browser(browser: Browser, profile: Path, since: datetime) -> BrowserRows:
    """The history entries visited since ``since`` and every bookmark of ``browser``."""
    if browser not in _LAYOUTS:
        raise ValueError(f"unknown browser {browser!r}")
    layout = _LAYOUTS[browser]
    try:
        history, truncated, bad_rows = _read_history(browser, profile / layout.history, since)
        bookmarks, bad_marks = _read_bookmarks(browser, profile / layout.bookmarks)
    except PermissionError:
        if browser == "safari":
            raise FullDiskAccessError() from None
        raise StoreUnreadableError() from None
    return BrowserRows(
        history=history, bookmarks=bookmarks, truncated=truncated, malformed=bad_rows + bad_marks
    )


def _stat(path: Path) -> int:
    """The size of ``path`` (a seam, so tests can stand in for macOS's EPERM)."""
    return path.stat().st_size


@contextmanager
def snapshot(store: Path) -> Iterator[Path]:
    """A private copy of ``store`` and its ``-wal``; the directory is removed on exit."""
    directory = Path(tempfile.mkdtemp(prefix="brain-web-"))
    try:
        copy = directory / store.name
        shutil.copyfile(store, copy)
        wal = store.with_name(f"{store.name}-wal")
        if wal.exists():
            shutil.copyfile(wal, copy.with_name(f"{copy.name}-wal"))
        yield copy
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _read_history(
    browser: str, store: Path, since: datetime
) -> tuple[tuple[HistoryRow, ...], bool, int]:
    try:
        size = _stat(store)
    except FileNotFoundError:
        raise HistoryNotFoundError() from None
    wal = store.with_name(f"{store.name}-wal")
    if wal.exists():
        size += _stat(wal)
    if size > MAX_STORE_BYTES:
        raise StoreTooLargeError()
    for attempt in (1, 2):
        try:
            with snapshot(store) as copy:
                raw = _query(browser, copy, since)
            break
        except UnsupportedSchemaError:
            raise
        except sqlite3.DatabaseError:
            if attempt == 2:
                raise StoreUnreadableError() from None
    return _parse_history(browser, raw)


def _query(browser: str, copy: Path, since: datetime) -> Sequence[Sequence[Any]]:
    conn = sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True, timeout=_SQLITE_TIMEOUT_SECONDS)
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA query_only=ON")
        if browser == "chrome":
            return list(_query_chrome(conn, since))
        return list(_query_safari(conn, since))
    finally:
        conn.close()


def _require_columns(conn: sqlite3.Connection, browser: str) -> None:
    for table, columns in _LAYOUTS[browser].columns.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not set(columns) <= present:
            raise UnsupportedSchemaError(browser)


def _query_chrome(conn: sqlite3.Connection, since: datetime) -> Sequence[Sequence[Any]]:
    _require_columns(conn, "chrome")
    return conn.execute(_CHROME_QUERY, (to_chrome_time(since), MAX_ROWS + 1)).fetchall()


def _query_safari(conn: sqlite3.Connection, since: datetime) -> Sequence[Sequence[Any]]:
    _require_columns(conn, "safari")
    return conn.execute(_SAFARI_QUERY, (to_safari_time(since), MAX_ROWS + 1)).fetchall()


def _parse_history(
    browser: str, raw: Sequence[Sequence[Any]]
) -> tuple[tuple[HistoryRow, ...], bool, int]:
    truncated = len(raw) > MAX_ROWS
    rows: list[HistoryRow] = []
    malformed = 0
    for record in raw[:MAX_ROWS]:
        row = _history_row(browser, record)
        if row is None:
            malformed += 1
        else:
            rows.append(row)
    return tuple(rows), truncated, malformed


def _history_row(browser: str, record: Sequence[Any]) -> HistoryRow | None:
    url, title, visits, first, last = record
    if not isinstance(url, str) or not (title is None or isinstance(title, str)):
        return None
    convert = from_chrome_time if browser == "chrome" else from_safari_time
    try:
        first_visit, last_visit = convert(first), convert(last)
    except (OverflowError, ValueError, TypeError):
        return None
    return HistoryRow(
        url=url, title=title or "", visits=int(visits), first_visit=first_visit,
        last_visit=last_visit,
    )


def _read_bookmarks(browser: str, path: Path) -> tuple[tuple[BookmarkRow, ...], int]:
    """Every bookmark and the count of malformed nodes; none when the file is absent."""
    what = "the bookmarks file"
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_BOOKMARKS_BYTES + 1)
    except FileNotFoundError:
        return (), 0
    if len(data) > MAX_BOOKMARKS_BYTES:
        raise StoreTooLargeError(what)
    try:
        tree = json.loads(data) if browser == "chrome" else plistlib.loads(data)
    except (ValueError, RecursionError, plistlib.InvalidFileException):
        # ValueError covers json.JSONDecodeError and UnicodeDecodeError.
        raise StoreUnreadableError(what) from None
    if not isinstance(tree, dict):
        raise StoreUnreadableError(what)
    if browser == "chrome":
        roots = tree.get("roots")
        if not isinstance(roots, dict):
            raise StoreUnreadableError(what)
        return _walk(list(roots.values()), _chrome_node)
    return _walk([tree], _safari_node)


def _walk(roots: list[Any], visit: Any) -> tuple[tuple[BookmarkRow, ...], int]:
    """Walk the tree iteratively; ``visit(node)`` → ``(bookmark | None, children | None, ok)``."""
    found: list[BookmarkRow] = []
    malformed = 0
    stack: list[tuple[Any, int]] = [(node, 1) for node in reversed(roots)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_BOOKMARK_DEPTH:
            malformed += 1
            continue
        bookmark, children, ok = visit(node)
        if not ok:
            malformed += 1
            continue
        if bookmark is not None:
            found.append(bookmark)
        for child in reversed(children or []):
            stack.append((child, depth + 1))
    return tuple(found), malformed


_Visit = tuple[BookmarkRow | None, list[Any] | None, bool]


def _chrome_node(node: Any) -> _Visit:
    if not isinstance(node, dict):
        return None, None, False
    if node.get("type") == "url":
        url, name = node.get("url"), node.get("name", "")
        if not isinstance(url, str) or not isinstance(name, str):
            return None, None, False
        return BookmarkRow(url, name), None, True
    children = node.get("children", [])
    if not isinstance(children, list):
        return None, None, False
    return None, children, True


def _safari_node(node: Any) -> _Visit:
    if not isinstance(node, dict):
        return None, None, False
    kind = node.get("WebBookmarkType")
    if kind == "WebBookmarkTypeLeaf":
        url = node.get("URLString")
        if not isinstance(url, str):
            return None, None, False
        info = node.get("URIDictionary")
        title = info.get("title") if isinstance(info, dict) else None
        return BookmarkRow(url, title if isinstance(title, str) else ""), None, True
    if kind == "WebBookmarkTypeList":
        children = node.get("Children", [])
        if not isinstance(children, list):
            return None, None, False
        return None, children, True
    return None, None, True

