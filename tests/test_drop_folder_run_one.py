"""Tests for :func:`brain.drop_folder.run_one`, the one-named-file run (RM-40's inbox).

Every tree is built in ``tmp_path``; no real folder of the owner's is read. The ingest
step is a fake that records its calls. ``run_one`` reuses :func:`run`'s private move,
so these rows pin what is new: the name rule, the dataless and lstat checks, and that
exactly the named file moves, whatever its mtime.
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from brain.drop_folder import (
    IMPORTED_DIR,
    SF_DATALESS,
    DropFolder,
    DropFolderError,
    FileOutcome,
    run_one,
)

DOCS = frozenset({".md", ".txt", ".pdf"})


def _folder(path: Path, **kw: Any) -> DropFolder:
    values: dict[str, Any] = {
        "path": path,
        "mode": "ingest",
        "kind": "manual",
        "extensions": DOCS,
        "sensitivity": "confidential",
        "auto": False,
    }
    values.update(kw)
    return DropFolder(**values)


def _sha8(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:8]


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


@dataclass
class _Ingest:
    answer: str | BaseException = "ingested"
    calls: list[tuple[Path, str, float]] = field(default_factory=list)

    def __call__(self, conn: Any, path: Path, *, title: str, timeout: float) -> str:
        self.calls.append((path, title, timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _run_one(folder: DropFolder, name: str, ingest: _Ingest, **kw: Any) -> FileOutcome:
    return run_one(None, folder, name, ingest=ingest, **kw)  # type: ignore[arg-type]


def test_only_the_named_file_moves_and_is_ingested_from_imported(tmp_path: Path) -> None:
    body = b"# zorbly notes\n"
    (tmp_path / "zorbly notes.md").write_bytes(body)
    (tmp_path / "other.md").write_bytes(b"left alone")
    ingest = _Ingest()

    outcome = _run_one(_folder(tmp_path), "zorbly notes.md", ingest, timeout=42.0)

    dst = tmp_path / IMPORTED_DIR / f"zorbly notes-{_sha8(body)}.md"
    assert outcome == FileOutcome("zorbly notes.md", "ingested")
    assert ingest.calls == [(dst, "zorbly notes", 42.0)]
    assert _tree(tmp_path) == {
        "other.md": b"left alone",
        f"{IMPORTED_DIR}/zorbly notes-{_sha8(body)}.md": body,
    }


def test_a_file_of_any_age_is_taken_at_once(tmp_path: Path) -> None:
    """No ``since`` and no settle window: the user picked this file by name."""
    fresh = tmp_path / "fresh.md"
    fresh.write_bytes(b"just written")
    old = tmp_path / "old.txt"
    old.write_bytes(b"years old")
    os.utime(old, (1.0, 1.0))
    ingest = _Ingest()

    assert _run_one(_folder(tmp_path), "fresh.md", ingest).status == "ingested"
    assert _run_one(_folder(tmp_path), "old.txt", ingest).status == "ingested"
    assert len(ingest.calls) == 2


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "../x.md", "sub/x.md", "/etc/x.md", ".hidden.md", "x\0.md", "x.png",
     IMPORTED_DIR, "noext"],
)
def test_a_name_that_is_not_a_bare_file_name_this_folder_takes_is_refused(
    tmp_path: Path, name: str
) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.md").write_bytes(b"nested")
    before = _tree(tmp_path)
    ingest = _Ingest()

    with pytest.raises(ValueError, match="not a file name"):
        _run_one(_folder(tmp_path), name, ingest)

    assert ingest.calls == []
    assert _tree(tmp_path) == before
    assert not (tmp_path / IMPORTED_DIR).exists()


def test_a_missing_file_is_vanished(tmp_path: Path) -> None:
    ingest = _Ingest()

    assert _run_one(_folder(tmp_path), "gone.md", ingest) == FileOutcome("gone.md", "vanished")
    assert ingest.calls == []


def test_a_symlink_is_never_followed_or_moved(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_bytes(b"outside the folder")
    (tmp_path / "link.md").symlink_to(outside)
    ingest = _Ingest()

    outcome = _run_one(_folder(tmp_path), "link.md", ingest)

    assert outcome.status == "failed"
    assert ingest.calls == []
    assert (tmp_path / "link.md").is_symlink()
    assert outside.read_bytes() == b"outside the folder"
    assert list((tmp_path / IMPORTED_DIR).iterdir()) == []


def test_a_directory_with_the_name_is_refused(tmp_path: Path) -> None:
    (tmp_path / "folderish.md").mkdir()
    ingest = _Ingest()

    outcome = _run_one(_folder(tmp_path), "folderish.md", ingest)

    assert outcome == FileOutcome("folderish.md", "failed", "MoveRaceError")
    assert ingest.calls == []
    assert (tmp_path / "folderish.md").is_dir()


def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe.md")
    ingest = _Ingest()

    outcome = _run_one(_folder(tmp_path), "pipe.md", ingest)

    assert outcome.status == "failed"
    assert ingest.calls == []
    assert stat.S_ISFIFO(os.lstat(tmp_path / "pipe.md").st_mode)


class _FakeStat:
    def __init__(self, real: os.stat_result, flags: int) -> None:
        self._real = real
        self.st_flags = flags

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_a_dataless_file_is_refused_unread(tmp_path: Path) -> None:
    (tmp_path / "evicted.md").write_bytes(b"in the cloud")
    ingest = _Ingest()

    def lstat(path: Path) -> Any:
        return _FakeStat(os.lstat(path), SF_DATALESS)

    outcome = _run_one(_folder(tmp_path), "evicted.md", ingest, lstat=lstat)

    assert outcome.status == "failed"
    assert "dataless" in outcome.detail
    assert ingest.calls == []
    assert (tmp_path / "evicted.md").read_bytes() == b"in the cloud"


def test_an_lstat_error_is_a_class_only_failure(tmp_path: Path) -> None:
    (tmp_path / "locked.md").write_bytes(b"x")

    def lstat(path: Path) -> Any:
        raise PermissionError(errno.EACCES, "denied", str(path))

    outcome = _run_one(_folder(tmp_path), "locked.md", _Ingest(), lstat=lstat)

    assert outcome == FileOutcome("locked.md", "failed", "PermissionError[EACCES]")


def test_a_failed_ingest_moves_the_file_back(tmp_path: Path) -> None:
    (tmp_path / "retry.md").write_bytes(b"try again")
    ingest = _Ingest(answer=RuntimeError("the embedder said a secret thing"))

    outcome = _run_one(_folder(tmp_path), "retry.md", ingest)

    assert outcome == FileOutcome("retry.md", "failed", "RuntimeError")
    assert _tree(tmp_path) == {"retry.md": b"try again"}


def test_an_empty_document_stays_in_imported(tmp_path: Path) -> None:
    body = b"   \n"
    (tmp_path / "blank.txt").write_bytes(body)

    outcome = _run_one(_folder(tmp_path), "blank.txt", _Ingest(answer="empty"))

    assert outcome == FileOutcome("blank.txt", "empty")
    assert _tree(tmp_path) == {f"{IMPORTED_DIR}/blank-{_sha8(body)}.txt": body}


def test_the_same_file_already_imported_is_a_duplicate(tmp_path: Path) -> None:
    body = b"twice"
    (tmp_path / IMPORTED_DIR).mkdir()
    (tmp_path / IMPORTED_DIR / f"again-{_sha8(body)}.md").write_bytes(body)
    (tmp_path / "again.md").write_bytes(body)

    outcome = _run_one(_folder(tmp_path), "again.md", _Ingest(answer="unchanged"))

    assert outcome == FileOutcome("again.md", "duplicate")
    assert _tree(tmp_path) == {f"{IMPORTED_DIR}/again-{_sha8(body)}.md": body}


def test_the_extension_check_is_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "LOUD.MD").write_bytes(b"caps")

    assert _run_one(_folder(tmp_path), "LOUD.MD", _Ingest()).status == "ingested"


def test_a_symlinked_imported_refuses_the_folder(tmp_path: Path) -> None:
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"
    elsewhere.mkdir()
    (tmp_path / IMPORTED_DIR).symlink_to(elsewhere)
    (tmp_path / "x.md").write_bytes(b"x")

    with pytest.raises(DropFolderError):
        _run_one(_folder(tmp_path), "x.md", _Ingest())

    assert (tmp_path / "x.md").read_bytes() == b"x"
    assert list(elsewhere.iterdir()) == []


def test_a_capture_folder_is_refused_and_nothing_moves(tmp_path: Path) -> None:
    """RM-39's capture mode deletes a file on success; run_one never runs one."""
    (tmp_path / "x.md").write_bytes(b"keep me")
    ingest = _Ingest()

    with pytest.raises(DropFolderError, match="only an ingest folder"):
        _run_one(_folder(tmp_path, mode="capture"), "x.md", ingest)

    assert ingest.calls == []
    assert _tree(tmp_path) == {"x.md": b"keep me"}
    assert not (tmp_path / IMPORTED_DIR).exists()


def test_an_ingest_folder_needs_a_kind(tmp_path: Path) -> None:
    with pytest.raises(DropFolderError, match="source kind"):
        _run_one(_folder(tmp_path, kind=None), "x.md", _Ingest())
