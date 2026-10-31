"""The X archive reader (RM-44): fixed names, no links, bounded, and the payload never executed.

Every archive is a synthetic tree under ``tmp_path`` (``tests/social_archive_fixtures.py``).
"""
from __future__ import annotations

import ast
import errno
import json
import os
from pathlib import Path

import pytest

from brain.ingest import social_archive as sa
from brain.ingest.social_archive import ArchiveLimits, SocialArchiveError, parse_payload
from tests import social_archive_fixtures as fx

MODULES = [
    Path(sa.__file__),
    Path(sa.__file__).with_name("social_posts.py"),
]
PID = fx.post_id_at(fx.NOW - fx.DAY)
ENTRY = fx.fields(PID)


def _likes(root: Path, text: str, name: str = "like.js") -> Path:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / name).write_text(text, encoding="utf-8")
    return root


def _parse(text: str, collection: str = "like", part: int = 0) -> list[dict]:
    return list(parse_payload(text, collection=collection, part=part))


# --- the payload is parsed as JSON, never executed (AC1) -----------------------------------------


def test_the_prefix_is_stripped_and_the_entries_unwrapped() -> None:
    assert _parse(fx.payload("like", 0, [ENTRY, fx.fields("7")])) == [ENTRY, fx.fields("7")]


@pytest.mark.parametrize(
    "text",
    [
        "window.YTD.like.part0 = []",
        "window.YTD.like.part0=[]",
        "﻿  window.YTD.like.part0 =\n[\n]\n",
        "window.YTD.like.part0 = [];",
        "window.YTD.like.part0 = [] ;\n\n",
    ],
)
def test_accepted_prefix_and_tail_forms(text: str) -> None:
    assert _parse(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "window.YTD.like.part0 = []; require('child_process')",
        "window.YTD.like.part0 = [] ;;",
        "window.YTD.like.part0 = [], alert(1)",
        "window.YTD.like.part0 = (function(){ return [] })()",
        "window.YTD.like.part0 = eval('[]')",
        "window.YTD.like.part0 = `[]`",
        "var x = 1; window.YTD.like.part0 = []",
        "window.YTD.like.part0 = [] // trailing comment",
        "window.YTD.like.part0 = {}",
        "window.YTD.like.part0 = ",
        "window.YTD.like.part0 = [",
        "window.YTD.like.part0 = [{\"like\": {}},]",
        "window.YTD.like.part0 = [,]",
        "window.YTD.like.part0 = [{\"like\": {}} {\"like\": {}}]",
        "[]",
        "",
        "window['YTD'].like.part0 = []",
        "window.YTD.like.part0 = [NaN]",
        "window.YTD.like.part0 = [{\"like\": {\"tweetId\": Infinity}}]",
    ],
)
def test_anything_but_the_prefix_and_one_array_refuses_the_file(text: str) -> None:
    with pytest.raises(SocialArchiveError):
        _parse(text)


@pytest.mark.parametrize(
    ("text", "collection", "part"),
    [
        ("window.YTD.bookmark.part0 = []", "like", 0),
        ("window.YTD.like.part1 = []", "like", 0),
        ("window.YTD.like.part0 = []", "like", 2),
        ("window.YTD.tweets.part0 = []", "like", 0),
    ],
)
def test_the_prefix_must_name_the_expected_file(text: str, collection: str, part: int) -> None:
    with pytest.raises(SocialArchiveError, match="prefix"):
        _parse(text, collection, part)


@pytest.mark.parametrize(
    "element",
    [
        {"bookmark": {"tweetId": "1"}},
        {"like": {"tweetId": "1"}, "extra": {}},
        {},
        {"like": ["1"]},
        {"like": "1"},
        ["like"],
        "like",
        7,
        None,
    ],
)
def test_an_unknown_entry_shape_refuses_the_file(element: object) -> None:
    text = f"window.YTD.like.part0 = {json.dumps([element])}"

    with pytest.raises(SocialArchiveError, match="unknown shape"):
        _parse(text)


