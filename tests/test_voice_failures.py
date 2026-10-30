"""The whisper-failure ledger, :mod:`brain.ingest.voice_failures` (RM-38, sec-107 F5).

Every ledger lives in ``tmp_path``; every memo is a few synthetic bytes.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from brain.ingest import voice_failures
from brain.ingest.voice_failures import (
    MAX_ENTRIES,
    MAX_FAILURES,
    STATE_NAME,
    forget,
    record_failure,
)


def _memo(tmp_path: Path, body: bytes = b"synthetic memo", name: str = "a.m4a") -> Path:
    path = tmp_path / name
    path.write_bytes(body)
    return path


def _ledger(state: Path) -> dict[str, int]:
    return json.loads((state / STATE_NAME).read_text())


def test_failures_are_counted_per_content_and_the_third_clears_the_entry(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    memo = _memo(tmp_path)
    digest = hashlib.sha256(b"synthetic memo").hexdigest()

    assert record_failure(state, memo) == 1
    assert _ledger(state) == {digest: 1}
    assert record_failure(state, memo) == 2
    assert record_failure(state, memo) == MAX_FAILURES == 3
    assert _ledger(state) == {}


def test_the_same_bytes_under_another_name_share_a_count(tmp_path: Path) -> None:
    state = tmp_path / "state"

    record_failure(state, _memo(tmp_path, name="a.m4a"))

    assert record_failure(state, _memo(tmp_path, name="b-1234abcd.m4a")) == 2


def test_different_bytes_are_counted_apart(tmp_path: Path) -> None:
    state = tmp_path / "state"

    record_failure(state, _memo(tmp_path, b"one", "a.m4a"))

    assert record_failure(state, _memo(tmp_path, b"two", "b.m4a")) == 1


def test_forget_clears_one_memo_s_count(tmp_path: Path) -> None:
    state = tmp_path / "state"
    kept = _memo(tmp_path, b"kept", "k.m4a")
    record_failure(state, kept)
    memo = _memo(tmp_path)
    record_failure(state, memo)

    forget(state, memo)

    assert _ledger(state) == {hashlib.sha256(b"kept").hexdigest(): 1}


def test_forget_with_no_ledger_reads_nothing(tmp_path: Path) -> None:
    """A success on a machine whose memos never failed must not hash a 1 GiB file."""
    state = tmp_path / "state"

    forget(state, tmp_path / "never-opened.m4a")

    assert not state.exists()


def test_forget_of_an_uncounted_memo_leaves_the_ledger_alone(tmp_path: Path) -> None:
    state = tmp_path / "state"
    record_failure(state, _memo(tmp_path, b"other", "o.m4a"))
    before = (state / STATE_NAME).stat().st_mtime_ns

    forget(state, _memo(tmp_path))

    assert (state / STATE_NAME).stat().st_mtime_ns == before


MEMO_DIGEST = hashlib.sha256(b"synthetic memo").hexdigest()


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[1, 2]",
        f'{{"{MEMO_DIGEST}": "x"}}',
        f'{{"{MEMO_DIGEST}": true}}',
        f'{{"{MEMO_DIGEST}": -1}}',
        f'{{"{MEMO_DIGEST}": 1, "other": 0}}',
    ],
)
def test_an_unreadable_ledger_starts_over(tmp_path: Path, text: str) -> None:
    """A bad value under this memo's own digest must not become its count."""
    state = tmp_path / "state"
    state.mkdir()
    (state / STATE_NAME).write_text(text)

    assert record_failure(state, _memo(tmp_path)) == 1


def test_the_ledger_keeps_at_most_max_entries_dropping_the_oldest(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    entries = {f"{i:064x}": 1 for i in range(MAX_ENTRIES)}
    (state / STATE_NAME).write_text(json.dumps(entries))

    record_failure(state, _memo(tmp_path))

    ledger = _ledger(state)
    assert len(ledger) == MAX_ENTRIES
    assert f"{0:064x}" not in ledger
    assert hashlib.sha256(b"synthetic memo").hexdigest() in ledger


def test_the_state_dir_is_private_and_no_temp_file_is_left(tmp_path: Path) -> None:
    state = tmp_path / "state"

    record_failure(state, _memo(tmp_path))

    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert sorted(p.name for p in state.iterdir()) == [STATE_NAME]


def test_a_symlinked_memo_is_not_hashed(tmp_path: Path) -> None:
    link = tmp_path / "link.m4a"
    link.symlink_to(_memo(tmp_path))

    with pytest.raises(OSError):
        record_failure(tmp_path / "state", link)


def test_a_large_memo_is_hashed_in_chunks(tmp_path: Path) -> None:
    body = os.urandom(voice_failures.CHUNK_BYTES * 2 + 3)

    record_failure(tmp_path / "state", _memo(tmp_path, body))

    assert _ledger(tmp_path / "state") == {hashlib.sha256(body).hexdigest(): 1}


def test_a_failed_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"

    def refuse(src: str, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(voice_failures.os, "replace", refuse)

    with pytest.raises(OSError):
        record_failure(state, _memo(tmp_path))

    assert list(state.iterdir()) == []
