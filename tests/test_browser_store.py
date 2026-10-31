"""Reading Chrome and Safari stores for `brain ingest-web` (RM-41), from synthetic profiles only.

The invariants: the live store is byte-copied and never opened by SQLite (the
profile directory is unchanged, and no ``-shm`` appears in it); the copy is opened
``mode=ro`` so rows still in the WAL are read (RM-41.D2: ``immutable=1`` hides
them); every failure maps to a fixed, path-free error class.
"""
from __future__ import annotations

import errno
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from brain.ingest import browser_store as bs
from brain.ingest.browser_store import (
    BookmarkRow,
    FullDiskAccessError,
    HistoryNotFoundError,
    HistoryRow,
    StoreTooLargeError,
    StoreUnreadableError,
    UnsupportedSchemaError,
    read_browser,
)
from tests import browser_fixtures as fx

A = "https://example.com/a"
B = "https://example.org/b"
IN_WINDOW = (fx.NOW - 3 * fx.DAY, fx.NOW - fx.DAY, fx.NOW)
BEFORE_WINDOW = fx.SINCE - fx.DAY


def _chrome(tmp_path: Path, **kwargs: Any) -> Path:
    return fx.chrome_profile(tmp_path / "chrome", **kwargs)


def _safari(tmp_path: Path, **kwargs: Any) -> Path:
    return fx.safari_profile(tmp_path / "safari", **kwargs)


# --- history rows ------------------------------------------------------------------------------


@pytest.mark.parametrize("browser", ["chrome", "safari"])
def test_visits_in_the_window_are_aggregated_per_entry(tmp_path: Path, browser: str) -> None:
    pages = [
        fx.Page(A, "Page A", (BEFORE_WINDOW, *IN_WINDOW)),
        fx.Page(B, "Page B", (BEFORE_WINDOW,)),
    ]
    profile = (_chrome if browser == "chrome" else _safari)(tmp_path, pages=pages)

    rows = read_browser(browser, profile, fx.SINCE)

    assert rows.history == (
        HistoryRow(url=A, title="Page A", visits=3, first_visit=IN_WINDOW[0],
                   last_visit=IN_WINDOW[-1]),
    )
    assert rows.bookmarks == ()
    assert rows.truncated is False
    assert rows.malformed == 0


@pytest.mark.parametrize("browser", ["chrome", "safari"])
def test_rows_still_in_the_wal_are_read(tmp_path: Path, browser: str) -> None:
    """RM-41.D2 regression: ``immutable=1`` on the copy would return no row here."""
    build = _chrome if browser == "chrome" else _safari
    profile = build(tmp_path, pages=[], wal_pages=[fx.Page(A, "Only in the WAL", IN_WINDOW)])
    wal = "History-wal" if browser == "chrome" else "History.db-wal"
    assert (profile / wal).stat().st_size > 0

    rows = read_browser(browser, profile, fx.SINCE)

    assert [row.url for row in rows.history] == [A]


@pytest.mark.parametrize("browser", ["chrome", "safari"])
def test_the_live_profile_is_left_exactly_as_it_was(tmp_path: Path, browser: str) -> None:
    build = _chrome if browser == "chrome" else _safari
    profile = build(
        tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)], wal_pages=[fx.Page(B, "B", IN_WINDOW)],
        bookmarks=[(A, "A")],
    )
    before = fx.snapshot_of(profile)

    read_browser(browser, profile, fx.SINCE)

    assert fx.snapshot_of(profile) == before
    assert not any(name.endswith("-shm") for name in before)


def test_the_temporary_copy_is_removed_even_when_the_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    made: list[Path] = []
    real_mkdtemp = bs.tempfile.mkdtemp

    def tracking_mkdtemp(**kwargs: Any) -> str:
        path = real_mkdtemp(**kwargs)
        made.append(Path(path))
        return path

    def boom(conn: sqlite3.Connection, since: datetime) -> Any:
        raise UnsupportedSchemaError("synthetic")

    monkeypatch.setattr(bs.tempfile, "mkdtemp", tracking_mkdtemp)
    monkeypatch.setattr(bs, "_query_chrome", boom)

    with pytest.raises(UnsupportedSchemaError):
        read_browser("chrome", profile, fx.SINCE)

    assert made and not any(path.exists() for path in made)


