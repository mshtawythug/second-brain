"""`brain doctor`'s ``apple notes`` row (roadmap ticket RM-36): can ``osascript`` run here?

``brain ingest-apple-notes`` reads Notes through ``osascript``, a macOS
built-in. Off macOS the row is OK and says the command is unavailable, because
nothing is broken; on macOS a missing ``osascript`` is a WARN (one optional
source disabled), never a FAIL. The row only looks the binary up: running
``osascript`` against Notes would raise the Automation permission prompt and
read the owner's notes, so it never does. The prompt itself is named in the
detail line, because it is the one thing the first real run will ask for.

A module of its own, as :mod:`brain.doctor_github` is, so the probe does not
spend :mod:`brain.doctor_runtime`'s line budget (roadmap spec §2.2);
``runtime_doctor_checks`` composes it in.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from typing import TYPE_CHECKING

from .doctor_runtime import _check

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .cli import _DoctorCheck

_NAME = "apple notes"


def apple_notes_doctor_check(
    *, platform: str, which: Callable[[str], str | None] | None = None
) -> list[_DoctorCheck]:
    """The ``apple notes`` row. ``which`` is a test seam (default ``shutil.which``)."""
    if platform != "darwin":
        return [_check(_NAME, "ok", "not macOS; brain ingest-apple-notes is unavailable here")]
    if (which or shutil.which)("osascript") is None:
        return [
            _check(
                _NAME,
                "warn",
                "osascript not on PATH; brain ingest-apple-notes is disabled",
                remedy="add /usr/bin to PATH",
            )
        ]
    return [
        _check(
            _NAME,
            "ok",
            "osascript on PATH, for brain ingest-apple-notes "
            "(its first run asks for Automation permission)",
        )
    ]
