"""`brain doctor`'s ``apple notes`` row (roadmap ticket RM-36): can ``osascript`` run here?

``brain ingest-apple-notes`` reads Notes through ``osascript``, a macOS
built-in, always at :data:`~brain.ingest.apple_notes.OSASCRIPT_PATH` (the command
never looks it up on ``PATH``). Off macOS the row is OK and says the command is
unavailable, because nothing is broken; on macOS a missing binary is a WARN (one
optional source disabled), never a FAIL. The row only checks the file: running
``osascript`` against Notes would raise the Automation permission prompt and
read the owner's notes, so it never does. The prompt itself is named in the
detail line, because it is the one thing the first real run will ask for.

A module of its own, as :mod:`brain.doctor_github` is, so the probe does not
spend :mod:`brain.doctor_runtime`'s line budget (roadmap spec §2.2);
``runtime_doctor_checks`` composes it in.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .doctor_runtime import _check
from .ingest.apple_notes import OSASCRIPT_PATH

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .cli import _DoctorCheck

_NAME = "apple notes"


def apple_notes_doctor_check(
    *, platform: str, is_file: Callable[[Path], bool] | None = None
) -> list[_DoctorCheck]:
    """The ``apple notes`` row. ``is_file`` is a test seam (default ``Path.is_file``)."""
    if platform != "darwin":
        return [_check(_NAME, "ok", "not macOS; brain ingest-apple-notes is unavailable here")]
    if not (is_file or Path.is_file)(Path(OSASCRIPT_PATH)):
        return [
            _check(
                _NAME,
                "warn",
                f"{OSASCRIPT_PATH} missing; brain ingest-apple-notes is disabled",
                remedy=f"check {OSASCRIPT_PATH}, which ships with macOS",
            )
        ]
    return [
        _check(
            _NAME,
            "ok",
            f"{OSASCRIPT_PATH} present, for brain ingest-apple-notes "
            "(its first run asks for Automation permission)",
        )
    ]