def test_the_temporary_copy_is_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    modes: list[int] = []
    real_query = bs._query_chrome

    def spying_query(conn: sqlite3.Connection, since: datetime) -> Any:
        (path,) = [row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"]
        modes.append(Path(path).parent.stat().st_mode & 0o777)
        return real_query(conn, since)

    monkeypatch.setattr(bs, "_query_chrome", spying_query)

    read_browser("chrome", profile, fx.SINCE)

    assert modes == [0o700]


def test_the_copy_is_opened_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    attempts: list[str] = []

    def writing_query(conn: sqlite3.Connection, since: datetime) -> Any:
        try:
            conn.execute("DELETE FROM urls")
        except sqlite3.OperationalError as exc:
            attempts.append(type(exc).__name__)
        return ()

    monkeypatch.setattr(bs, "_query_chrome", writing_query)

    read_browser("chrome", profile, fx.SINCE)

    assert attempts == ["OperationalError"]


def test_the_title_is_the_latest_non_empty_one_in_safari(tmp_path: Path) -> None:
    profile = tmp_path / "safari"
    fx.safari_profile(profile, pages=[fx.Page(A, "Old title", IN_WINDOW[:2])])
    with sqlite3.connect(profile / "History.db") as conn:
        conn.execute(
            "INSERT INTO history_visits(history_item, visit_time, title) VALUES (1, ?, '')",
            (fx.safari_time(IN_WINDOW[2]),),
        )
    conn.close()

    rows = read_browser("safari", profile, fx.SINCE)

    assert rows.history[0].title == "Old title"
    assert rows.history[0].visits == 3


def test_a_null_title_reads_as_empty(tmp_path: Path) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    with sqlite3.connect(profile / "History") as conn:
        conn.execute("UPDATE urls SET title = NULL")
    conn.close()

    assert read_browser("chrome", profile, fx.SINCE).history[0].title == ""


def test_time_conversions_at_known_instants() -> None:
    moment = datetime(2026, 10, 30, 12, 0, 0, 250000, tzinfo=UTC)

    assert bs.from_chrome_time(fx.chrome_time(moment)) == moment
    assert bs.to_chrome_time(moment) == fx.chrome_time(moment)
    assert bs.from_safari_time(fx.safari_time(moment)) == moment
    assert bs.to_safari_time(moment) == fx.safari_time(moment)
    assert bs.to_chrome_time(fx.CHROME_EPOCH) == 0
    assert bs.from_safari_time(0.0) == fx.SAFARI_EPOCH


def test_rows_of_the_wrong_type_are_skipped_and_counted(tmp_path: Path) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW), fx.Page(B, "B", IN_WINDOW)])
    with sqlite3.connect(profile / "History") as conn:
        # BLOBs: a LONGVARCHAR column has TEXT affinity, so a number would read back as text.
        conn.execute("UPDATE urls SET url = x'41' WHERE id = 2")
        conn.execute("UPDATE urls SET title = x'00ff' WHERE id = 1")
    conn.close()

    rows = read_browser("chrome", profile, fx.SINCE)

    assert rows.history == ()
    assert rows.malformed == 2


def test_an_out_of_range_time_is_skipped_and_counted(tmp_path: Path) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    with sqlite3.connect(profile / "History") as conn:
        conn.execute("UPDATE visits SET visit_time = 9223372036854775807")
    conn.close()

    rows = read_browser("chrome", profile, fx.SINCE)

    assert rows.history == ()
    assert rows.malformed == 1


def test_the_row_cap_truncates_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = [fx.Page(f"https://example.com/{n}", str(n), IN_WINDOW) for n in range(3)]
    profile = _chrome(tmp_path, pages=pages)
    monkeypatch.setattr(bs, "MAX_ROWS", 2)

    rows = read_browser("chrome", profile, fx.SINCE)

    assert len(rows.history) == 2
    assert rows.truncated is True


