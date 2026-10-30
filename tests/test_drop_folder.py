"""Tests for the drop-folder runner, :mod:`brain.drop_folder` (RM-33).

Every tree is built in ``tmp_path``; no real folder of the owner's is read. The
ingest step is a fake that records its calls; the DB end-to-end lives in
``tests/test_cli_ingest_folder.py``.
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from brain import drop_folder
from brain.drop_folder import (
    IMPORTED_DIR,
    SETTLE_S,
    SF_DATALESS,
    DropFolder,
    DropFolderError,
    run,
    scan,
)

IMAGES = frozenset({".png", ".jpg", ".jpeg"})
NOW = 1_800_000_000.0  # a fixed wall clock, seconds since the epoch
SINCE = datetime.fromtimestamp(NOW, UTC) - timedelta(days=2)
#: For entries whose mtime is the real "now" (symlinks, directories): no time filter at all,
#: so only the guard under test can leave them out.
EPOCH = datetime.fromtimestamp(0, UTC)


def _folder(path: Path, **kw: Any) -> DropFolder:
    values: dict[str, Any] = {
        "path": path,
        "mode": "ingest",
        "kind": "screenshot",
        "extensions": IMAGES,
        "sensitivity": "confidential",
    }
    values.update(kw)
    return DropFolder(**values)


def _drop(folder: Path, name: str, body: bytes, *, age_s: float = 3600.0) -> Path:
    """Write ``name`` into ``folder`` with an mtime ``age_s`` before :data:`NOW`."""
    path = folder / name
    path.write_bytes(body)
    os.utime(path, (NOW - age_s, NOW - age_s))
    return path


def _sha8(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:8]


@dataclass
class _Ingest:
    """A fake ``IngestFn``: answers each call from ``answers`` and records it."""

    answers: list[str | BaseException] = field(default_factory=list)
    calls: list[tuple[Path, str, float]] = field(default_factory=list)
    seen_bytes: list[bytes] = field(default_factory=list)
    default: str = "ingested"

    def __call__(self, conn: Any, path: Path, *, title: str, timeout: float) -> str:
        self.calls.append((path, title, timeout))
        self.seen_bytes.append(path.read_bytes())
        answer = self.answers[len(self.calls) - 1] if len(self.calls) <= len(self.answers) else (
            self.default
        )
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _run(folder: DropFolder, ingest: _Ingest, **kw: Any) -> drop_folder.DropReport:
    return run(None, folder, since=SINCE, ingest=ingest, now=NOW, **kw)  # type: ignore[arg-type]


def _tree(root: Path) -> dict[str, bytes]:
    """Every regular file under ``root``, relative path → bytes."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def test_scan_finds_matching_files_oldest_first(tmp_path: Path) -> None:
    _drop(tmp_path, "b.png", b"b", age_s=100)
    _drop(tmp_path, "a.JPG", b"a", age_s=200)
    _drop(tmp_path, "c.jpeg", b"c", age_s=300)

    found = scan(_folder(tmp_path), since=SINCE)

    assert [p.name for p in found] == ["c.jpeg", "a.JPG", "b.png"]


def test_scan_skips_other_extensions(tmp_path: Path) -> None:
    _drop(tmp_path, "a.png", b"a")
    _drop(tmp_path, "notes.txt", b"t")
    _drop(tmp_path, "noext", b"n")

    assert [p.name for p in scan(_folder(tmp_path), since=SINCE)] == ["a.png"]


def test_scan_never_follows_or_returns_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = _drop(outside, "secret.png", b"s")
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "link.png").symlink_to(target)
    (drop / "dangling.png").symlink_to(outside / "gone.png")

    assert scan(_folder(drop), since=EPOCH) == []


def test_scan_skips_icloud_stubs_and_hidden_files(tmp_path: Path) -> None:
    _drop(tmp_path, ".shot.png.icloud", b"stub")
    _drop(tmp_path, ".Screenshot in progress.png", b"tmp")
    _drop(tmp_path, "real.png", b"r")

    assert [p.name for p in scan(_folder(tmp_path), since=SINCE)] == ["real.png"]


def test_scan_skips_a_dataless_file(tmp_path: Path) -> None:
    _drop(tmp_path, "evicted.png", b"e")
    _drop(tmp_path, "local.png", b"l")

    def lstat(path: Any) -> Any:
        st = os.lstat(path)
        if Path(path).name != "evicted.png":
            return st
        return _FakeStat(st, st_flags=SF_DATALESS)

    found = scan(_folder(tmp_path), since=SINCE, lstat=lstat)

    assert [p.name for p in found] == ["local.png"]