def test_deep_nesting_refuses_the_file() -> None:
    nested = "[" * 200_000 + "]" * 200_000
    text = f'window.YTD.like.part0 = [{{"like": {{"tweetId": "1", "x": {nested}}}}}]'

    with pytest.raises(SocialArchiveError, match="not valid JSON"):
        _parse(text)


def test_an_over_long_number_refuses_the_file() -> None:
    text = 'window.YTD.like.part0 = [{"like": {"tweetId": "1", "n": ' + "9" * 5000 + "}}]"

    with pytest.raises(SocialArchiveError, match="not valid JSON"):
        _parse(text)


def test_the_entry_cap_refuses_the_file() -> None:
    text = fx.payload("like", 0, [ENTRY] * 4)

    assert len(list(parse_payload(text, collection="like", part=0, max_entries=4))) == 4
    with pytest.raises(SocialArchiveError, match="more entries"):
        list(parse_payload(text, collection="like", part=0, max_entries=3))


def test_the_new_modules_never_execute_or_reach_the_network() -> None:
    banned_calls = {"eval", "exec", "compile", "__import__"}
    banned_modules = {"subprocess", "socket", "httpx", "urllib.request", "http", "ctypes",
                      "pickle", "marshal", "shelve", "runpy", "importlib"}
    for module in MODULES:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in banned_calls, (module.name, node.func.id)
            if isinstance(node, ast.Import):
                assert not {a.name for a in node.names} & banned_modules, module.name
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                assert node.module not in banned_modules, module.name


# --- reading the archive tree --------------------------------------------------------------------


def test_likes_and_bookmarks_are_read(tmp_path: Path) -> None:
    other = fx.fields("99")
    root = fx.write_archive(tmp_path / "a", likes=[[ENTRY]], bookmarks=[[other]])

    read = sa.read_archive(root)

    assert read.likes == (ENTRY,)
    assert read.bookmarks == (other,)
    assert read.bookmarks_present is True


def test_a_missing_bookmarks_file_is_not_an_error(tmp_path: Path) -> None:
    root = fx.write_archive(tmp_path / "a", likes=[[ENTRY]], bookmarks=None)

    read = sa.read_archive(root)

    assert read.bookmarks == ()
    assert read.bookmarks_present is False


def test_parts_are_read_in_order_until_the_first_gap(tmp_path: Path) -> None:
    parts = [[fx.fields("1")], [fx.fields("2")], [fx.fields("3")]]
    root = fx.write_archive(tmp_path / "a", likes=parts)
    (root / "data" / "like-part5.js").write_text(
        fx.payload("like", 5, [fx.fields("5")]), encoding="utf-8"
    )

    read = sa.read_archive(root)

    assert [entry["tweetId"] for entry in read.likes] == ["1", "2", "3"]


def test_a_part_whose_prefix_names_another_part_is_refused(tmp_path: Path) -> None:
    root = fx.write_archive(tmp_path / "a", likes=[[ENTRY]])
    (root / "data" / "like-part1.js").write_text(fx.payload("like", 2, []), encoding="utf-8")

    with pytest.raises(SocialArchiveError, match=r"data/like-part1\.js"):
        sa.read_archive(root)


def test_too_many_parts_are_refused(tmp_path: Path) -> None:
    root = fx.write_archive(tmp_path / "a", likes=[[], [], []])

    assert sa.read_archive(root, limits=ArchiveLimits(max_parts=3)).likes == ()
    with pytest.raises(SocialArchiveError, match="more than 2 parts"):
        sa.read_archive(root, limits=ArchiveLimits(max_parts=2))