# --- bookmarks ---------------------------------------------------------------------------------


def test_chrome_bookmarks_are_read_from_every_folder(tmp_path: Path) -> None:
    profile = _chrome(tmp_path, bookmarks=[(A, "Bookmark A"), (B, "Bookmark B")])

    rows = read_browser("chrome", profile, fx.SINCE)

    assert set(rows.bookmarks) == {BookmarkRow(A, "Bookmark A"), BookmarkRow(B, "Bookmark B")}


def test_safari_bookmarks_and_the_reading_list_are_read(tmp_path: Path) -> None:
    profile = _safari(tmp_path, bookmarks=[(A, "Bookmark A"), (B, "Reading B")])

    rows = read_browser("safari", profile, fx.SINCE)

    assert set(rows.bookmarks) == {BookmarkRow(A, "Bookmark A"), BookmarkRow(B, "Reading B")}


def test_missing_bookmarks_mean_no_bookmarks(tmp_path: Path) -> None:
    assert read_browser("chrome", _chrome(tmp_path), fx.SINCE).bookmarks == ()
    assert read_browser("safari", _safari(tmp_path), fx.SINCE).bookmarks == ()


@pytest.mark.parametrize(
    "tree",
    [
        {"roots": {"bar": {"type": "folder", "children": [
            {"type": "url", "url": 7, "name": "x"},
            {"type": "url", "url": A, "name": ["x"]},
            "not a node",
            {"type": "folder", "children": "not a list"},
        ]}}},
    ],
)
def test_malformed_chrome_bookmark_nodes_are_skipped_and_counted(
    tmp_path: Path, tree: Any
) -> None:
    profile = _chrome(tmp_path)
    fx.write_chrome_bookmarks(profile, tree)

    rows = read_browser("chrome", profile, fx.SINCE)

    assert rows.bookmarks == ()
    assert rows.malformed == 4


def test_a_bookmark_without_a_name_reads_with_an_empty_title(tmp_path: Path) -> None:
    profile = _chrome(tmp_path)
    fx.write_chrome_bookmarks(
        profile, {"roots": {"bar": {"type": "folder", "children": [{"type": "url", "url": A}]}}}
    )

    assert read_browser("chrome", profile, fx.SINCE).bookmarks == (BookmarkRow(A, ""),)


def test_malformed_safari_bookmark_nodes_are_skipped_and_counted(tmp_path: Path) -> None:
    profile = _safari(tmp_path)
    fx.write_safari_bookmarks(
        profile,
        {"WebBookmarkType": "WebBookmarkTypeList", "Children": [
            {"WebBookmarkType": "WebBookmarkTypeLeaf", "URLString": 3},
            {"WebBookmarkType": "WebBookmarkTypeLeaf", "URLString": A, "URIDictionary": "x"},
            {"WebBookmarkType": "WebBookmarkTypeList", "Children": "x"},
            "not a node",
            {"WebBookmarkType": "WebBookmarkTypeProxy", "Title": "History"},
        ]},
    )

    rows = read_browser("safari", profile, fx.SINCE)

    assert rows.bookmarks == (BookmarkRow(A, ""),)
    assert rows.malformed == 3


def test_bookmark_nesting_past_the_depth_cap_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bs, "MAX_BOOKMARK_DEPTH", 3)
    node: dict[str, Any] = {"type": "url", "url": A, "name": "deep"}
    for _ in range(4):
        node = {"type": "folder", "children": [node]}
    profile = _chrome(tmp_path)
    fx.write_chrome_bookmarks(profile, {"roots": {"bar": node}})

    rows = read_browser("chrome", profile, fx.SINCE)

    assert rows.bookmarks == ()
    assert rows.malformed == 1


@pytest.mark.parametrize("payload", [b"{not json", b"[1, 2]", b"{\"roots\": 3}"])
def test_an_unparseable_chrome_bookmarks_file_fails_loudly(tmp_path: Path, payload: bytes) -> None:
    profile = _chrome(tmp_path)
    (profile / "Bookmarks").write_bytes(payload)

    with pytest.raises(StoreUnreadableError):
        read_browser("chrome", profile, fx.SINCE)


