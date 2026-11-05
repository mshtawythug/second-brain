"""`brain doctor`'s ``fzf`` row (roadmap ticket RM-52): can ``brain search --pick`` run here?

A WARN at worst, never a FAIL, and a WARN leaves doctor's exit code at 0: a missing ``fzf`` disables
one optional flag, as a missing ``git`` does for ``brain ingest-code``. It reads ``--version`` only.

A module of its own, as :mod:`brain.doctor_git` is, so the probe does not spend
:mod:`brain.doctor_runtime`'s line budget (roadmap spec §2.2); ``runtime_doctor_checks``
composes it in.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from typing import TYPE_CHECKING

from .doctor_runtime import _check
from .search_pick import MIN_FZF_VERSION, installed_fzf_version

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .cli import _DoctorCheck

_NAME = "fzf"


def fzf_doctor_check(
    *,
    which: Callable[[str], str | None] | None = None,
    version: Callable[[str], tuple[int, int] | None] | None = None,
) -> list[_DoctorCheck]:
    """The ``fzf`` row. ``which`` and ``version`` are test seams."""
    binary = (which or shutil.which)("fzf")
    if binary is None:
        return [
            _check(
                _NAME,
                "warn",
                "fzf not on PATH; brain search --pick is disabled",
                remedy="brew install fzf (or your OS's package)",
            )
        ]
    found = (version or installed_fzf_version)(binary)
    if found is not None and found < MIN_FZF_VERSION:
        floor = f"{MIN_FZF_VERSION[0]}.{MIN_FZF_VERSION[1]}"
        return [
            _check(
                _NAME,
                "warn",
                f"fzf {found[0]}.{found[1]} is older than {floor}; brain search --pick refuses it",
                remedy="upgrade fzf (brew upgrade fzf)",
            )
        ]
    return [_check(_NAME, "ok", "fzf on PATH, for brain search --pick")]