def test_a_folder_without_data_is_refused(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()

    with pytest.raises(SocialArchiveError, match="no data/ folder"):
        sa.read_archive(tmp_path / "a")


def test_a_data_folder_without_likes_is_refused(tmp_path: Path) -> None:
    (tmp_path / "a" / "data").mkdir(parents=True)

    with pytest.raises(SocialArchiveError, match="no likes file"):
        sa.read_archive(tmp_path / "a")


@pytest.mark.parametrize("make", ["missing", "file"])
def test_a_root_that_is_not_a_folder_is_refused(tmp_path: Path, make: str) -> None:
    root = tmp_path / "a"
    if make == "file":
        root.write_text("x", encoding="utf-8")

    with pytest.raises(SocialArchiveError, match="not a folder"):
        sa.read_archive(root)


def test_an_unreadable_root_is_refused(tmp_path: Path) -> None:
    root = fx.write_archive(tmp_path / "a", likes=[[ENTRY]])
    root.chmod(0)
    try:
        with pytest.raises(SocialArchiveError, match="could not be opened"):
            sa.read_archive(root)
    finally:
        root.chmod(0o700)


def test_a_symlinked_root_is_the_owners_choice_and_is_followed(tmp_path: Path) -> None:
    real = fx.write_archive(tmp_path / "real", likes=[[ENTRY]])
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    assert sa.read_archive(link).likes == (ENTRY,)


def test_a_symlinked_data_folder_is_refused(tmp_path: Path) -> None:
    elsewhere = fx.write_archive(tmp_path / "elsewhere", likes=[[ENTRY]])
    root = tmp_path / "a"
    root.mkdir()
    (root / "data").symlink_to(elsewhere / "data", target_is_directory=True)

    with pytest.raises(SocialArchiveError, match="data/ is not a plain folder"):
        sa.read_archive(root)


def test_a_data_entry_that_is_a_file_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "a"
    root.mkdir()
    (root / "data").write_text("x", encoding="utf-8")

    with pytest.raises(SocialArchiveError, match="data/ is not a plain folder"):
        sa.read_archive(root)


@pytest.mark.parametrize("name", ["like.js", "bookmark.js", "like-part1.js"])
def test_a_symlinked_archive_file_is_refused(tmp_path: Path, name: str) -> None:
    root = fx.write_archive(tmp_path / "a", likes=[[ENTRY]])
    target = tmp_path / "target.js"
    collection = name.split(".")[0].split("-")[0]
    part = 1 if "part1" in name else 0
    target.write_text(fx.payload(collection, part, [ENTRY]), encoding="utf-8")
    path = root / "data" / name
    path.unlink(missing_ok=True)
    path.symlink_to(target)

    with pytest.raises(SocialArchiveError, match=rf"data/{name} is not a plain file"):
        sa.read_archive(root)


def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    root = tmp_path / "a"
    (root / "data").mkdir(parents=True)
    os.mkfifo(root / "data" / "like.js")

    with pytest.raises(SocialArchiveError, match=r"data/like\.js is not a plain file"):
        sa.read_archive(root)


def test_a_folder_named_like_a_file_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "a"
    (root / "data" / "like.js").mkdir(parents=True)

    with pytest.raises(SocialArchiveError, match=r"data/like\.js is not a plain file"):
        sa.read_archive(root)


def test_a_hard_linked_file_is_refused(tmp_path: Path) -> None:
    root = fx.write_archive(tmp_path / "a", likes=[[ENTRY]])
    os.link(root / "data" / "like.js", tmp_path / "second-name.js")

    with pytest.raises(SocialArchiveError, match=r"data/like\.js has more than one link"):
        sa.read_archive(root)


def test_a_file_over_the_size_cap_is_refused(tmp_path: Path) -> None:
    root = fx.write_archive(tmp_path / "a", likes=[[ENTRY]])
    size = (root / "data" / "like.js").stat().st_size

    assert sa.read_archive(root, limits=ArchiveLimits(max_file_bytes=size)).likes == (ENTRY,)
    with pytest.raises(SocialArchiveError, match=r"data/like\.js is larger than"):
        sa.read_archive(root, limits=ArchiveLimits(max_file_bytes=size - 1))


def test_a_file_that_grows_after_its_size_is_read_is_refused(tmp_path: Path) -> None:
    root = fx.write_archive(tmp_path / "a", likes=[[ENTRY]])
    size = (root / "data" / "like.js").stat().st_size
    grown = {"done": False}

    def growing_read(fd: int, count: int) -> bytes:
        chunk = os.read(fd, count)
        if chunk or grown["done"]:
            return chunk
        grown["done"] = True
        return b" " * 10

    with pytest.raises(SocialArchiveError, match=r"data/like\.js grew"):
        sa.read_archive(root, limits=ArchiveLimits(max_file_bytes=size + 5), read=growing_read)


def test_the_run_wide_read_cap_spans_files(tmp_path: Path) -> None:
    root = fx.write_archive(tmp_path / "a", likes=[[ENTRY]], bookmarks=[[ENTRY]])
    total = sum(p.stat().st_size for p in (root / "data").iterdir())

    assert sa.read_archive(root, limits=ArchiveLimits(max_read_bytes=total)).bookmarks
    with pytest.raises(SocialArchiveError, match="more than the read cap"):
        sa.read_archive(root, limits=ArchiveLimits(max_read_bytes=total - 1))


def test_the_entry_cap_spans_files(tmp_path: Path) -> None:
    root = fx.write_archive(tmp_path / "a", likes=[[ENTRY, ENTRY]], bookmarks=[[ENTRY]])

    assert len(sa.read_archive(root, limits=ArchiveLimits(max_entries=3)).bookmarks) == 1
    with pytest.raises(SocialArchiveError, match="more entries"):
        sa.read_archive(root, limits=ArchiveLimits(max_entries=2))


def test_invalid_utf8_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "a"
    (root / "data").mkdir(parents=True)
    (root / "data" / "like.js").write_bytes(b"window.YTD.like.part0 = [\"\xff\"]")

    with pytest.raises(SocialArchiveError, match=r"data/like\.js is not UTF-8"):
        sa.read_archive(root)


def test_a_malformed_bookmarks_file_refuses_the_run(tmp_path: Path) -> None:
    root = fx.write_archive(tmp_path / "a", likes=[[ENTRY]])
    (root / "data" / "bookmark.js").write_text("window.YTD.bookmark.part0 = [1]", "utf-8")

    with pytest.raises(SocialArchiveError, match=r"data/bookmark\.js .*unknown shape"):
        sa.read_archive(root)


def test_the_error_names_no_content(tmp_path: Path) -> None:
    secret = "SyntheticPrivateText"
    root = _likes(tmp_path / "a", f'window.YTD.like.part0 = [{{"{secret}": 1}}]')

    with pytest.raises(SocialArchiveError) as caught:
        sa.read_archive(root)

    assert secret not in str(caught.value)


def test_an_unreadable_file_is_refused(tmp_path: Path) -> None:
    root = fx.write_archive(tmp_path / "a", likes=[[ENTRY]])
    (root / "data" / "like.js").chmod(0)
    try:
        with pytest.raises(SocialArchiveError, match=r"data/like\.js could not be read"):
            sa.read_archive(root)
    finally:
        (root / "data" / "like.js").chmod(0o600)


def test_an_unreadable_data_folder_is_refused(tmp_path: Path) -> None:
    root = fx.write_archive(tmp_path / "a", likes=[[ENTRY]])
    (root / "data").chmod(0)
    try:
        with pytest.raises(SocialArchiveError, match="data/ could not be opened"):
            sa.read_archive(root)
    finally:
        (root / "data").chmod(0o700)


def test_a_read_that_fails_midway_is_refused(tmp_path: Path) -> None:
    root = fx.write_archive(tmp_path / "a", likes=[[ENTRY]])

    def failing_read(fd: int, count: int) -> bytes:
        raise OSError(errno.EIO, "synthetic I/O error")

    with pytest.raises(SocialArchiveError, match=r"data/like\.js could not be read"):
        sa.read_archive(root, read=failing_read)