def test_deeply_nested_json_fails_loudly_not_with_a_recursion_traceback(tmp_path: Path) -> None:
    profile = _chrome(tmp_path)
    (profile / "Bookmarks").write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")

    with pytest.raises(StoreUnreadableError):
        read_browser("chrome", profile, fx.SINCE)


@pytest.mark.parametrize("payload", [b"not a plist", b"bplist00garbage"])
def test_an_unparseable_safari_plist_fails_loudly(tmp_path: Path, payload: bytes) -> None:
    profile = _safari(tmp_path)
    (profile / "Bookmarks.plist").write_bytes(payload)

    with pytest.raises(StoreUnreadableError):
        read_browser("safari", profile, fx.SINCE)


def test_a_plist_whose_root_is_not_a_dict_fails_loudly(tmp_path: Path) -> None:
    profile = _safari(tmp_path)
    fx.write_safari_bookmarks(profile, ["a", "list"])

    with pytest.raises(StoreUnreadableError):
        read_browser("safari", profile, fx.SINCE)


def test_an_oversized_bookmarks_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _chrome(tmp_path, bookmarks=[(A, "A"), (B, "B")])
    monkeypatch.setattr(bs, "MAX_BOOKMARKS_BYTES", 16)

    with pytest.raises(StoreTooLargeError):
        read_browser("chrome", profile, fx.SINCE)


# --- failures ----------------------------------------------------------------------------------


@pytest.mark.parametrize("browser", ["chrome", "safari"])
def test_a_profile_without_history_is_reported(tmp_path: Path, browser: str) -> None:
    (tmp_path / "empty").mkdir()

    with pytest.raises(HistoryNotFoundError) as caught:
        read_browser(browser, tmp_path / "empty", fx.SINCE)

    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize(
    ("browser", "statement"),
    [
        ("chrome", "ALTER TABLE visits RENAME COLUMN visit_time TO when_visited"),
        ("chrome", "DROP TABLE urls"),
        ("safari", "ALTER TABLE history_visits RENAME COLUMN title TO page_title"),
        ("safari", "DROP TABLE history_items"),
    ],
)
def test_a_missing_table_or_column_fails_loudly(
    tmp_path: Path, browser: str, statement: str
) -> None:
    build = _chrome if browser == "chrome" else _safari
    profile = build(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    name = "History" if browser == "chrome" else "History.db"
    with sqlite3.connect(profile / name) as conn:
        conn.execute(statement)
    conn.close()

    with pytest.raises(UnsupportedSchemaError):
        read_browser(browser, profile, fx.SINCE)


def test_a_file_that_is_not_sqlite_is_unreadable_after_one_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "chrome"
    profile.mkdir()
    (profile / "History").write_bytes(b"this is not a database" * 100)
    snapshots: list[int] = []
    real_snapshot = bs.snapshot

    def counting_snapshot(path: Path) -> Any:
        snapshots.append(1)
        return real_snapshot(path)

    monkeypatch.setattr(bs, "snapshot", counting_snapshot)

    with pytest.raises(StoreUnreadableError):
        read_browser("chrome", profile, fx.SINCE)

    assert len(snapshots) == 2


def test_a_torn_first_copy_is_retried_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    calls: list[int] = []
    real_query = bs._query_chrome

    def torn_once(conn: sqlite3.Connection, since: datetime) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real_query(conn, since)

    monkeypatch.setattr(bs, "_query_chrome", torn_once)

    rows = read_browser("chrome", profile, fx.SINCE)

    assert [row.url for row in rows.history] == [A]
    assert len(calls) == 2


def test_an_oversized_store_is_refused_before_it_is_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    monkeypatch.setattr(bs, "MAX_STORE_BYTES", 10)
    writes: list[int] = []
    monkeypatch.setattr(bs, "_write", lambda fd, data: writes.append(len(data)) or len(data))

    with pytest.raises(StoreTooLargeError):
        read_browser("chrome", profile, fx.SINCE)

    assert writes == []


def test_safari_permission_denied_is_full_disk_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _safari(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])

    def denied(path: Path) -> Any:
        raise PermissionError(errno.EPERM, "Operation not permitted", str(path))

    monkeypatch.setattr(bs, "_stat", denied)

    with pytest.raises(FullDiskAccessError) as caught:
        read_browser("safari", profile, fx.SINCE)

    assert str(tmp_path) not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_an_unreadable_safari_bookmarks_file_is_full_disk_access(tmp_path: Path) -> None:
    profile = _safari(tmp_path, bookmarks=[(A, "A"), (B, "B")])
    plist = profile / "Bookmarks.plist"
    plist.chmod(0)
    try:
        with pytest.raises(FullDiskAccessError):
            read_browser("safari", profile, fx.SINCE)
    finally:
        plist.chmod(0o600)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_an_unreadable_chrome_store_is_unreadable_not_full_disk_access(tmp_path: Path) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    (profile / "History").chmod(0)
    try:
        with pytest.raises(StoreUnreadableError) as caught:
            read_browser("chrome", profile, fx.SINCE)
    finally:
        (profile / "History").chmod(0o600)

    assert not isinstance(caught.value, FullDiskAccessError)


