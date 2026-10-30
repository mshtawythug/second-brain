"""The inbox job's refusals as fixed codes and messages, class-only (RM-40).

A pure leaf beside ``ingest_service`` (which runs the job and is the module permitted to
mutate), split out so that module stays under the file-size ceiling. Nothing here does
I/O. Every message is fixed text: never an exception's message, a file name or a path,
because ``run_one``'s failure detail can name the ``imported/`` file.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from ..errors import BrainError


class InboxRefused(BrainError):
    """The inbox job stopped before anything was ingested; ``code`` and ``message`` are fixed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#: ``describe_failure``'s shape: a class name, optionally ``[ERRNO]``. Anything else in
#: a ``FileOutcome.detail`` (it can name the ``imported/`` file) never reaches a job.
CLASS_ONLY = re.compile(r"[A-Za-z_]\w*(\[[A-Z0-9]+\])?")

#: ``run_one``'s refusals of the file it opened, by class: ``(code, message)``. A fresh
#: :class:`InboxRefused` is raised each time; an exception instance is never shared.
_OPEN_REFUSALS: Mapping[str, tuple[str, str]] = {
    "FileTooLargeError": (
        "inbox_file_too_large",
        "The file is over the size limit (it grew, or was replaced). Nothing was written.",
    ),
    "FileIdentityError": (
        "inbox_file_changed",
        "The file was replaced, or has another hard link, since it was checked. "
        "Nothing was written.",
    ),
    "DatalessFileError": (
        "file_not_on_disk", "The file is in iCloud, not on this disk. Nothing was written."
    ),
}

#: The job's re-check codes that keep their own meaning; any other refusal there means
#: the file changed since the submit.
_RECHECK_REFUSALS: Mapping[str, tuple[str, str]] = {
    "inbox_unreadable": (
        "inbox_unreadable", "The inbox folder could not be read. Nothing was moved."
    ),
    "inbox_file_too_large": _OPEN_REFUSALS["FileTooLargeError"],
    "file_not_on_disk": _OPEN_REFUSALS["DatalessFileError"],
}


def refused_open(detail: str) -> InboxRefused:
    """The job error for a ``run_one`` failure that never reached the ingest, class-only."""
    if detail in _OPEN_REFUSALS:
        return InboxRefused(*_OPEN_REFUSALS[detail])
    reason = detail if CLASS_ONLY.fullmatch(detail) else "it changed"
    return InboxRefused(
        "inbox_move_failed",
        f"The file could not be moved into imported/ ({reason}). Nothing was written.",
    )


def recheck_refused(code: str) -> InboxRefused:
    """The job error when its re-check refuses the file (``code`` is the route's)."""
    if code in _RECHECK_REFUSALS:
        return InboxRefused(*_RECHECK_REFUSALS[code])
    return InboxRefused(
        "inbox_file_changed",
        f"The file changed since it was listed ({code}). Nothing was moved.",
    )