def test_scan_keeps_a_file_with_other_flags(tmp_path: Path) -> None:
    _drop(tmp_path, "flagged.png", b"f")

    def lstat(path: Any) -> Any:
        return _FakeStat(os.lstat(path), st_flags=0x1 | 0x8000)

    assert [p.name for p in scan(_folder(tmp_path), since=SINCE, lstat=lstat)] == [
        "flagged.png"
    ]


def test_sf_dataless_is_the_macos_value() -> None:
    assert SF_DATALESS == 0x40000000
    assert getattr(stat, "SF_DATALESS", SF_DATALESS) == SF_DATALESS


class _FakeStat:
    """An ``os.stat_result`` stand-in with chosen ``st_flags``."""

    def __init__(self, real: os.stat_result, *, st_flags: int) -> None:
        self._real = real
        self.st_flags = st_flags

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_scan_never_enters_subdirectories(tmp_path: Path) -> None:
    (tmp_path / IMPORTED_DIR).mkdir()
    _drop(tmp_path / IMPORTED_DIR, "old-1a2b3c4d.png", b"o")
    (tmp_path / "sub").mkdir()
    _drop(tmp_path / "sub", "nested.png", b"n")
    (tmp_path / "dir.png").mkdir()

    assert scan(_folder(tmp_path), since=EPOCH) == []


def test_scan_honours_since(tmp_path: Path) -> None:
    _drop(tmp_path, "old.png", b"o", age_s=3 * 86400)
    _drop(tmp_path, "edge.png", b"e", age_s=2 * 86400)
    _drop(tmp_path, "new.png", b"n", age_s=86400)

    found = scan(_folder(tmp_path), since=SINCE)

    assert [p.name for p in found] == ["edge.png", "new.png"]


def test_scan_honours_until(tmp_path: Path) -> None:
    _drop(tmp_path, "settled.png", b"s", age_s=10)
    _drop(tmp_path, "fresh.png", b"f", age_s=1)
    until = datetime.fromtimestamp(NOW - SETTLE_S, UTC)

    found = scan(_folder(tmp_path), since=SINCE, until=until)

    assert [p.name for p in found] == ["settled.png"]


def test_scan_skips_a_file_that_vanishes_mid_scan(tmp_path: Path) -> None:
    _drop(tmp_path, "gone.png", b"g")
    _drop(tmp_path, "here.png", b"h")

    def lstat(path: Any) -> Any:
        if Path(path).name == "gone.png":
            raise FileNotFoundError(errno.ENOENT, "gone")
        return os.lstat(path)

    assert [p.name for p in scan(_folder(tmp_path), since=SINCE, lstat=lstat)] == ["here.png"]


# ---------------------------------------------------------------------------
# run: the move-first ingest
# ---------------------------------------------------------------------------


def test_run_moves_then_ingests_from_the_final_path_with_the_original_title(
    tmp_path: Path,
) -> None:
    body = b"synthetic png bytes"
    _drop(tmp_path, "Screenshot 1.png", body)
    ingest = _Ingest()

    report = _run(_folder(tmp_path), ingest)

    final = tmp_path / IMPORTED_DIR / f"Screenshot 1-{_sha8(body)}.png"
    deadline = pytest.approx(drop_folder.RUN_DEADLINE_S, abs=5)
    assert ingest.calls == [(final, "Screenshot 1", deadline)]
    assert ingest.seen_bytes == [body]
    assert _tree(tmp_path) == {f"{IMPORTED_DIR}/{final.name}": body}
    assert [(o.name, o.status) for o in report.outcomes] == [("Screenshot 1.png", "ingested")]
    assert report.deferred == 0
    assert report.counts() == {"ingested": 1}


def test_the_extension_is_lowercased_in_the_imported_name(tmp_path: Path) -> None:
    _drop(tmp_path, "Shot.PNG", b"x")
    ingest = _Ingest()

    _run(_folder(tmp_path), ingest)

    assert ingest.calls[0][0].name == f"Shot-{_sha8(b'x')}.png"


def test_a_long_stem_is_cut_to_fit_a_file_name(tmp_path: Path) -> None:
    stem = "é" * 120  # 240 UTF-8 bytes
    _drop(tmp_path, f"{stem}.png", b"x")
    ingest = _Ingest()

    _run(_folder(tmp_path), ingest)

    final, title, _ = ingest.calls[0]
    assert len(final.name.encode("utf-8")) <= 255
    assert final.name.startswith("é" * 100)
    assert title == stem  # the title keeps the whole original stem


