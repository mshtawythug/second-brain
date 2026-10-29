"""The upload staging area (Ingest spec §7.3, §7.4, RM-13).

Disk only, no server and no database: every root is created under ``tmp_path``, and
the clock is injected, so expiry is tested without a sleep. The stream is a plain
async generator driven with ``asyncio.run``.
"""
from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from brain.ui.errors import UiNotFound, UiPayloadTooLarge, UiTooManyRequests, UiUnsupportedMedia
from brain.ui.ingest_limits import MAX_TEXT_UPLOAD_BYTES, UPLOAD_TTL
from brain.ui.upload_checks import SafeName
from brain.ui.upload_staging import (
    ROOT_PREFIX,
    UploadStaging,
    pid_alive,
    sweep_stale_roots,
)

_NAME = SafeName("synthetic", ".txt")


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def _chunks(parts: Iterable[bytes]) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def _staging(tmp_path: Path, **kw: object) -> tuple[UploadStaging, _Clock]:
    clock = _Clock()
    return UploadStaging(clock=clock, parent=tmp_path, **kw), clock  # type: ignore[arg-type]


def _stage(staging: UploadStaging, data: bytes, *, name: SafeName = _NAME,
           declared: int | None = -1, parts: int = 1):
    size = len(data)
    step = max(1, -(-size // parts))
    pieces = [data[i : i + step] for i in range(0, size, step)] or [b""]
    return asyncio.run(
        staging.stage(name, size if declared == -1 else declared, _chunks(pieces))
    )


def _files_under(path: Path) -> list[Path]:
    return sorted(p for p in path.rglob("*") if p.is_file())


def test_the_root_is_private_and_named_by_pid(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)

    assert staging.root.parent == tmp_path
    assert staging.root.name.startswith(f"{ROOT_PREFIX}{os.getpid()}-")
    assert stat.S_IMODE(staging.root.stat().st_mode) == 0o700


def test_a_staged_file_lands_at_root_id_basename_with_private_modes(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)

    up = _stage(staging, b"synthetic text\n", parts=3)

    assert up.path == staging.root / up.upload_id / "synthetic.txt"
    assert up.path.read_bytes() == b"synthetic text\n"
    assert up.size == 15 and up.kind == "text"
    assert len(up.upload_id) == 32 and int(up.upload_id, 16) >= 0
    assert stat.S_IMODE(up.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(up.path.stat().st_mode) == 0o600
    assert staging.used_bytes() == 15


def test_an_honest_length_over_the_cap_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    staging, _ = _staging(tmp_path)

    async def never() -> AsyncIterator[bytes]:
        raise AssertionError("the body was read")
        yield b""  # pragma: no cover

    with pytest.raises(UiPayloadTooLarge) as caught:
        asyncio.run(staging.stage(_NAME, MAX_TEXT_UPLOAD_BYTES + 1, never()))

    assert caught.value.code == "upload_too_large"
    assert _files_under(staging.root) == [] and list(staging.root.iterdir()) == []


@pytest.mark.parametrize("declared", [None, 10])
def test_a_lying_or_absent_length_is_caught_by_the_counter(
    tmp_path: Path, declared: int | None
) -> None:
    staging, _ = _staging(tmp_path)
    data = b"x" * (MAX_TEXT_UPLOAD_BYTES + 1)

    with pytest.raises(UiPayloadTooLarge) as caught:
        _stage(staging, data, declared=declared, parts=8)

    assert caught.value.code == "upload_too_large"
    assert list(staging.root.iterdir()) == []  # the partial file and its directory are gone
    assert staging.used_bytes() == 0


def test_exactly_the_cap_is_accepted(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)

    up = _stage(staging, b"x" * MAX_TEXT_UPLOAD_BYTES, parts=4)

    assert up.size == MAX_TEXT_UPLOAD_BYTES


def test_a_sniff_failure_deletes_the_file(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)

    with pytest.raises(UiUnsupportedMedia):
        _stage(staging, b"not a pdf", name=SafeName("paper", ".pdf"))

    assert list(staging.root.iterdir()) == []
    assert staging.used_bytes() == 0


def test_a_failing_stream_deletes_the_partial_file(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)

    async def broken() -> AsyncIterator[bytes]:
        yield b"partial"
        raise ConnectionResetError("client went away")

    with pytest.raises(ConnectionResetError):
        asyncio.run(staging.stage(_NAME, None, broken()))

    assert list(staging.root.iterdir()) == []
    assert staging.used_bytes() == 0


def test_the_budget_is_checked_from_the_declared_length(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path, budget=100)
    _stage(staging, b"a" * 60)

    async def never() -> AsyncIterator[bytes]:
        raise AssertionError("the body was read")
        yield b""  # pragma: no cover

    with pytest.raises(UiTooManyRequests) as caught:
        asyncio.run(staging.stage(_NAME, 41, never()))

    assert caught.value.code == "staging_full"
    assert len(list(staging.root.iterdir())) == 1


def test_the_budget_is_rechecked_by_the_counter(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path, budget=100)
    _stage(staging, b"a" * 60)

    with pytest.raises(UiTooManyRequests):
        _stage(staging, b"b" * 41, declared=None, parts=4)

    assert len(list(staging.root.iterdir())) == 1
    assert staging.used_bytes() == 60


def test_claimed_uploads_count_against_the_budget_until_released(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path, budget=100)
    up = _stage(staging, b"a" * 60)
    staging.claim(up.upload_id)

    with pytest.raises(UiTooManyRequests):
        _stage(staging, b"b" * 41)
    staging.release(up.upload_id)

    assert not up.path.parent.exists()
    assert staging.used_bytes() == 0
    _stage(staging, b"b" * 41)


def test_claim_makes_an_upload_unknown_to_a_second_claim_and_to_discard(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)
    up = _stage(staging, b"text")

    assert staging.claim(up.upload_id) == up
    for again in (staging.claim, staging.discard):
        with pytest.raises(UiNotFound) as caught:
            again(up.upload_id)
        assert caught.value.code == "upload_unknown"
    assert up.path.exists()


def test_unclaim_puts_an_upload_back(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)
    up = _stage(staging, b"text")
    staging.claim(up.upload_id)

    staging.unclaim(up.upload_id)

    assert staging.claim(up.upload_id) == up


def test_discard_deletes_an_unclaimed_upload(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)
    up = _stage(staging, b"text")

    staging.discard(up.upload_id)

    assert not up.path.parent.exists()
    with pytest.raises(UiNotFound):
        staging.discard(up.upload_id)


@pytest.mark.parametrize("bad", ["", "x", "../" + "a" * 29, "A" * 32, "a" * 33])
def test_a_malformed_id_is_unknown(tmp_path: Path, bad: str) -> None:
    staging, _ = _staging(tmp_path)

    for op in (staging.claim, staging.discard):
        with pytest.raises(UiNotFound):
            op(bad)


def test_release_is_idempotent(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)
    up = _stage(staging, b"text")
    staging.claim(up.upload_id)

    staging.release(up.upload_id)
    staging.release(up.upload_id)

    assert not up.path.parent.exists()


def test_an_unsubmitted_upload_expires_and_a_claimed_one_does_not(tmp_path: Path) -> None:
    staging, clock = _staging(tmp_path)
    idle = _stage(staging, b"idle")
    held = _stage(staging, b"held")
    staging.claim(held.upload_id)

    clock.now += UPLOAD_TTL + timedelta(seconds=1)
    staging.expire()

    assert not idle.path.parent.exists()
    with pytest.raises(UiNotFound):
        staging.claim(idle.upload_id)
    assert held.path.exists()


def test_expiry_runs_on_stage_and_on_claim(tmp_path: Path) -> None:
    staging, clock = _staging(tmp_path)
    idle = _stage(staging, b"idle")
    clock.now += UPLOAD_TTL + timedelta(seconds=1)

    _stage(staging, b"fresh")

    assert not idle.path.parent.exists()


def test_close_removes_the_whole_root(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)
    up = _stage(staging, b"text")
    root = up.path.parent.parent
    staging.claim(up.upload_id)

    staging.close()

    assert not root.exists()
    staging.release(up.upload_id)  # a job settling after shutdown is harmless


def test_no_root_exists_until_the_first_upload(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)
    staging.expire()
    staging.close()

    assert list(tmp_path.iterdir()) == []


def test_sweep_uses_the_parent_and_spares_this_process(tmp_path: Path) -> None:
    dead = tmp_path / f"{ROOT_PREFIX}{2**22 + 12345}-abc"
    dead.mkdir()
    staging, _ = _staging(tmp_path)
    up = _stage(staging, b"text")

    assert staging.sweep() == [dead]
    assert up.path.exists()


def test_nothing_is_written_outside_the_root(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)
    before = set(tmp_path.rglob("*"))

    _stage(staging, b"x", name=SafeName("x", ".txt"))

    after = set(tmp_path.rglob("*")) - before
    assert after and all(p == staging.root or staging.root in p.parents for p in after)


def test_the_sweep_removes_dead_roots_only(tmp_path: Path) -> None:
    """A dead pid's root goes; this process's root, a live pid's root and a root
    without a parseable pid are kept (§7.4, "Lifetime")."""
    dead = tmp_path / f"{ROOT_PREFIX}999999-abc"
    mine = tmp_path / f"{ROOT_PREFIX}{os.getpid()}-def"
    live = tmp_path / f"{ROOT_PREFIX}{os.getppid()}-ghi"
    odd = tmp_path / f"{ROOT_PREFIX}notapid-jkl"
    other = tmp_path / "unrelated-999999-x"
    for root in (dead, mine, live, odd, other):
        (root / "u").mkdir(parents=True)
        (root / "u" / "f.txt").write_text("x")

    removed = sweep_stale_roots(tmp_path, own_pid=os.getpid(), is_alive=lambda pid: pid != 999999)

    assert removed == [dead]
    assert not dead.exists()
    for kept in (mine, live, odd, other):
        assert kept.exists()


def test_the_sweep_never_follows_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    link = tmp_path / f"{ROOT_PREFIX}999999-link"
    link.symlink_to(target, target_is_directory=True)

    removed = sweep_stale_roots(tmp_path, own_pid=os.getpid(), is_alive=lambda pid: False)

    assert removed == []
    assert (target / "keep.txt").exists()


def test_pid_alive() -> None:
    assert pid_alive(os.getpid())
    assert pid_alive(os.getppid())
    assert not pid_alive(2**22 + 12345)  # above macOS and Linux pid_max defaults


def test_the_sweep_never_removes_its_own_root(tmp_path: Path) -> None:
    """Even when every pid reads as dead, this process's own root is kept."""
    mine = tmp_path / f"{ROOT_PREFIX}{os.getpid()}-own"
    mine.mkdir()

    removed = sweep_stale_roots(tmp_path, own_pid=os.getpid(), is_alive=lambda pid: False)

    assert removed == []
    assert mine.exists()
