"""Read Chrome's and Safari's local history and bookmarks for `brain ingest-web` (RM-41).

Path (c) of the new-source protocol: the browser's own files, read with the
stdlib (``sqlite3``, ``json``, ``plistlib``). This module only reads and
returns rows; URL normalization, the allowlist and the qualification rule live
in :mod:`brain.ingest.browser`.

**The live database is never opened by SQLite.** :func:`snapshot` copies the
store and its ``-wal`` (never the ``-shm``) into a private ``mkdtemp``
directory (mode 0700, prefix :data:`TEMP_PREFIX`), removed in ``finally``. The
COPY is opened ``mode=ro`` with ``query_only`` and ``trusted_schema=OFF``.
Nothing in the profile directory is created, locked or modified.

**Only regular files are read, and never past a byte bound** (sec-114 F5).
``--profile`` is user-supplied, so a store may be a symlink to ``/dev/zero`` or
a FIFO. Each file is ``lstat``-ed (a regular file, or it is refused), opened
``O_NOFOLLOW | O_NONBLOCK``, ``fstat``-ed (still the same regular file), and
copied by a loop that counts the bytes it actually reads against
:data:`MAX_STORE_BYTES`, whatever the size the stat reported. Any other
``OSError`` (``ENOSPC`` on a full disk included) becomes a one-line
:class:`StoreUnreadableError` naming the error class and code, never a path.

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

**A hostile store cannot hang the read** (sec-114 F6). Every object read must be
a real table in ``sqlite_master`` (a view could hide an unbounded recursive
query), and every query runs under a progress handler that aborts it after
:data:`QUERY_TIMEOUT_SECONDS`. Every column read is checked first (``PRAGMA
table_info``); one missing raises :class:`UnsupportedSchemaError`.

**Bookmarks are walked once per node** (sec-114 F2). A binary plist can
reference one object many times, or itself; the walk skips a node it has
already visited and stops after :data:`MAX_BOOKMARK_NODES`.

**A killed run** (SIGKILL, a power cut) cannot remove its copy, which holds the
whole history. :func:`sweep_stale_snapshots`, run when ``brain ingest-web``
starts, removes this user's :data:`TEMP_PREFIX` directories older than
:data:`STALE_SNAPSHOT_S` (sec-114 F10).

**Errors** are :class:`BrowserStoreError` subclasses with fixed messages: no
path, no file content. A ``PermissionError`` on Safari's files is
:class:`FullDiskAccessError` (macOS keeps ``~/Library/Safari`` behind Full Disk
Access); on Chrome's it is :class:`StoreUnreadableError`.
"""
from __future__ import annotations

import errno
import json
import os
import plistlib
import shutil
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from ..errors import BrainError
from . import stale_tempdirs

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
    "sweep_stale_snapshots",
]

Browser = Literal["chrome", "safari"]
BROWSERS: tuple[Browser, ...] = ("chrome", "safari")

#: The largest history store copied (main file plus WAL), counted as it is read.
MAX_STORE_BYTES = 1024 * 1024 * 1024
#: The largest bookmarks file read.
MAX_BOOKMARKS_BYTES = 64 * 1024 * 1024
#: The most history entries read in one run; past it the read stops and says so.
MAX_ROWS = 200_000
#: Bookmark folders nested deeper than this are skipped and counted.
MAX_BOOKMARK_DEPTH = 64
#: The most bookmark nodes walked; past it the walk stops and says so (sec-114 F2).
MAX_BOOKMARK_NODES = 100_000
#: The longest a history query may run before it is aborted (sec-114 F6).
QUERY_TIMEOUT_SECONDS = 60.0
#: SQLite VM steps between two looks at the query clock.
_PROGRESS_OPS = 10_000
#: The prefix of every snapshot directory, and what the stale sweep matches.
TEMP_PREFIX = "brain-web-"
#: A snapshot directory older than this belongs to a killed run (a live read is minutes).
STALE_SNAPSHOT_S = 3 * 60 * 60
_SQLITE_TIMEOUT_SECONDS = 5.0
_COPY_CHUNK = 1024 * 1024
_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW

_CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)
_SAFARI_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)

#: ``os.write`` (a seam, so a test can stand in for a full disk).
_write: Callable[[int, memoryview], int] = os.write


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
        super().__init__(
            f"unsupported {browser} history schema: a table or column is missing or not a table"
        )


class StoreUnreadableError(BrowserStoreError):
    def __init__(self, what: str = "the history store", reason: str | None = None) -> None:
        detail = f" ({reason})" if reason else ""
        super().__init__(f"{what} could not be read{detail}; close the browser and retry")


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
    bookmarks_truncated: bool = False


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


def sweep_stale_snapshots(
    *, root: Path | None = None, now: float | None = None, uid: int | None = None
) -> int:
    """Remove this user's snapshot directories a killed run left behind; the count."""
    return stale_tempdirs.sweep_stale_tempdirs(
        TEMP_PREFIX, max_age_s=STALE_SNAPSHOT_S, root=root, now=now, uid=uid
    )


def read_browser(browser: Browser, profile: Path, since: datetime) -> BrowserRows:
    """The history entries visited since ``since`` and every bookmark of ``browser``."""
    if browser not in _LAYOUTS:
        raise ValueError(f"unknown browser {browser!r}")
    layout = _LAYOUTS[browser]
    try:
        history, truncated, bad_rows = _read_history(browser, profile / layout.history, since)
        bookmarks, bad_marks, marks_truncated = _read_bookmarks(
            browser, profile / layout.bookmarks
        )
    except PermissionError:
        if browser == "safari":
            raise FullDiskAccessError() from None
        raise StoreUnreadableError(reason="PermissionError") from None
    except OSError as exc:
        raise StoreUnreadableError(reason=_os_reason(exc)) from None
    return BrowserRows(
        history=history,
        bookmarks=bookmarks,
        truncated=truncated,
        malformed=bad_rows + bad_marks,
        bookmarks_truncated=marks_truncated,
    )


def _os_reason(exc: OSError) -> str:
    """``OSError ENOSPC``: the class and the errno name, never the message (it holds a path)."""
    code = errno.errorcode.get(exc.errno) if exc.errno is not None else None
    return f"{type(exc).__name__} {code}" if code else type(exc).__name__


def _stat(path: Path) -> os.stat_result:
    """``lstat`` of ``path`` (a seam, so tests can stand in for macOS's EPERM)."""
    return os.lstat(path)


@contextmanager
def _open_regular(path: Path, what: str) -> Iterator[tuple[int, int]]:
    """``(fd, size)`` of ``path``, opened only when it is a regular file (sec-114 F5)."""
    before = _stat(path)
    if not stat.S_ISREG(before.st_mode):
        raise StoreUnreadableError(what, reason="not a regular file")
    fd = os.open(path, _OPEN_FLAGS)
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise StoreUnreadableError(what, reason="not a regular file")
        yield fd, after.st_size
    finally:
        os.close(fd)


def _copy_bounded(source: int, destination: Path, budget: int) -> int:
    """Copy ``source`` to a new ``destination``; the bytes copied, never more than ``budget``."""
    out = os.open(destination, _CREATE_FLAGS, 0o600)
    copied = 0
    try:
        while chunk := os.read(source, _COPY_CHUNK):
            copied += len(chunk)
            if copied > budget:
                raise StoreTooLargeError()
            view = memoryview(chunk)
            while view:
                view = view[_write(out, view):]
    finally:
        os.close(out)
    return copied