def test_an_unknown_browser_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown browser"):
        read_browser("lynx", tmp_path, fx.SINCE)  # type: ignore[arg-type]


def test_default_profiles_resolve_under_the_given_home(tmp_path: Path) -> None:
    assert bs.default_profile("chrome", tmp_path) == (
        tmp_path / "Library" / "Application Support" / "Google" / "Chrome" / "Default"
    )
    assert bs.default_profile("safari", tmp_path) == tmp_path / "Library" / "Safari"


def test_every_error_class_is_a_browser_store_error_with_a_fixed_message() -> None:
    for cls in (FullDiskAccessError, HistoryNotFoundError, StoreTooLargeError,
                StoreUnreadableError, UnsupportedSchemaError):
        assert issubclass(cls, bs.BrowserStoreError)
        assert cls.__name__.endswith("Error")


def test_bookmarks_json_survives_round_trip_of_unicode_titles(tmp_path: Path) -> None:
    profile = _chrome(tmp_path)
    (profile / "Bookmarks").write_text(
        json.dumps({"roots": {"bar": {"type": "folder", "children": [
            {"type": "url", "url": A, "name": "Café ☕"}]}}}),
        encoding="utf-8",
    )

    assert read_browser("chrome", profile, fx.SINCE).bookmarks == (BookmarkRow(A, "Café ☕"),)


# --- sec-114 F2: shared plist references and cycles ------------------------------------------


def test_shared_plist_references_are_walked_once(tmp_path: Path) -> None:
    """A 10-way fan-out nested 6 deep is a few hundred bytes and a million leaves when walked
    naively; each node is walked once instead."""
    leaf = {"WebBookmarkType": "WebBookmarkTypeLeaf", "URLString": A,
            "URIDictionary": {"title": "Shared"}}
    node: dict[str, Any] = leaf
    for _ in range(6):
        node = {"WebBookmarkType": "WebBookmarkTypeList", "Children": [node] * 10}
    profile = _safari(tmp_path)
    plist = fx.write_safari_bookmarks(profile, node)
    assert plist.stat().st_size < 1024

    rows = read_browser("safari", profile, fx.SINCE)

    assert rows.bookmarks == (BookmarkRow(A, "Shared"),)
    assert rows.bookmarks_truncated is False


def test_a_cycle_is_walked_once() -> None:
    folder: dict[str, Any] = {"WebBookmarkType": "WebBookmarkTypeList"}
    folder["Children"] = [
        folder,
        {"WebBookmarkType": "WebBookmarkTypeLeaf", "URLString": A},
    ]

    found, malformed, truncated = bs._walk([folder], bs._safari_node)

    assert found == (BookmarkRow(A, ""),)
    assert (malformed, truncated) == (0, False)


