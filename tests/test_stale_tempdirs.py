"""``brain.ingest.stale_tempdirs``: the sweep shared by voice memos and ingest-web."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from brain.ingest import stale_tempdirs
from brain.ingest.stale_tempdirs import sweep_stale_tempdirs

NOW = 20_000_000.0
HOUR = 3600.0


def _aged(root: Path, name: str, age_s: float, *, directory: bool = True) -> Path:
    path = root / name
    if directory:
        path.mkdir()
    else:
        path.write_bytes(b"synthetic")
    os.utime(path, (NOW - age_s, NOW - age_s))
    return path


def test_only_old_directories_with_the_prefix_are_removed(tmp_path: Path) -> None:
    old = _aged(tmp_path, "brain-x-old", 5 * HOUR)
    young = _aged(tmp_path, "brain-x-young", 1 * HOUR)
    other = _aged(tmp_path, "brain-y-old", 5 * HOUR)
    a_file = _aged(tmp_path, "brain-x-file", 5 * HOUR, directory=False)

    removed = sweep_stale_tempdirs("brain-x-", max_age_s=3 * HOUR, root=tmp_path, now=NOW)

    assert removed == 1
    assert not old.exists()
    assert young.exists() and other.exists() and a_file.exists()


def test_another_users_directory_is_kept(tmp_path: Path) -> None:
    kept = _aged(tmp_path, "brain-x-old", 5 * HOUR)

    assert sweep_stale_tempdirs(
        "brain-x-", max_age_s=HOUR, root=tmp_path, now=NOW, uid=os.getuid() + 1
    ) == 0
    assert kept.exists()


def test_a_missing_root_is_quiet(tmp_path: Path) -> None:
    assert sweep_stale_tempdirs("brain-x-", max_age_s=HOUR, root=tmp_path / "gone") == 0


def test_an_entry_removed_during_the_sweep_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = _aged(tmp_path, "brain-x-gone", 5 * HOUR)
    real_scandir = os.scandir

    def scandir_then_remove(path: Any) -> Any:
        entries = list(real_scandir(path))
        if Path(path) == tmp_path:
            gone.rmdir()
        return iter(entries)

    monkeypatch.setattr(stale_tempdirs.os, "scandir", scandir_then_remove)

    assert sweep_stale_tempdirs("brain-x-", max_age_s=HOUR, root=tmp_path, now=NOW) == 0


def test_the_defaults_are_the_temp_dir_and_the_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile
    import time

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    old = tmp_path / "brain-x-old"
    old.mkdir()
    stamp = time.time() - 5 * HOUR
    os.utime(old, (stamp, stamp))

    assert sweep_stale_tempdirs("brain-x-", max_age_s=HOUR) == 1
    assert not old.exists()