@contextmanager
def snapshot(store: Path) -> Iterator[Path]:
    """A private copy of ``store`` and its ``-wal``; the directory is removed on exit."""
    directory = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        copy = directory / store.name
        budget = MAX_STORE_BYTES
        for source, target, required in (
            (store, copy, True),
            (store.with_name(f"{store.name}-wal"), copy.with_name(f"{copy.name}-wal"), False),
        ):
            try:
                with _open_regular(source, "the history store") as (fd, size):
                    if size > budget:
                        raise StoreTooLargeError()
                    budget -= _copy_bounded(fd, target, budget)
            except FileNotFoundError:
                if required:
                    raise
        yield copy
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _read_history(
    browser: str, store: Path, since: datetime
) -> tuple[tuple[HistoryRow, ...], bool, int]:
    try:
        _stat(store)
    except FileNotFoundError:
        raise HistoryNotFoundError() from None
    for attempt in (1, 2):
        try:
            with snapshot(store) as copy:
                raw = _query(browser, copy, since)
            break
        except sqlite3.DatabaseError:
            if attempt == 2:
                raise StoreUnreadableError() from None
    return _parse_history(browser, raw)


def _query(browser: str, copy: Path, since: datetime) -> Sequence[Sequence[Any]]:
    conn = sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True, timeout=_SQLITE_TIMEOUT_SECONDS)
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    expired: list[bool] = []

    def past_deadline() -> int:
        if time.monotonic() > deadline:
            expired.append(True)
            return 1
        return 0

    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA query_only=ON")
        conn.set_progress_handler(past_deadline, _PROGRESS_OPS)
        if browser == "chrome":
            return list(_query_chrome(conn, since))
        return list(_query_safari(conn, since))
    except sqlite3.OperationalError:
        if expired:
            raise StoreUnreadableError(
                reason=f"the query took longer than {QUERY_TIMEOUT_SECONDS:g} s"
            ) from None
        raise
    finally:
        conn.close()


def _require_tables(conn: sqlite3.Connection, browser: str) -> None:
    """Every object read is a real table with every column read (sec-114 F6)."""
    columns = _LAYOUTS[browser].columns
    marks = ", ".join("?" for _ in columns)
    kinds = dict(
        conn.execute(
            f"SELECT name, type FROM sqlite_master WHERE name IN ({marks})", tuple(columns)
        ).fetchall()
    )
    for table, wanted in columns.items():
        if kinds.get(table) != "table":
            raise UnsupportedSchemaError(browser)
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not set(wanted) <= present:
            raise UnsupportedSchemaError(browser)


def _query_chrome(conn: sqlite3.Connection, since: datetime) -> Sequence[Sequence[Any]]:
    _require_tables(conn, "chrome")
    return conn.execute(_CHROME_QUERY, (to_chrome_time(since), MAX_ROWS + 1)).fetchall()


def _query_safari(conn: sqlite3.Connection, since: datetime) -> Sequence[Sequence[Any]]:
    _require_tables(conn, "safari")
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


def _read_bookmarks(browser: str, path: Path) -> tuple[tuple[BookmarkRow, ...], int, bool]:
    """Every bookmark, the malformed-node count, and whether the walk stopped at its cap."""
    what = "the bookmarks file"
    try:
        with _open_regular(path, what) as (fd, _size):
            data = _read_bounded(fd, MAX_BOOKMARKS_BYTES + 1)
    except FileNotFoundError:
        return (), 0, False
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


def _read_bounded(fd: int, limit: int) -> bytes:
    """At most ``limit`` bytes of ``fd``."""
    parts: list[bytes] = []
    remaining = limit
    while remaining > 0 and (chunk := os.read(fd, min(remaining, _COPY_CHUNK))):
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def _walk(roots: list[Any], visit: Any) -> tuple[tuple[BookmarkRow, ...], int, bool]:
    """Walk the tree iteratively, each node once (sec-114 F2).

    ``visit(node)`` → ``(bookmark | None, children | None, ok)``. A node already
    visited (a shared plist reference, or a cycle) is skipped; past
    :data:`MAX_BOOKMARK_NODES` the walk stops and reports it.
    """
    found: list[BookmarkRow] = []
    malformed = 0
    seen: set[int] = set()
    walked = 0
    stack: list[tuple[Any, int]] = [(node, 1) for node in reversed(roots)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, (dict, list)):
            if id(node) in seen:
                continue
            seen.add(id(node))
        walked += 1
        if walked > MAX_BOOKMARK_NODES:
            return tuple(found), malformed, True
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
    return tuple(found), malformed, False


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
