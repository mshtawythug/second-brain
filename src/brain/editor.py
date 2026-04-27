"""Thin helper for opening text in the user's terminal editor ($VISUAL/$EDITOR/vi)."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class EditorError(Exception):
    """No usable editor was found on $VISUAL, $EDITOR, or PATH."""


def find_editor() -> str:
    """Resolve the editor command from $VISUAL → $EDITOR → ``vi`` on PATH.

    Raises :class:`EditorError` if none of the three is available.
    """
    for env in ("VISUAL", "EDITOR"):
        cmd = os.environ.get(env)
        if cmd:
            return cmd
    vi = shutil.which("vi")
    if vi:
        return vi
    raise EditorError(
        "no editor available — set $VISUAL or $EDITOR, or install vi on PATH"
    )


def run_editor_on(path: Path) -> int:
    """Run the resolved editor against ``path`` and return its exit code."""
    cmd = find_editor()
    completed = subprocess.run([cmd, str(path)], check=False)  # noqa: S603 - editor cmd from env
    return completed.returncode


def make_temp_file(initial_text: str, *, suffix: str = ".brain.json") -> Path:
    """Create a temp file pre-populated with ``initial_text`` and return its path."""
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    path = Path(name)
    path.write_text(initial_text, encoding="utf-8")
    return path


def open_in_editor(
    initial_text: str, *, suffix: str = ".brain.json"
) -> tuple[str | None, Path]:
    """Edit ``initial_text`` interactively and return ``(final_text, temp_path)``.

    A new temp file is created with ``suffix`` and pre-populated with
    ``initial_text``. The user's editor is invoked against it. If the editor
    exits with a non-zero status, ``final_text`` is ``None`` (the caller should
    treat that as an aborted edit). Otherwise ``final_text`` is whatever the
    file contains after the editor exits.

    The caller owns ``temp_path`` and is responsible for unlinking it (the
    recovery flow in the CLI may reuse the same path for a second pass).
    """
    path = make_temp_file(initial_text, suffix=suffix)
    rc = run_editor_on(path)
    if rc != 0:
        return None, path
    return path.read_text(encoding="utf-8"), path
