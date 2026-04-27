"""Thin helper for opening text in the user's terminal editor ($VISUAL/$EDITOR/vi)."""
import contextlib
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


class EditorError(Exception):
    """No usable editor was found on $VISUAL, $EDITOR, or PATH."""


def find_editor() -> list[str]:
    """Resolve the editor command from $VISUAL → $EDITOR → ``vi`` on PATH.

    Returns the command as a list of argv tokens — ``$VISUAL`` and ``$EDITOR``
    are split via :func:`shlex.split` so multi-token values like
    ``code --wait`` or ``vim -p`` work as users expect.

    Raises :class:`EditorError` if none of the three is available.
    """
    for env in ("VISUAL", "EDITOR"):
        cmd = os.environ.get(env)
        if cmd:
            return shlex.split(cmd)
    vi = shutil.which("vi")
    if vi:
        return [vi]
    raise EditorError(
        "no editor available — set $VISUAL or $EDITOR, or install vi on PATH"
    )


def run_editor_on(path: Path) -> int:
    """Run the resolved editor against ``path`` and return its exit code.

    Raises :class:`EditorError` if no editor is configured *or* if the
    configured editor binary cannot be found / executed (e.g. a typo in
    ``$EDITOR``) — the bare ``FileNotFoundError`` from ``subprocess.run``
    would otherwise leak as an uncaught traceback.
    """
    argv = find_editor()
    try:
        completed = subprocess.run([*argv, str(path)], check=False)  # noqa: S603 - editor cmd from env
    except FileNotFoundError as e:
        raise EditorError(f"editor command not found: {argv[0]}") from e
    return completed.returncode


def make_temp_file(initial_text: str, *, suffix: str = ".brain.json") -> Path:
    """Create a temp file pre-populated with ``initial_text`` and return its path.

    Cleans up the temp file on the disk if the initial write fails, so callers
    don't leak orphaned ``mkstemp`` output on transient I/O errors.
    """
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    path = Path(name)
    try:
        path.write_text(initial_text, encoding="utf-8")
    except OSError:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return path
