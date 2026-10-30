"""Tests for :mod:`brain.ui.inbox`: which folder is the inbox, the name rule, the
resolve-then-compare guard and the listing (RM-40).

Pure: ``tmp_path`` trees and a ``tmp_path`` BRAIN_HOME, no database. Every name is
synthetic.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from brain.config import ConfigError
from brain.drop_folder import SETTLE_S, SF_DATALESS, DropFolder
from brain.ui import inbox
from brain.ui.errors import (
    UiBadRequest,
    UiConflict,
    UiNotFound,
    UiPayloadTooLarge,
    UiUnsupportedMedia,
)
from brain.ui.inbox import InboxFile, check_name, find_inbox, list_inbox, resolve_inbox_file
from brain.ui.ingest_limits import MAX_TEXT_UPLOAD_BYTES

DOCS = frozenset({".md", ".txt", ".pdf", ".png"})
NOW = 1_800_000_000.0


def _folder(path: Path, **kw: Any) -> DropFolder:
    values: dict[str, Any] = {
        "path": path.resolve(),
        "mode": "ingest",
        "kind": "manual",
        "extensions": DOCS,
        "sensitivity": "confidential",
        "auto": False,
    }
    values.update(kw)
    return DropFolder(**values)


def _toml(home: Path, *entries: str) -> None:
    home.mkdir(exist_ok=True)
    (home / "drop-folders.toml").write_text("\n".join(entries), encoding="utf-8")


def _entry(path: Path, *, kind: str = "manual", auto: bool = False) -> str:
    return (
        f'[[folder]]\npath = "{path}"\nkind = "{kind}"\nauto = {str(auto).lower()}\n'
        'extensions = [".md", ".txt"]\n'
    )


def _write(path: Path, body: bytes = b"synthetic", *, age_s: float = 3600.0) -> Path:
    path.write_bytes(body)
    os.utime(path, (NOW - age_s, NOW - age_s))
    return path


# --- find_inbox (RM-40.D1) ------------------------------------------------------------


def test_no_config_file_means_no_inbox(tmp_path: Path) -> None:
    assert find_inbox(tmp_path / "home") is None


def test_the_inbox_is_the_first_manual_folder_with_auto_off(tmp_path: Path) -> None:
    shots, auto_manual, first, second = (tmp_path / n for n in ("s", "am", "i1", "i2"))
    for d in (shots, auto_manual, first, second):
        d.mkdir()
    _toml(
        tmp_path / "home",
        _entry(shots, kind="screenshot", auto=False),
        _entry(auto_manual, auto=True),
        _entry(first),
        _entry(second),
    )

    found = find_inbox(tmp_path / "home")

    assert found is not None
    assert found.path == first.resolve()
    assert found.kind == "manual" and found.auto is False


def test_a_folder_list_with_no_candidate_has_no_inbox(tmp_path: Path) -> None:
    shots = tmp_path / "s"
    shots.mkdir()
    _toml(tmp_path / "home", _entry(shots, kind="screenshot"), _entry(shots.parent, auto=True))

    assert find_inbox(tmp_path / "home") is None


def test_a_bad_config_raises_its_config_error(tmp_path: Path) -> None:
    _toml(tmp_path / "home", "not toml [[[")

    with pytest.raises(ConfigError):
        find_inbox(tmp_path / "home")


def test_the_inbox_takes_only_uploadable_extensions(tmp_path: Path) -> None:
    assert inbox.inbox_extensions(_folder(tmp_path)) == frozenset({".md", ".txt", ".pdf"})


# --- check_name (AC1) -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [None, 7, ["a.md"], "", ".", "..", "../a.md", "a/b.md", "/etc/a.md", ".hidden.md",
     "a\0.md", "a\x1b[31m.md", "a\nb.md", "a‮.md", "\ud800.md", "é" * 128 + ".md"],
)
def test_a_name_that_is_not_one_plain_file_name_is_invalid(raw: object) -> None:
    with pytest.raises(UiBadRequest) as exc:
        check_name(raw, DOCS)
    assert exc.value.code == "invalid_name"


def test_a_name_of_255_bytes_is_allowed() -> None:
    name = "a" * 252 + ".md"
    assert check_name(name, DOCS) == name


@pytest.mark.parametrize("raw", ["photo.heic", "notes", "archive.MD.zip"])
def test_a_name_with_another_extension_is_unsupported(raw: str) -> None:
    with pytest.raises(UiUnsupportedMedia) as exc:
        check_name(raw, frozenset({".md"}))
    assert exc.value.code == "unsupported_file"


def test_the_extension_check_ignores_case() -> None:
    assert check_name("LOUD.MD", frozenset({".md"})) == "LOUD.MD"


def test_spaces_and_unicode_letters_are_fine() -> None:
    assert check_name("Ideas  for  Ünïcode — draft.md", DOCS) == "Ideas  for  Ünïcode — draft.md"


# --- resolve_inbox_file (AC1, AC2) ----------------------------------------------------


def test_a_regular_file_resolves_to_its_size(tmp_path: Path) -> None:
    _write(tmp_path / "keep.md", b"12345")

    assert resolve_inbox_file(_folder(tmp_path), "keep.md") == 5


def test_a_missing_file_is_unknown(tmp_path: Path) -> None:
    with pytest.raises(UiNotFound) as exc:
        resolve_inbox_file(_folder(tmp_path), "nope.md")
    assert exc.value.code == "inbox_file_unknown"


def test_a_symlink_out_of_the_inbox_is_refused_by_lstat(tmp_path: Path) -> None:
    root = tmp_path / "inbox"
    root.mkdir()
    outside = _write(tmp_path / "secret.md")
    (root / "link.md").symlink_to(outside)

    with pytest.raises(UiBadRequest) as exc:
        resolve_inbox_file(_folder(root), "link.md")
    assert exc.value.code == "not_a_regular_file"


def test_a_link_swapped_in_after_the_lstat_is_caught_by_resolve_then_compare(
    tmp_path: Path,
) -> None:
    """The lstat saw a regular file; by the resolve the name is a link out of the inbox."""
    root = tmp_path / "inbox"
    root.mkdir()
    real = _write(tmp_path / "regular.md")
    (root / "link.md").symlink_to(tmp_path / "secret.md")

    def lstat(path: Path) -> Any:
        return os.lstat(real)  # what the name was a moment ago

    with pytest.raises(UiBadRequest) as exc:
        resolve_inbox_file(_folder(root), "link.md", lstat=lstat)
    assert exc.value.code == "outside_inbox"


@pytest.mark.parametrize("cycle", ["self", "pair"])
def test_a_symlink_loop_is_not_a_regular_file(tmp_path: Path, cycle: str) -> None:
    """#110 rev F1: ``Path.resolve`` raises ``RuntimeError`` on a loop on 3.11."""
    if cycle == "self":
        (tmp_path / "loop.md").symlink_to(tmp_path / "loop.md")
    else:
        (tmp_path / "loop.md").symlink_to(tmp_path / "b.md")
        (tmp_path / "b.md").symlink_to(tmp_path / "loop.md")

    with pytest.raises(UiBadRequest) as exc:
        resolve_inbox_file(_folder(tmp_path), "loop.md")
    assert exc.value.code == "not_a_regular_file"