def test_imported_is_created_when_missing(tmp_path: Path) -> None:
    _drop(tmp_path, "a.png", b"a")

    _run(_folder(tmp_path), _Ingest())

    assert (tmp_path / IMPORTED_DIR).is_dir()


def test_two_different_files_with_one_name_in_two_runs_are_two_imports(tmp_path: Path) -> None:
    """AC3: the content hash in the name keeps them apart."""
    ingest = _Ingest()
    _drop(tmp_path, "New Recording 1.png", b"first")
    _run(_folder(tmp_path), ingest)
    _drop(tmp_path, "New Recording 1.png", b"second")
    _run(_folder(tmp_path), ingest)

    imported = tmp_path / IMPORTED_DIR
    assert [c[0] for c in ingest.calls] == [
        imported / f"New Recording 1-{_sha8(b'first')}.png",
        imported / f"New Recording 1-{_sha8(b'second')}.png",
    ]
    assert [c[1] for c in ingest.calls] == ["New Recording 1", "New Recording 1"]
    assert _tree(tmp_path) == {
        f"{IMPORTED_DIR}/New Recording 1-{_sha8(b'first')}.png": b"first",
        f"{IMPORTED_DIR}/New Recording 1-{_sha8(b'second')}.png": b"second",
    }


def test_the_same_file_dropped_twice_is_a_no_op_the_second_time(tmp_path: Path) -> None:
    """AC4: the drop copy goes, the earlier import is untouched, nothing is ingested."""
    ingest = _Ingest()
    _drop(tmp_path, "shot.png", b"same")
    _run(_folder(tmp_path), ingest)
    final = tmp_path / IMPORTED_DIR / f"shot-{_sha8(b'same')}.png"
    before = os.stat(final)
    _drop(tmp_path, "shot.png", b"same")

    report = _run(_folder(tmp_path), ingest)

    assert len(ingest.calls) == 1
    assert [(o.name, o.status) for o in report.outcomes] == [("shot.png", "duplicate")]
    assert _tree(tmp_path) == {f"{IMPORTED_DIR}/{final.name}": b"same"}
    after = os.stat(final)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_a_name_collision_with_different_bytes_touches_neither_file(tmp_path: Path) -> None:
    """``sha8`` is 32 bits: a matching name is checked against the full hash first."""
    body = b"dropped"
    (tmp_path / IMPORTED_DIR).mkdir()
    impostor = tmp_path / IMPORTED_DIR / f"shot-{_sha8(body)}.png"
    impostor.write_bytes(b"a different file entirely")
    _drop(tmp_path, "shot.png", body)
    ingest = _Ingest()

    report = _run(_folder(tmp_path), ingest)

    assert ingest.calls == []
    assert report.outcomes[0].status == "failed"
    assert _tree(tmp_path) == {
        "shot.png": body,
        f"{IMPORTED_DIR}/{impostor.name}": b"a different file entirely",
    }


def test_a_symlink_at_the_imported_name_is_never_followed_or_removed(tmp_path: Path) -> None:
    body = b"dropped"
    outside = tmp_path / "outside.png"
    outside.write_bytes(body)  # same bytes: a followed link would look like a duplicate
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / IMPORTED_DIR).mkdir()
    link = drop / IMPORTED_DIR / f"shot-{_sha8(body)}.png"
    link.symlink_to(outside)
    _drop(drop, "shot.png", body)

    report = _run(_folder(drop), _Ingest())

    assert report.outcomes[0].status == "failed"
    # Refused by the regular-file check itself, not by O_NOFOLLOW's ELOOP further on.
    assert report.outcomes[0].detail == f"a different file holds {link.name}"
    assert (drop / "shot.png").read_bytes() == body
    assert link.is_symlink()
    assert outside.read_bytes() == body


def test_a_failed_ingest_moves_the_file_back(tmp_path: Path) -> None:
    """AC5, first half."""
    body = b"fails"
    _drop(tmp_path, "shot.png", body)
    ingest = _Ingest(answers=[ValueError("could not ingest")])

    report = _run(_folder(tmp_path), ingest)

    assert _tree(tmp_path) == {"shot.png": body}
    assert (tmp_path / IMPORTED_DIR).is_dir()
    assert report.outcomes[0].status == "failed"
    assert report.outcomes[0].detail == "ValueError"
    assert "could not ingest" not in report.outcomes[0].detail


