"""`brain doctor`'s ``github`` row (roadmap ticket RM-31): is the ``gh`` CLI on ``PATH``?

``brain ingest-github`` shells out to ``gh`` and uses its login; without it the
command exits 1 with an install hint. The row is a WARN at worst, never a FAIL:
a missing ``gh`` disables one optional source, as a missing ``gws`` does for
Gmail. It runs no ``gh`` command (``gh auth status`` would call the code host),
so it costs nothing and reads no account.

A module of its own, as :mod:`brain.doctor_reranker` is, so the probe does not
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

_NAME = "github"


def github_doctor_check(
    *, which: Callable[[str], str | None] | None = None
) -> list[_DoctorCheck]:
    """The ``github`` row. ``which`` is a test seam (default ``shutil.which``)."""
    if (which or shutil.which)("gh") is None:
        return [
            _check(
                _NAME,
                "warn",
                "gh CLI not on PATH; brain ingest-github is disabled",
                remedy="brew install gh && gh auth login",
            )
        ]
    return [_check(_NAME, "ok", "gh CLI on PATH, for brain ingest-github")]