def test_a_loop_swapped_in_after_the_lstat_is_not_a_regular_file(tmp_path: Path) -> None:
    """The resolve's ``RuntimeError`` is a 4xx too, never an unhandled 500."""
    real = _write(tmp_path / "real.txt")
    (tmp_path / "loop.md").symlink_to(tmp_path / "loop.md")

    def lstat(path: Path) -> Any:
        return os.lstat(real)

    with pytest.raises(UiBadRequest) as exc:
        resolve_inbox_file(_folder(tmp_path), "loop.md", lstat=lstat)
    assert exc.value.code == "not_a_regular_file"


def test_an_lstat_it_may_not_make_is_inbox_unreadable(tmp_path: Path) -> None:
    def lstat(path: Path) -> Any:
        raise PermissionError(1, "Operation not permitted", str(path))

    with pytest.raises(UiConflict) as exc:
        resolve_inbox_file(_folder(tmp_path), "x.md", lstat=lstat)
    assert exc.value.code == "inbox_unreadable"


@pytest.mark.parametrize("gone", ["removed", "loop"])
def test_an_inbox_root_that_no_longer_resolves_is_unreadable(tmp_path: Path, gone: str) -> None:
    root = tmp_path / "inbox"  # built directly: _folder() would resolve the path
    folder = DropFolder(path=root, mode="ingest", kind="manual", extensions=DOCS,
                        sensitivity="confidential", auto=False)
    if gone == "loop":
        root.symlink_to(root)

    with pytest.raises(UiConflict) as exc:
        resolve_inbox_file(folder, "x.md")
    assert exc.value.code == "inbox_unreadable"


def test_a_symlink_to_a_file_inside_the_inbox_is_refused_by_lstat(tmp_path: Path) -> None:
    _write(tmp_path / "real.md")
    (tmp_path / "alias.md").symlink_to(tmp_path / "real.md")

    with pytest.raises(UiBadRequest) as exc:
        resolve_inbox_file(_folder(tmp_path), "alias.md")
    assert exc.value.code == "not_a_regular_file"