def test_a_failed_ingest_with_the_name_retaken_stays_in_imported(tmp_path: Path) -> None:
    """Move-back never overwrites: a new drop with the same name keeps its place."""
    body = b"first"
    _drop(tmp_path, "shot.png", body)
    newcomer = b"arrived during the ingest"

    def ingest_then_new_drop(conn: Any, path: Path, *, title: str, timeout: float) -> str:
        (tmp_path / "shot.png").write_bytes(newcomer)
        raise RuntimeError("boom")

    report = run(None, _folder(tmp_path), since=SINCE, ingest=ingest_then_new_drop, now=NOW)  # type: ignore[arg-type]

    outcome = report.outcomes[0]
    assert outcome.status == "failed"
    assert "left in imported/" in outcome.detail
    assert _tree(tmp_path) == {
        "shot.png": newcomer,
        f"{IMPORTED_DIR}/shot-{_sha8(body)}.png": body,
    }


def test_a_failure_on_an_existing_destination_never_moves_or_deletes_it(tmp_path: Path) -> None:
    """AC5, second half: only a move this run made is ever moved back."""
    body = b"same"
    ingest = _Ingest()
    _drop(tmp_path, "shot.png", body)
    _run(_folder(tmp_path), ingest)
    final = tmp_path / IMPORTED_DIR / f"shot-{_sha8(body)}.png"
    _drop(tmp_path, "shot.png", body)
    failing = _Ingest(answers=[RuntimeError("would fail")])

    _run(_folder(tmp_path), failing)

    assert failing.calls == []
    assert final.read_bytes() == body
    assert not (tmp_path / "shot.png").exists()


def test_an_empty_document_stays_in_imported(tmp_path: Path) -> None:
    """OCR ran and read nothing: the file is processed, never retried."""
    body = b"blank"
    _drop(tmp_path, "blank.png", body)

    report = _run(_folder(tmp_path), _Ingest(answers=["empty"]))

    assert report.outcomes[0].status == "empty"
    assert _tree(tmp_path) == {f"{IMPORTED_DIR}/blank-{_sha8(body)}.png": body}


def test_a_file_that_vanishes_before_the_move_is_reported_vanished(tmp_path: Path) -> None:
    _drop(tmp_path, "gone.png", b"g")
    ingest = _Ingest()

    def link(src: Path, dst: Path) -> None:
        raise FileNotFoundError(errno.ENOENT, "gone")

    report = _run(_folder(tmp_path), ingest, link=link)

    assert report.outcomes[0].status == "vanished"
    assert ingest.calls == []


def test_a_file_that_vanishes_before_hashing_is_reported_vanished(tmp_path: Path) -> None:
    path = _drop(tmp_path, "gone.png", b"g")
    ingest = _Ingest()

    def lstat(p: Any) -> Any:
        st = os.lstat(p)
        if Path(p) == path:
            path.unlink()  # gone between the scan and the hash
        return st

    report = _run(_folder(tmp_path), ingest, lstat=lstat)

    assert report.outcomes[0].status == "vanished"
    assert ingest.calls == []


def test_a_file_swapped_for_a_symlink_after_the_scan_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"secret")
    drop = tmp_path / "drop"
    drop.mkdir()
    path = _drop(drop, "shot.png", b"real")
    ingest = _Ingest()

    def lstat(p: Any) -> Any:
        st = os.lstat(p)
        if Path(p) == path:
            path.unlink()
            path.symlink_to(outside)  # swapped between the scan and the read
        return st

    report = _run(_folder(drop), ingest, lstat=lstat)

    assert report.outcomes[0].status == "failed"
    assert ingest.calls == []
    assert (drop / "shot.png").is_symlink()
    assert outside.read_bytes() == b"secret"


def test_any_other_move_error_is_a_failure_and_the_file_stays(tmp_path: Path) -> None:
    _drop(tmp_path, "shot.png", b"x")

    def link(src: Path, dst: Path) -> None:
        raise PermissionError(errno.EACCES, "denied")

    report = _run(_folder(tmp_path), _Ingest(), link=link)

    assert report.outcomes[0].status == "failed"
    assert report.outcomes[0].detail == "PermissionError[EACCES]"
    assert _tree(tmp_path) == {"shot.png": b"x"}