def test_the_walk_stops_at_the_node_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bs, "MAX_BOOKMARK_NODES", 5)
    pairs = [(f"https://example.com/{n}", str(n)) for n in range(10)]
    profile = _chrome(tmp_path, bookmarks=pairs)

    rows = read_browser("chrome", profile, fx.SINCE)

    assert rows.bookmarks_truncated is True
    assert len(rows.bookmarks) < 5


# --- sec-114 F5: only regular files, a counted copy, OSErrors by class -----------------------


def _with_history(tmp_path: Path, make: Any) -> Path:
    profile = tmp_path / "chrome"
    profile.mkdir()
    make(profile / "History")
    return profile


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda p: p.symlink_to("/dev/zero"), id="symlink-to-device"),
        pytest.param(lambda p: os.mkfifo(p), id="fifo"),
        pytest.param(lambda p: p.mkdir(), id="directory"),
    ],
)
def test_a_store_that_is_not_a_regular_file_is_refused_unread(
    tmp_path: Path, make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _with_history(tmp_path, make)
    writes: list[int] = []
    monkeypatch.setattr(bs, "_write", lambda fd, data: writes.append(len(data)) or len(data))

    with pytest.raises(StoreUnreadableError) as caught:
        read_browser("chrome", profile, fx.SINCE)

    assert "not a regular file" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert writes == []


def test_a_symlinked_store_is_refused_even_when_it_points_at_a_real_store(
    tmp_path: Path,
) -> None:
    real = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    profile = tmp_path / "linked"
    profile.mkdir()
    (profile / "History").symlink_to(real / "History")

    with pytest.raises(StoreUnreadableError, match="not a regular file"):
        read_browser("chrome", profile, fx.SINCE)


def test_the_copy_counts_bytes_whatever_the_size_reported(tmp_path: Path) -> None:
    read_end, write_end = os.pipe()
    os.write(write_end, b"x" * 300)
    os.close(write_end)
    try:
        with pytest.raises(StoreTooLargeError):
            bs._copy_bounded(read_end, tmp_path / "copy", 100)
    finally:
        os.close(read_end)


def test_a_partial_write_is_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = os.write
    monkeypatch.setattr(bs, "_write", lambda fd, data: real_write(fd, bytes(data[:7])))
    source = tmp_path / "source"
    source.write_bytes(b"0123456789" * 5)
    fd = os.open(source, os.O_RDONLY)
    try:
        assert bs._copy_bounded(fd, tmp_path / "copy", 1000) == 50
    finally:
        os.close(fd)

    assert (tmp_path / "copy").read_bytes() == b"0123456789" * 5


def test_a_full_disk_is_a_one_line_refusal_by_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])

    def full(fd: int, data: Any) -> int:
        raise OSError(errno.ENOSPC, "No space left on device", str(tmp_path / "copy"))

    monkeypatch.setattr(bs, "_write", full)

    with pytest.raises(StoreUnreadableError) as caught:
        read_browser("chrome", profile, fx.SINCE)

    assert "(OSError ENOSPC)" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert caught.value.__cause__ is None


def test_an_oserror_without_an_errno_is_named_by_class() -> None:
    assert bs._os_reason(OSError("synthetic")) == "OSError"
    assert bs._os_reason(IsADirectoryError(errno.EISDIR, "x")) == "IsADirectoryError EISDIR"


# --- sec-114 F6: views refused, queries bounded ----------------------------------------------


@pytest.mark.parametrize(
    "statements",
    [
        ("ALTER TABLE urls RENAME TO urls_real",
         "CREATE VIEW urls AS SELECT id, url, title FROM urls_real"),
        ("ALTER TABLE visits RENAME TO visits_real",
         "CREATE VIEW visits AS WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n)"
         " SELECT x AS url, x AS visit_time FROM n"),
    ],
)
def test_a_view_in_place_of_a_table_is_refused(tmp_path: Path, statements: tuple[str, ...]) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    with sqlite3.connect(profile / "History") as conn:
        for statement in statements:
            conn.execute(statement)
    conn.close()

    with pytest.raises(UnsupportedSchemaError, match="not a table"):
        read_browser("chrome", profile, fx.SINCE)


