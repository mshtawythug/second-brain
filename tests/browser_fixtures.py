"""Synthetic browser stores for the `brain ingest-web` tests (RM-41). Never a real profile.

Each builder writes a profile directory under a test's ``tmp_path`` (or a scratch
directory) holding only the tables and columns :mod:`brain.ingest.browser_store`
reads, so a store schema change shows up as a fixture edit. Every URL is on
``example.com`` / ``example.org`` / ``example.net``.

**The WAL.** A browser keeps its newest visits in the write-ahead log until a
checkpoint. ``wal_pages`` rows are written after a checkpoint, and the files are
copied into the profile while the staging connection is still open, so the
profile holds a ``-wal`` with frames the main file does not have, which is what a
live browser's profile looks like. (Closing the last connection would checkpoint
them into the main file.)
"""
from __future__ import annotations

import json
import plistlib
import shutil
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)
SAFARI_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
SINCE = datetime(2026, 10, 1, tzinfo=UTC)
DAY = timedelta(days=1)


@dataclass(frozen=True)
class Page:
    """One history entry: a URL, its title, and when it was visited."""

    url: str
    title: str = ""
    visits: Sequence[datetime] = field(default_factory=tuple)


def chrome_time(moment: datetime) -> int:
    """Microseconds since 1601-01-01 UTC (Chrome's ``visit_time``)."""
    return (moment - CHROME_EPOCH) // timedelta(microseconds=1)


def safari_time(moment: datetime) -> float:
    """Seconds since 2001-01-01 UTC (Safari's ``visit_time``)."""
    return (moment - SAFARI_EPOCH).total_seconds()


_CHROME_SCHEMA = (
    "CREATE TABLE urls(id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR, "
    "visit_count INTEGER DEFAULT 0, last_visit_time INTEGER DEFAULT 0)",
    "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER)",
)
_SAFARI_SCHEMA = (
    "CREATE TABLE history_items(id INTEGER PRIMARY KEY, url TEXT, "
    "domain_expansion TEXT, visit_count INTEGER DEFAULT 0)",
    "CREATE TABLE history_visits(id INTEGER PRIMARY KEY, history_item INTEGER, "
    "visit_time REAL, title TEXT)",
)


def _insert_chrome(conn: sqlite3.Connection, page: Page) -> None:
    cursor = conn.execute(
        "INSERT INTO urls(url, title, visit_count) VALUES (?, ?, ?)",
        (page.url, page.title, len(page.visits)),
    )
    for moment in page.visits:
        conn.execute(
            "INSERT INTO visits(url, visit_time) VALUES (?, ?)",
            (cursor.lastrowid, chrome_time(moment)),
        )


def _insert_safari(conn: sqlite3.Connection, page: Page) -> None:
    cursor = conn.execute(
        "INSERT INTO history_items(url, visit_count) VALUES (?, ?)",
        (page.url, len(page.visits)),
    )
    for moment in page.visits:
        conn.execute(
            "INSERT INTO history_visits(history_item, visit_time, title) VALUES (?, ?, ?)",
            (cursor.lastrowid, safari_time(moment), page.title),
        )


def _build_store(
    path: Path,
    schema: Sequence[str],
    insert: Any,
    pages: Sequence[Page],
    wal_pages: Sequence[Page],
) -> Path:
    """Write the store at ``path`` (and ``path-wal`` when ``wal_pages`` is non-empty)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / f".staging-{path.name}"
    staging.mkdir()
    staged = staging / path.name
    conn = sqlite3.connect(staged)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        for statement in schema:
            conn.execute(statement)
        for page in pages:
            insert(conn, page)
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for page in wal_pages:
            insert(conn, page)
        conn.commit()
        shutil.copyfile(staged, path)
        wal = staged.with_name(f"{staged.name}-wal")
        if wal_pages:
            shutil.copyfile(wal, path.with_name(f"{path.name}-wal"))
    finally:
        conn.close()
        shutil.rmtree(staging)
    return path


def chrome_profile(
    profile: Path,
    pages: Sequence[Page] = (),
    *,
    wal_pages: Sequence[Page] = (),
    bookmarks: Sequence[tuple[str, str]] | None = None,
) -> Path:
    """A Chrome profile directory: ``History`` (+ ``-wal``) and, optionally, ``Bookmarks``."""
    _build_store(profile / "History", _CHROME_SCHEMA, _insert_chrome, pages, wal_pages)
    if bookmarks is not None:
        write_chrome_bookmarks(profile, chrome_bookmarks_tree(bookmarks))
    return profile


def chrome_bookmarks_tree(bookmarks: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """Chrome's ``Bookmarks`` JSON with ``(url, name)`` pairs, the last one in a sub-folder."""
    leaves = [{"type": "url", "url": url, "name": name} for url, name in bookmarks]
    nested = leaves[-1:]
    return {
        "version": 1,
        "roots": {
            "bookmark_bar": {
                "type": "folder",
                "name": "Bookmarks bar",
                "children": [
                    *leaves[:-1],
                    {"type": "folder", "name": "Reading", "children": nested},
                ],
            },
            "other": {"type": "folder", "name": "Other", "children": []},
            "synced": {"type": "folder", "name": "Mobile", "children": []},
        },
    }


def write_chrome_bookmarks(profile: Path, tree: Any) -> Path:
    path = profile / "Bookmarks"
    path.write_text(json.dumps(tree), encoding="utf-8")
    return path


def safari_profile(
    profile: Path,
    pages: Sequence[Page] = (),
    *,
    wal_pages: Sequence[Page] = (),
    bookmarks: Sequence[tuple[str, str]] | None = None,
) -> Path:
    """A Safari directory: ``History.db`` (+ ``-wal``) and, optionally, ``Bookmarks.plist``."""
    _build_store(profile / "History.db", _SAFARI_SCHEMA, _insert_safari, pages, wal_pages)
    if bookmarks is not None:
        write_safari_bookmarks(profile, safari_bookmarks_tree(bookmarks))
    return profile


def safari_bookmarks_tree(bookmarks: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """Safari's ``Bookmarks.plist`` with ``(url, title)`` leaves; the last in the Reading List."""
    leaves = [
        {
            "WebBookmarkType": "WebBookmarkTypeLeaf",
            "URLString": url,
            "URIDictionary": {"title": title},
        }
        for url, title in bookmarks
    ]
    return {
        "WebBookmarkType": "WebBookmarkTypeList",
        "Children": [
            {
                "WebBookmarkType": "WebBookmarkTypeList",
                "Title": "BookmarksBar",
                "Children": leaves[:-1],
            },
            {
                "WebBookmarkType": "WebBookmarkTypeList",
                "Title": "com.apple.ReadingList",
                "Children": leaves[-1:],
            },
        ],
    }


def write_safari_bookmarks(profile: Path, tree: Any) -> Path:
    path = profile / "Bookmarks.plist"
    path.write_bytes(plistlib.dumps(tree, fmt=plistlib.FMT_BINARY))
    return path


def snapshot_of(directory: Path) -> dict[str, tuple[int, int]]:
    """``{name: (size, mtime_ns)}`` for every entry, to prove a run left a profile alone."""
    return {
        entry.name: (entry.stat().st_size, entry.stat().st_mtime_ns)
        for entry in sorted(directory.iterdir())
    }