def test_a_dangling_symlink_is_refused(tmp_path: Path) -> None:
    (tmp_path / "dangling.md").symlink_to(tmp_path / "missing.md")

    with pytest.raises(UiBadRequest) as exc:
        resolve_inbox_file(_folder(tmp_path), "dangling.md")
    assert exc.value.code == "not_a_regular_file"


def test_a_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "dir.md").mkdir()

    with pytest.raises(UiBadRequest) as exc:
        resolve_inbox_file(_folder(tmp_path), "dir.md")
    assert exc.value.code == "not_a_regular_file"


def test_a_fifo_is_refused(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe.md")

    with pytest.raises(UiBadRequest) as exc:
        resolve_inbox_file(_folder(tmp_path), "pipe.md")
    assert exc.value.code == "not_a_regular_file"


class _FakeStat:
    def __init__(self, real: os.stat_result, **over: Any) -> None:
        self._real = real
        self._over = over

    def __getattr__(self, name: str) -> Any:
        return self._over[name] if name in self._over else getattr(self._real, name)


def test_a_dataless_file_is_refused(tmp_path: Path) -> None:
    _write(tmp_path / "cloud.md")

    def lstat(path: Path) -> Any:
        return _FakeStat(os.lstat(path), st_flags=SF_DATALESS)

    with pytest.raises(UiConflict) as exc:
        resolve_inbox_file(_folder(tmp_path), "cloud.md", lstat=lstat)
    assert exc.value.code == "file_not_on_disk"


def test_a_file_over_its_upload_cap_is_too_large(tmp_path: Path) -> None:
    _write(tmp_path / "huge.md")

    def lstat(path: Path) -> Any:
        return _FakeStat(os.lstat(path), st_size=MAX_TEXT_UPLOAD_BYTES + 1)

    with pytest.raises(UiPayloadTooLarge) as exc:
        resolve_inbox_file(_folder(tmp_path), "huge.md", lstat=lstat)
    assert exc.value.code == "inbox_file_too_large"


def test_a_file_at_its_upload_cap_is_allowed(tmp_path: Path) -> None:
    _write(tmp_path / "full.md")

    def lstat(path: Path) -> Any:
        return _FakeStat(os.lstat(path), st_size=MAX_TEXT_UPLOAD_BYTES)

    assert resolve_inbox_file(_folder(tmp_path), "full.md", lstat=lstat) == MAX_TEXT_UPLOAD_BYTES


# --- list_inbox -----------------------------------------------------------------------


def test_the_listing_is_names_and_sizes_newest_first(tmp_path: Path) -> None:
    _write(tmp_path / "older.md", b"a", age_s=7200)
    _write(tmp_path / "newer.txt", b"bbb", age_s=60)

    listing = list_inbox(_folder(tmp_path), now=NOW)

    assert listing.files == (InboxFile("newer.txt", 3), InboxFile("older.md", 1))
    assert listing.truncated is False


def test_the_listing_leaves_out_what_cannot_be_picked(tmp_path: Path) -> None:
    _write(tmp_path / "ok.md")
    _write(tmp_path / "shot.png")  # the folder takes it; the inbox does not (D3)
    _write(tmp_path / ".hidden.md")
    _write(tmp_path / "ctrl\x1b.md")
    _write(tmp_path / "settling.md", age_s=SETTLE_S / 2)
    (tmp_path / "link.md").symlink_to(tmp_path / "ok.md")
    (tmp_path / "dir.md").mkdir()
    (tmp_path / "imported").mkdir()
    _write(tmp_path / "imported" / "done-12345678.md")

    listing = list_inbox(_folder(tmp_path), now=NOW)

    assert [f.name for f in listing.files] == ["ok.md"]


def test_the_listing_is_capped(tmp_path: Path) -> None:
    for i in range(inbox.MAX_LISTED + 2):
        _write(tmp_path / f"n{i:03}.md", age_s=10_000 - i)

    listing = list_inbox(_folder(tmp_path), now=NOW)

    assert len(listing.files) == inbox.MAX_LISTED
    assert listing.truncated is True
    assert listing.files[0].name == f"n{inbox.MAX_LISTED + 1:03}.md"  # newest kept


def test_a_file_that_vanishes_while_listing_is_left_out(tmp_path: Path) -> None:
    _write(tmp_path / "stays.md")
    _write(tmp_path / "goes.md")
    calls: list[Path] = []

    def lstat(path: Path) -> Any:
        calls.append(path)
        if path.name == "goes.md" and calls.count(path) > 1:
            raise FileNotFoundError(path)
        return os.lstat(path)

    listing = list_inbox(_folder(tmp_path), now=NOW, lstat=lstat)

    assert [f.name for f in listing.files] == ["stays.md"]