# ---------------------------------------------------------------------------
# the copy fallback, for volumes with no hard links
# ---------------------------------------------------------------------------


def _no_hard_links(code: int) -> Callable[[Path, Path], None]:
    def link(src: Path, dst: Path) -> None:
        raise OSError(code, os.strerror(code))

    return link


@pytest.mark.parametrize(
    "code", [errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK, errno.ENOSYS]
)
def test_without_hard_links_the_move_copies_exclusively(tmp_path: Path, code: int) -> None:
    body = b"copied"
    src = _drop(tmp_path, "shot.png", body)
    mtime = src.stat().st_mtime
    ingest = _Ingest()

    report = _run(_folder(tmp_path), ingest, link=_no_hard_links(code))

    final = tmp_path / IMPORTED_DIR / f"shot-{_sha8(body)}.png"
    assert report.outcomes[0].status == "ingested"
    assert _tree(tmp_path) == {f"{IMPORTED_DIR}/{final.name}": body}
    assert final.stat().st_mtime == mtime


def test_the_copy_fallback_never_overwrites_a_destination(tmp_path: Path) -> None:
    body = b"same"
    (tmp_path / IMPORTED_DIR).mkdir()
    final = tmp_path / IMPORTED_DIR / f"shot-{_sha8(body)}.png"
    final.write_bytes(body)
    _drop(tmp_path, "shot.png", body)
    ingest = _Ingest()

    report = _run(_folder(tmp_path), ingest, link=_no_hard_links(errno.EXDEV))

    assert report.outcomes[0].status == "duplicate"
    assert ingest.calls == []
    assert _tree(tmp_path) == {f"{IMPORTED_DIR}/{final.name}": body}


def test_the_copy_fallback_moves_back_on_failure(tmp_path: Path) -> None:
    body = b"fails"
    _drop(tmp_path, "shot.png", body)

    report = _run(
        _folder(tmp_path),
        _Ingest(answers=[ValueError("x")]),
        link=_no_hard_links(errno.ENOTSUP),
    )

    assert report.outcomes[0].status == "failed"
    assert _tree(tmp_path) == {"shot.png": body}


def test_a_failed_copy_removes_its_partial_destination(tmp_path: Path) -> None:
    body = b"x" * 10
    _drop(tmp_path, "shot.png", body)

    def failing_write(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "full")

    report = _run(
        _folder(tmp_path), _Ingest(), link=_no_hard_links(errno.EXDEV), write=failing_write
    )

    assert report.outcomes[0].status == "failed"
    assert report.outcomes[0].detail == "OSError[ENOSPC]"
    assert _tree(tmp_path) == {"shot.png": body}


# ---------------------------------------------------------------------------
# the folder itself
# ---------------------------------------------------------------------------


def test_a_symlinked_imported_refuses_the_folder(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / IMPORTED_DIR).symlink_to(elsewhere)
    _drop(drop, "shot.png", b"x")
    ingest = _Ingest()

    with pytest.raises(DropFolderError):
        _run(_folder(drop), ingest)

    assert ingest.calls == []
    assert (drop / "shot.png").exists()
    assert list(elsewhere.iterdir()) == []


def test_a_dangling_imported_symlink_refuses_the_folder(tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / IMPORTED_DIR).symlink_to(tmp_path / "nowhere")

    with pytest.raises(DropFolderError):
        _run(_folder(drop), _Ingest())

    assert not (tmp_path / "nowhere").exists()


def test_a_file_named_imported_refuses_the_folder(tmp_path: Path) -> None:
    (tmp_path / IMPORTED_DIR).write_bytes(b"not a directory")

    with pytest.raises(DropFolderError):
        _run(_folder(tmp_path), _Ingest())


def test_capture_mode_is_refused_until_rm39(tmp_path: Path) -> None:
    with pytest.raises(DropFolderError, match="RM-39"):
        _run(_folder(tmp_path, mode="capture", kind=None), _Ingest())

    assert not (tmp_path / IMPORTED_DIR).exists()


def test_an_ingest_folder_needs_a_kind(tmp_path: Path) -> None:
    with pytest.raises(DropFolderError):
        _run(_folder(tmp_path, kind=None), _Ingest())


# ---------------------------------------------------------------------------
# the per-run bounds leave untouched files exactly in place
# ---------------------------------------------------------------------------


