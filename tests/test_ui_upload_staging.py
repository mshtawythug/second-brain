"""The upload staging area (Ingest spec §7.3, §7.4, RM-13).

Disk only, no server and no database: every root is created under ``tmp_path``, and
the clock is injected, so expiry is tested without a sleep. The stream is a plain
async generator driven with ``asyncio.run``.
"""
from __future__ import annotations

import asyncio
import os
import stat
import threading
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from brain.ui.errors import (
    UiNotFound,
    UiPayloadTooLarge,
    UiTooManyRequests,
    UiUnavailable,
    UiUnsupportedMedia,
)
from brain.ui.ingest_limits import (
    MAX_STAGED_UPLOADS,
    MAX_TEXT_UPLOAD_BYTES,
    STAGING_BLOCK_BYTES,
    UPLOAD_TTL,
)
from brain.ui.upload_checks import SafeName
from brain.ui.upload_staging import (
    ROOT_PREFIX,
    UploadStaging,
    pid_alive,
    sweep_stale_roots,
)

_NAME = SafeName("synthetic", ".txt")
_B = STAGING_BLOCK_BYTES
#: In range (≤ 2**22) and above every real pid_max on macOS; on Linux a pid this
#: high is possible only on a host configured to the maximum, and is not in use.
_DEAD_PID = 2**22 - 1


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
    assert staging.used_bytes() == _B  # charged a whole block (sec-77 #3)


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
        raise RuntimeError("client went away")

    with pytest.raises(RuntimeError):
        asyncio.run(staging.stage(_NAME, None, broken()))

    assert list(staging.root.iterdir()) == []
    assert staging.used_bytes() == 0


def test_the_budget_is_checked_from_the_declared_length(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path, budget=2 * _B)
    _stage(staging, b"a" * (_B + 1))  # charged two blocks: the whole budget

    async def never() -> AsyncIterator[bytes]:
        raise AssertionError("the body was read")
        yield b""  # pragma: no cover

    with pytest.raises(UiTooManyRequests) as caught:
        asyncio.run(staging.stage(_NAME, 1, never()))

    assert caught.value.code == "staging_full"
    assert len(list(staging.root.iterdir())) == 1


def test_the_budget_is_rechecked_by_the_counter(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path, budget=2 * _B)
    _stage(staging, b"a" * _B)

    with pytest.raises(UiTooManyRequests):
        _stage(staging, b"b" * (_B + 1), declared=None, parts=4)

    assert len(list(staging.root.iterdir())) == 1
    assert staging.used_bytes() == _B


def test_claimed_uploads_count_against_the_budget_until_released(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path, budget=2 * _B)
    up = _stage(staging, b"a" * (_B + 1))
    staging.claim(up.upload_id)

    with pytest.raises(UiTooManyRequests):
        _stage(staging, b"b")
    staging.release(up.upload_id)

    assert not up.path.parent.exists()
    assert staging.used_bytes() == 0
    _stage(staging, b"b")


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
    dead = tmp_path / f"{ROOT_PREFIX}{_DEAD_PID}-abc"
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
    """Both directions (aud-77 A2): dead only on ProcessLookupError."""
    assert pid_alive(os.getpid())
    assert pid_alive(os.getppid())
    # init/launchd: kill(1, 0) is a PermissionError for a non-root user (it exists,
    # it is not ours) and succeeds as root; alive either way.
    assert pid_alive(1)
    assert not pid_alive(2**22 + 12345)  # above macOS and Linux pid_max defaults
    assert not pid_alive(_DEAD_PID)


def test_the_sweep_never_removes_its_own_root(tmp_path: Path) -> None:
    """Even when every pid reads as dead, this process's own root is kept."""
    mine = tmp_path / f"{ROOT_PREFIX}{os.getpid()}-own"
    mine.mkdir()

    removed = sweep_stale_roots(tmp_path, own_pid=os.getpid(), is_alive=lambda pid: False)

    assert removed == []
    assert mine.exists()


