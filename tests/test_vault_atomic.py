"""Tests for ``brain.vault._atomic``.

The helper is small but it's the only thing standing between a partial
write and a corrupted vault file, so its three contracts (create new,
replace existing, clean up the temp on failure) each get a dedicated
test.
"""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from brain.vault._atomic import atomic_write_text


def test_writes_new_file(tmp_path: Path) -> None:
    target = tmp_path / "fresh.md"
    atomic_write_text(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    # No leftover tempfile in the same directory.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fresh.md"]


def test_replaces_existing_file_atomically(tmp_path: Path) -> None:
    target = tmp_path / "existing.md"
    target.write_text("old content\n", encoding="utf-8")
    atomic_write_text(target, "new content\n")
    assert target.read_text(encoding="utf-8") == "new content\n"
    # No leftover tempfile.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["existing.md"]


def test_failure_during_write_cleans_up_tempfile(tmp_path: Path) -> None:
    target = tmp_path / "willfail.md"
    target.write_text("original\n", encoding="utf-8")

    # Force the os.replace step to fail. The exception must propagate, the
    # original file must still hold its prior content, and no ``*.tmp``
    # sibling can be left behind.
    with patch(
        "brain.vault._atomic.os.replace",
        side_effect=OSError("simulated disk full"),
    ), pytest.raises(OSError, match="simulated disk full"):
        atomic_write_text(target, "would-be new content\n")

    assert target.read_text(encoding="utf-8") == "original\n"
    leftovers = sorted(p.name for p in tmp_path.iterdir())
    assert leftovers == ["willfail.md"], (
        f"stray temp file left behind: {leftovers!r}"
    )


def test_failure_before_tempfile_created_is_safe(tmp_path: Path) -> None:
    """If the write itself fails before ``os.replace`` runs, cleanup still
    completes without raising — :meth:`Path.unlink(missing_ok=True)` swallows
    the FileNotFoundError so the helper never re-raises a confusing
    second exception while handling the first.
    """
    target = tmp_path / "wontwrite.md"

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated write failure")

    with patch.object(Path, "write_text", boom), pytest.raises(
        OSError, match="simulated write failure"
    ):
        atomic_write_text(target, "anything\n")

    # No file created (target nor tempfile).
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_lands_in_same_directory(tmp_path: Path) -> None:
    """The temp file must be a sibling of the target so the rename never
    crosses a filesystem boundary. We assert the contract by patching
    ``os.replace`` to record its source path.
    """
    target = tmp_path / "sub" / "deep.md"
    target.parent.mkdir(parents=True)
    seen_src: list[str] = []

    real_replace = os.replace

    def recording(src: object, dst: object) -> None:
        seen_src.append(str(src))
        real_replace(src, dst)

    with patch("brain.vault._atomic.os.replace", recording):
        atomic_write_text(target, "deep content\n")

    assert len(seen_src) == 1
    # Tempfile sat next to the target, not in /tmp or anywhere else.
    assert Path(seen_src[0]).parent == target.parent
    assert target.read_text(encoding="utf-8") == "deep content\n"