def test_a_query_past_its_deadline_is_aborted_once_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    monkeypatch.setattr(bs, "QUERY_TIMEOUT_SECONDS", -1.0)
    monkeypatch.setattr(bs, "_PROGRESS_OPS", 1)
    snapshots: list[int] = []
    real_snapshot = bs.snapshot

    def counting_snapshot(path: Path) -> Any:
        snapshots.append(1)
        return real_snapshot(path)

    monkeypatch.setattr(bs, "snapshot", counting_snapshot)

    with pytest.raises(StoreUnreadableError, match="took longer than"):
        read_browser("chrome", profile, fx.SINCE)

    assert snapshots == [1]


# --- sec-114 F10: stale snapshots from killed runs --------------------------------------------


def _aged(root: Path, name: str, age_s: float, now: float) -> Path:
    path = root / name
    path.mkdir()
    (path / "History").write_bytes(b"synthetic")
    os.utime(path, (now - age_s, now - age_s))
    return path


def test_stale_snapshots_are_swept_and_fresh_ones_kept(tmp_path: Path) -> None:
    now = 50_000_000.0
    stale = _aged(tmp_path, "brain-web-stale", bs.STALE_SNAPSHOT_S + 60, now)
    fresh = _aged(tmp_path, "brain-web-fresh", 60, now)
    other = _aged(tmp_path, "brain-voice-old", bs.STALE_SNAPSHOT_S * 10, now)

    assert bs.sweep_stale_snapshots(root=tmp_path, now=now) == 1

    assert not stale.exists()
    assert fresh.exists() and other.exists()


def test_another_users_snapshot_is_not_swept(tmp_path: Path) -> None:
    now = 50_000_000.0
    _aged(tmp_path, "brain-web-stale", bs.STALE_SNAPSHOT_S + 60, now)

    assert bs.sweep_stale_snapshots(root=tmp_path, now=now, uid=os.getuid() + 1) == 0


def test_snapshots_use_the_swept_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    prefixes: list[str] = []
    real_mkdtemp = bs.tempfile.mkdtemp

    def tracking(**kwargs: Any) -> str:
        prefixes.append(kwargs["prefix"])
        return real_mkdtemp(**kwargs)

    monkeypatch.setattr(bs.tempfile, "mkdtemp", tracking)

    read_browser("chrome", profile, fx.SINCE)

    assert prefixes == [bs.TEMP_PREFIX] == ["brain-web-"]


def test_a_file_swapped_between_the_lstat_and_the_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fstat of the opened fd must be the file the lstat saw (no TOCTOU swap)."""
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    decoy = tmp_path / "decoy"
    decoy.write_bytes(b"another regular file")
    real_stat = bs._stat
    monkeypatch.setattr(bs, "_stat", lambda path: real_stat(decoy) if path.name == "History"
                        else real_stat(path))

    with pytest.raises(StoreUnreadableError, match="not a regular file"):
        read_browser("chrome", profile, fx.SINCE)


def test_a_store_removed_before_the_copy_is_not_silently_skipped(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError), bs.snapshot(tmp_path / "History"):
        pass  # pragma: no cover - the snapshot never yields


def test_a_query_inside_its_deadline_runs_under_the_progress_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    monkeypatch.setattr(bs, "_PROGRESS_OPS", 1)

    assert [row.url for row in read_browser("chrome", profile, fx.SINCE).history] == [A]


def test_an_operational_error_that_is_not_the_deadline_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _chrome(tmp_path, pages=[fx.Page(A, "A", IN_WINDOW)])
    calls: list[int] = []

    def locked(conn: sqlite3.Connection, since: datetime) -> Any:
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(bs, "_query_chrome", locked)

    with pytest.raises(StoreUnreadableError) as caught:
        read_browser("chrome", profile, fx.SINCE)

    assert len(calls) == 2
    assert "took longer" not in str(caught.value)
