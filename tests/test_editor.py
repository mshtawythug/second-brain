"""Tests for `brain.editor` — the $VISUAL/$EDITOR/vi launch helper."""
import stat
from pathlib import Path

import pytest

from brain.editor import EditorError, find_editor, make_temp_file, run_editor_on


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_find_editor_prefers_visual(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUAL", "vim-from-visual")
    monkeypatch.setenv("EDITOR", "nano-from-editor")
    assert find_editor() == ["vim-from-visual"]


def test_find_editor_falls_back_to_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "nano-from-editor")
    assert find_editor() == ["nano-from-editor"]


def test_find_editor_splits_multi_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``VISUAL`` / ``EDITOR`` may carry args (e.g. ``code --wait``); split them."""
    monkeypatch.setenv("VISUAL", "code --wait --new-window")
    monkeypatch.delenv("EDITOR", raising=False)
    assert find_editor() == ["code", "--wait", "--new-window"]


def test_find_editor_falls_back_to_vi(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When $VISUAL/$EDITOR are unset, fall back to `vi` resolved on PATH."""
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    fake_vi = tmp_path / "vi"
    _write_executable(fake_vi, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert find_editor() == [str(fake_vi)]


def test_find_editor_raises_when_nothing_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(EditorError):
        find_editor()


def test_run_editor_on_executes_multi_token_editor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: a user with EDITOR='wrapper --flag' must reach the wrapper.

    We point ``EDITOR`` at a wrapper script plus a flag; the script writes a
    sentinel into the file plus the flag it received as $1, so the test can
    verify both that the launch succeeded and that the flag was forwarded.
    """
    wrapper = tmp_path / "wrapper.sh"
    _write_executable(
        wrapper,
        '#!/bin/sh\n'
        'flag="$1"\n'
        'shift\n'
        'target="$1"\n'
        'printf "wrote %s\\n" "$flag" > "$target"\n'
        'exit 0\n',
    )
    monkeypatch.setenv("EDITOR", f"{wrapper} --some-flag")
    monkeypatch.delenv("VISUAL", raising=False)
    payload = make_temp_file("seed", suffix=".test")
    try:
        rc = run_editor_on(payload)
        assert rc == 0
        assert payload.read_text() == "wrote --some-flag\n"
    finally:
        if payload.exists():
            payload.unlink()


def test_make_temp_file_unlinks_on_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If write_text raises, the mkstemp output should be removed."""
    created: list[Path] = []
    real_write = Path.write_text

    def boom(self: Path, *args: object, **kwargs: object) -> int:
        created.append(self)
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError, match="disk full"):
        make_temp_file("x")
    monkeypatch.setattr(Path, "write_text", real_write)  # restore for assert
    assert created  # the helper did try to write
    for p in created:
        assert not p.exists(), f"orphaned temp file left behind: {p}"