# --- sec-77 (PR #77 security review) regressions ---------------------------------


def test_a_tiny_upload_is_charged_one_block_and_a_big_one_whole_blocks(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)

    _stage(staging, b"")
    _stage(staging, b"x" * (_B + 1))

    assert staging.used_bytes() == _B + 2 * _B


def test_the_number_of_staged_uploads_is_capped(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path, max_uploads=3)
    first = _stage(staging, b"1")
    _stage(staging, b"2")
    staging.claim(_stage(staging, b"3").upload_id)  # a claimed upload still counts

    with pytest.raises(UiTooManyRequests) as caught:
        _stage(staging, b"4")

    assert caught.value.code == "too_many_uploads"
    assert len(list(staging.root.iterdir())) == 3
    staging.discard(first.upload_id)
    _stage(staging, b"4")


def test_the_default_upload_count_cap_is_sixteen() -> None:
    assert MAX_STAGED_UPLOADS == 16


@pytest.mark.parametrize("digits", ["9999999999", "99999999999999999999", "4194305"])
def test_a_huge_or_out_of_range_pid_is_left_alone_and_does_not_raise(
    tmp_path: Path, digits: str
) -> None:
    """``mkdir $TMPDIR/brain-ui-ingest-9999999999-x`` used to make ``os.kill`` raise
    OverflowError and stop ``brain ui`` from starting (sec-77 #1)."""
    planted = tmp_path / f"{ROOT_PREFIX}{digits}-x"
    planted.mkdir()

    assert sweep_stale_roots(tmp_path, own_pid=os.getpid()) == []
    assert planted.exists()


@pytest.mark.parametrize("pid", [2**64, -1, 0])
def test_pid_alive_treats_a_pid_the_os_cannot_look_up_as_alive(pid: int) -> None:
    assert pid_alive(pid) is True


def test_the_sweep_skips_a_root_this_user_does_not_own(tmp_path: Path) -> None:
    """Simulated with ``own_uid``: chown needs root. The temp directory can be shared."""
    theirs = tmp_path / f"{ROOT_PREFIX}{_DEAD_PID}-theirs"
    theirs.mkdir()

    removed = sweep_stale_roots(
        tmp_path, own_pid=os.getpid(), own_uid=os.getuid() + 1, is_alive=lambda pid: False
    )

    assert removed == []
    assert theirs.exists()


def test_the_sweep_skips_a_symlinked_or_file_entry_whatever_it_points_at(tmp_path: Path) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("x")
    (tmp_path / f"{ROOT_PREFIX}{_DEAD_PID}-link").symlink_to(precious, target_is_directory=True)
    (tmp_path / f"{ROOT_PREFIX}{_DEAD_PID}-file").write_text("not a directory")

    removed = sweep_stale_roots(tmp_path, own_pid=os.getpid(), is_alive=lambda pid: False)

    assert removed == []
    assert (precious / "keep.txt").exists()


def test_a_sweep_that_raises_is_logged_by_class_and_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def exploding(*args: object, **kwargs: object) -> list[Path]:
        raise PermissionError(13, "synthetic", str(tmp_path / "secret-name"))

    staging, _ = _staging(tmp_path, sweeper=exploding)
    with caplog.at_level("WARNING", logger="brain.ui.upload_staging"):
        assert staging.sweep() == []

    assert "PermissionError" in caplog.text and "secret-name" not in caplog.text


def test_a_stream_oserror_is_a_fixed_503_and_leaves_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    staging, _ = _staging(tmp_path)

    async def broken() -> AsyncIterator[bytes]:
        yield b"partial"
        raise ConnectionResetError(54, "reset", "zorblax-name.txt")

    with caplog.at_level("DEBUG"), pytest.raises(UiUnavailable) as caught:
        asyncio.run(staging.stage(SafeName("zorblax-name", ".txt"), None, broken()))

    assert caught.value.code == "staging_failed"
    assert "zorblax" not in caught.value.message and "zorblax" not in caplog.text
    assert list(staging.root.iterdir()) == []
    assert staging.used_bytes() == 0


