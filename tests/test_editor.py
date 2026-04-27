"""Tests for `brain.editor` — the $VISUAL/$EDITOR/vi launch helper."""
import stat
from pathlib import Path

import pytest

from brain.editor import EditorError, find_editor, open_in_editor


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_find_editor_prefers_visual(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUAL", "vim-from-visual")
    monkeypatch.setenv("EDITOR", "nano-from-editor")
    assert find_editor() == "vim-from-visual"


def test_find_editor_falls_back_to_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "nano-from-editor")
    assert find_editor() == "nano-from-editor"


def test_find_editor_falls_back_to_vi(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When $VISUAL/$EDITOR are unset, fall back to `vi` resolved on PATH."""
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    fake_vi = tmp_path / "vi"
    _write_executable(fake_vi, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert find_editor() == str(fake_vi)


def test_find_editor_raises_when_nothing_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(EditorError):
        find_editor()


def test_open_in_editor_returns_text_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fake editor rewrites the file; open_in_editor returns the new text."""
    fake = tmp_path / "fake.sh"
    _write_executable(
        fake,
        "#!/bin/sh\ncat > \"$1\" <<'BRAIN_EOF'\nedited content\nBRAIN_EOF\nexit 0\n",
    )
    monkeypatch.setenv("EDITOR", str(fake))
    monkeypatch.delenv("VISUAL", raising=False)
    text, path = open_in_editor("seed")
    try:
        assert text == "edited content\n"
        assert path.exists()
    finally:
        if path.exists():
            path.unlink()


def test_open_in_editor_returns_none_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = tmp_path / "fake.sh"
    _write_executable(fake, "#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("EDITOR", str(fake))
    monkeypatch.delenv("VISUAL", raising=False)
    text, path = open_in_editor("seed")
    try:
        assert text is None
        assert path.exists()
    finally:
        if path.exists():
            path.unlink()
