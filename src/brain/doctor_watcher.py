"""`brain doctor` checks for the vault-sync watcher (A9) and retired LaunchAgents (R2).

``vault watcher``
    Spec §8.5 A9 / Q8: without ``brain vault sync --watch`` running, edits made
    outside ``brain ui`` (Obsidian, an editor) do not reach search, MCP or the
    UI until a manual ``brain vault sync``. On macOS launchd supervises the
    watcher, so the question is whether ``com.brain.watcher`` is installed.
    Whether it is loaded and healthy is the ``daemons`` check's job, so an
    installed watcher that is down FAILs there and is not reported twice.
    Elsewhere there is no launchd, so a process scan looks for it.
``watcher_state``
    The same detection, as the three-way answer ``brain ui``'s status line
    shows (A9's UI half, ``docs/specs/2026-10-15-ui-watcher-status-design.md``).
    It is a process scan on every platform, macOS included, because the UI's
    sentence is "not RUNNING" and it has no ``daemons`` line beside it.
``retired daemon``
    ``com.brain.build``, the Quartz rebuild daemon, is retired at R2.
    ``brain-down`` boots it out but leaves its plist, and the next
    ``brain-install-launchd`` deletes it. A leftover is reported here, as one
    WARN whether it is loaded or not. The ``daemons`` status loop covers only the
    installed labels.

Both are WARN, never FAIL (ruling R8-5): a machine that never opted into the
daemons is not broken, and a red line there would cry wolf. Like
:mod:`brain.doctor_runtime`, this module never imports ``brain.cli`` at module
scope; ``_check`` defers that import.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .bin.launchd import _RETIRED_LABELS
from .doctor_runtime import _check
from .maintenance import snapshot_processes

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .cli import _DoctorCheck

#: The vault-sync watcher's LaunchAgent (one of ``brain.bin.launchd._INSTALLED_LABELS``).
WATCHER_LABEL = "com.brain.watcher"

_INVISIBLE_EDITS = (
    "external edits will not appear in search until `brain vault sync`"
)


def is_vault_watcher(cmd: str) -> bool:
    """True iff ``cmd`` runs ``brain vault sync ... --watch``.

    Tokenised like ``maintenance._is_brain_ingest``: the first token whose
    basename is ``brain`` (the console script, or ``-m brain``) must be followed
    by ``vault sync``, and ``--watch`` must follow that. A search query that
    merely contains the words never matches.
    """
    tokens = cmd.split()
    for i, token in enumerate(tokens):
        if token.rsplit("/", 1)[-1] == "brain":
            return tokens[i + 1 : i + 3] == ["vault", "sync"] and "--watch" in tokens[i + 3 :]
    return False


#: What ``brain ui`` may say about the watcher. Closed: the UI mirrors these
#: strings in ``static/js/watcher_status.js`` and a test holds the two equal.
WatcherState = Literal["running", "not_running", "unavailable"]
WATCHER_STATES: tuple[WatcherState, ...] = ("running", "not_running", "unavailable")


def watcher_state(processes: Sequence[str] | None = None) -> WatcherState:
    """Whether the vault-sync watcher is running, as far as a ``ps`` snapshot can tell.

    An EMPTY snapshot is ``"unavailable"``, not ``"not_running"``.
    ``snapshot_processes`` returns ``[]`` only when ``ps`` is missing, timed
    out or raised, because a snapshot that worked always contains at least the
    process asking. So ``[]`` means the check did not run, and calling that
    "not running" would report a fact nobody observed.
    """
    procs = processes if processes is not None else snapshot_processes()
    if not procs:
        return "unavailable"
    if any(is_vault_watcher(cmd) for cmd in procs):
        return "running"
    return "not_running"


def watcher_doctor_check(
    *, launchd_dir: Path, platform: str, processes: Sequence[str] | None = None
) -> list[_DoctorCheck]:
    """Report whether the vault-sync watcher is installed (macOS) or running (elsewhere).

    ``processes`` is the test seam, the same one ``maintenance.ingest_in_flight``
    has: ``None`` takes a ``ps`` snapshot. It is consulted only off macOS, where
    the answer is :func:`watcher_state`'s — so an unreadable snapshot is a
    distinct "unknown" WARN, matching ``brain ui``, not "not running".
    """
    if platform == "darwin":
        if (launchd_dir / f"{WATCHER_LABEL}.plist").is_file():
            return [
                _check(
                    "vault watcher",
                    "ok",
                    f"{WATCHER_LABEL} installed; its health is on the daemons line",
                )
            ]
        return [
            _check(
                "vault watcher",
                "warn",
                f"{WATCHER_LABEL} not installed: {_INVISIBLE_EDITS}",
                "brain-install-launchd",
            )
        ]

    # One rule with the UI's status line: `watcher_state` owns the scan, so an
    # unreadable process list is "unknown" here exactly as it is in `brain ui`.
    state = watcher_state(processes)
    if state == "running":
        return [_check("vault watcher", "ok", "`brain vault sync --watch` is running")]
    if state == "unavailable":
        return [
            _check(
                "vault watcher",
                "warn",
                "the process list could not be read (`ps` failed), so the watcher's "
                f"status is unknown; if it is stopped, {_INVISIBLE_EDITS}",
                "brain vault sync --watch",
            )
        ]
    return [
        _check(
            "vault watcher",
            "warn",
            f"`brain vault sync --watch` is not running: {_INVISIBLE_EDITS}",
            "brain vault sync --watch",
        )
    ]


def retired_daemon_doctor_check(*, launchd_dir: Path, platform: str) -> list[_DoctorCheck]:
    """WARN while a retired LaunchAgent's plist is still installed; silent otherwise."""
    if platform != "darwin":
        return []
    leftover = [
        label for label in _RETIRED_LABELS if (launchd_dir / f"{label}.plist").is_file()
    ]
    if not leftover:
        return []
    return [
        _check(
            "retired daemon",
            "warn",
            f"{', '.join(leftover)} is still installed, but the Quartz wiki has been "
            f"dormant since R2 and nothing should rebuild it",
            "brain-install-launchd",
        )
    ]