def test_a_filesystem_refusal_is_a_fixed_503_with_no_filename_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A read-only root makes ``mkdir`` fail with a real OSError: no patching."""
    staging, _ = _staging(tmp_path)
    staging.root.chmod(0o500)
    try:
        with caplog.at_level("DEBUG"), pytest.raises(UiUnavailable) as caught:
            _stage(staging, b"text", name=SafeName("zorblax-quarterly", ".txt"))
    finally:
        staging.root.chmod(0o700)

    assert caught.value.code == "staging_failed"
    assert "OSError" in caplog.text or "PermissionError" in caplog.text
    assert "zorblax" not in caplog.text
    assert staging.used_bytes() == 0


def test_opening_writing_and_sniffing_run_off_the_event_loop_thread(tmp_path: Path) -> None:
    """A 25 MiB write or a 5 MiB UTF-8 check on the loop would stall every other
    request (sec-77 #5). The seams record the thread each call ran on."""
    threads: dict[str, set[int]] = {"open": set(), "write": set(), "sniff": set()}

    class _Recording:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def write(self, data: bytes) -> int:
            threads["write"].add(threading.get_ident())
            return int(self.inner.write(data))

        def close(self) -> None:
            self.inner.close()

    def opener(directory: Path, path: Path) -> Any:
        threads["open"].add(threading.get_ident())
        directory.mkdir(mode=0o700)
        return _Recording(path.open("wb"))

    def sniffer(path: Path, ext: str) -> None:
        threads["sniff"].add(threading.get_ident())

    staging, _ = _staging(tmp_path, opener=opener, sniffer=sniffer)

    async def run() -> int:
        loop_thread = threading.get_ident()
        await staging.stage(_NAME, 9, _chunks([b"abc", b"def", b"ghi"]))
        return loop_thread

    loop_thread = asyncio.run(run())

    for call, seen in threads.items():
        assert seen, call
        assert loop_thread not in seen, f"{call} ran on the event loop thread"


def test_removing_a_regular_file_logs_by_class_and_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``_remove`` expects a directory; anything else is logged, never raised (aud-77 A3)."""
    from brain.ui.upload_staging import _remove

    planted = tmp_path / "zorblax-not-a-directory"
    planted.write_text("x")
    with caplog.at_level("WARNING", logger="brain.ui.upload_staging"):
        _remove(planted)

    assert "NotADirectoryError" in caplog.text or "OSError" in caplog.text
    assert "zorblax" not in caplog.text


def test_removing_a_missing_directory_is_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from brain.ui.upload_staging import _remove

    with caplog.at_level("DEBUG", logger="brain.ui.upload_staging"):
        _remove(tmp_path / "gone")

    assert caplog.text == ""


def test_the_sweep_of_a_missing_parent_returns_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="brain.ui.upload_staging"):
        assert sweep_stale_roots(tmp_path / "missing", own_pid=os.getpid()) == []

    assert "FileNotFoundError" in caplog.text and "missing" not in caplog.text


def test_an_entry_that_cannot_be_lstat_is_left_alone(tmp_path: Path) -> None:
    """A parent this user can list (r) but not search (x): ``iterdir`` names the
    entry and ``lstat`` fails. The sweep skips it rather than guessing."""
    parent = tmp_path / "listable"
    (parent / f"{ROOT_PREFIX}{_DEAD_PID}-x").mkdir(parents=True)
    parent.chmod(0o400)
    try:
        removed = sweep_stale_roots(parent, own_pid=os.getpid(), is_alive=lambda pid: False)
    finally:
        parent.chmod(0o700)

    assert removed == []
    assert (parent / f"{ROOT_PREFIX}{_DEAD_PID}-x").exists()