def test_the_file_cap_defers_the_rest_untouched(tmp_path: Path) -> None:
    for i in range(5):
        _drop(tmp_path, f"s{i}.png", f"body{i}".encode(), age_s=1000 - i)
    before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    ingest = _Ingest()

    report = _run(_folder(tmp_path), ingest, max_files=2)

    assert [c[1] for c in ingest.calls] == ["s0", "s1"]
    assert report.deferred == 3
    for name in ("s2.png", "s3.png", "s4.png"):
        st = (tmp_path / name).stat()
        assert (st.st_ino, st.st_mtime_ns) == before[name]


def test_the_deadline_defers_the_rest_untouched(tmp_path: Path) -> None:
    for i in range(4):
        _drop(tmp_path, f"s{i}.png", f"body{i}".encode(), age_s=1000 - i)
    before = {p.name: (p.stat().st_ino, p.read_bytes()) for p in tmp_path.iterdir()}
    # One clock read at the start, then one before each file.
    ticks = iter([0.0, 0.0, 100.0, 500.0])

    report = _run(_folder(tmp_path), _Ingest(), clock=lambda: next(ticks), deadline_s=480.0)

    assert report.counts() == {"ingested": 2}
    assert report.deferred == 2
    for name in ("s2.png", "s3.png"):
        st = (tmp_path / name).stat()
        assert (st.st_ino, (tmp_path / name).read_bytes()) == before[name]


def test_the_ingest_gets_what_is_left_of_the_deadline(tmp_path: Path) -> None:
    _drop(tmp_path, "a.png", b"a")
    ticks = iter([0.0, 300.0])
    ingest = _Ingest()

    _run(_folder(tmp_path), ingest, clock=lambda: next(ticks), deadline_s=480.0)

    assert ingest.calls[0][2] == pytest.approx(180.0)


def test_the_settle_window_leaves_a_fresh_file_for_the_next_run(tmp_path: Path) -> None:
    _drop(tmp_path, "fresh.png", b"f", age_s=SETTLE_S / 2)
    ingest = _Ingest()

    report = _run(_folder(tmp_path), ingest)

    assert ingest.calls == []
    assert report.outcomes == ()
    assert _tree(tmp_path) == {"fresh.png": b"f"}


def test_an_empty_folder_is_an_empty_report(tmp_path: Path) -> None:
    report = _run(_folder(tmp_path), _Ingest())

    assert report.outcomes == ()
    assert report.deferred == 0
    assert report.counts() == {}
    assert report.failed is False


def test_report_failed_is_true_when_any_file_failed(tmp_path: Path) -> None:
    _drop(tmp_path, "a.png", b"a", age_s=200)
    _drop(tmp_path, "b.png", b"b", age_s=100)

    report = _run(_folder(tmp_path), _Ingest(answers=[ValueError("x"), "ingested"]))

    assert report.counts() == {"failed": 1, "ingested": 1}
    assert report.failed is True


def test_run_uses_the_wall_clock_by_default(tmp_path: Path) -> None:
    path = tmp_path / "now.png"
    path.write_bytes(b"n")  # mtime: now, inside the settle window
    ingest = _Ingest()

    run(None, _folder(tmp_path), since=SINCE, ingest=ingest)  # type: ignore[arg-type]

    assert ingest.calls == []


def test_a_destination_that_vanishes_during_the_duplicate_check_is_vanished(
    tmp_path: Path,
) -> None:
    """Another run moved its file back out of ``imported/`` between our link and check."""
    _drop(tmp_path, "shot.png", b"x")

    def link(src: Path, dst: Path) -> None:
        raise FileExistsError(errno.EEXIST, "taken")  # ... and gone again by the lstat

    report = _run(_folder(tmp_path), _Ingest(), link=link)

    assert report.outcomes[0].status == "vanished"
    assert _tree(tmp_path) == {"shot.png": b"x"}


def test_an_unreadable_destination_is_a_failure_touching_neither(tmp_path: Path) -> None:
    body = b"x"
    (tmp_path / IMPORTED_DIR).mkdir()
    final = tmp_path / IMPORTED_DIR / f"shot-{_sha8(body)}.png"
    final.write_bytes(body)
    final.chmod(0)
    _drop(tmp_path, "shot.png", body)
    try:
        report = _run(_folder(tmp_path), _Ingest())
    finally:
        final.chmod(0o600)

    assert report.outcomes[0].status == "failed"
    assert report.outcomes[0].detail == "PermissionError[EACCES]"
    assert _tree(tmp_path) == {"shot.png": body, f"{IMPORTED_DIR}/{final.name}": body}
