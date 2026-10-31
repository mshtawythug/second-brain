"""`brain doctor`'s ``git`` row (roadmap ticket RM-43): is ``git`` on ``PATH``?

``brain ingest-code`` asks ``git`` which files the owner touched; without it the
command exits 1 with an install hint. The row is a WARN at worst, never a FAIL:
a missing ``git`` disables one optional source, as a missing ``gh`` does for
``brain ingest-github``. It runs no ``git`` command, so it reads no repository.

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

_NAME = "git"


def git_doctor_check(
    *, which: Callable[[str], str | None] | None = None
) -> list[_DoctorCheck]:
    """The ``git`` row. ``which`` is a test seam (default ``shutil.which``)."""
    if (which or shutil.which)("git") is None:
        return [
            _check(
                _NAME,
                "warn",
                "git not on PATH; brain ingest-code is disabled",
                remedy="xcode-select --install (or brew install git)",
            )
        ]
    return [_check(_NAME, "ok", "git on PATH, for brain ingest-code")]
