"""Runtime-health `brain doctor` checks: dotenv, launchd daemons, the vault watcher.

Doctor's original checks all stop at the process boundary: they confirm that
*this* invocation can reach Postgres, Ollama and the vault. That left a whole
class of outage invisible — a missing ``$BRAIN_HOME/.env`` broke all three
launchd daemons for twelve days while `brain doctor`, run from a shell that
happened to have the environment loaded, reported everything green and exited
0. The (since-deleted) wiki served stale content the entire time and looked alive.

The checks here close that gap by asking about state OUTSIDE the current
process:

``config``
    Does ``$BRAIN_HOME/.env`` resolve and load? The daemons run with no
    ambient environment, so the file is the only thing they get.
``daemons``
    Are the LaunchAgents loaded, and did each one last exit 0?
``daemon code``
    Do the daemons import the same ``brain`` package this CLI does? If not,
    every other check describes the CLI's install rather than the daemons'.
``brief delivery``
    Composed in from :mod:`brain.doctor_delivery` (RM-16): the brief's delivery channel.
``reranker``
    Composed in from :mod:`brain.doctor_reranker` (RM-14b): is the local ``llama-server`` up?
``github``
    Composed in from :mod:`brain.doctor_github` (RM-31): is ``gh`` on ``PATH``?
``ocr``
    Composed in from :mod:`brain.doctor_ocr` (RM-32): can image-only PDFs be OCR'd?
``apple notes``
    Composed in from :mod:`brain.doctor_apple_notes` (RM-36): can ``osascript`` run here?
``vault watcher`` / ``retired daemon``
    Composed in from :mod:`brain.doctor_watcher` (wiki-to-ui phase 8): is the
    vault-sync watcher installed or running (A9), and is the retired
    ``com.brain.build`` still installed?

The ``wiki build`` check that followed was deleted with the Quartz wiki at R3
(phase 9).

Like :func:`brain.cli_backup.backup_doctor_checks` this module never imports
``brain.cli`` at module scope — ``cli.py`` imports *it*. The doctor value types
are resolved through a deferred import inside the function bodies.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

from .bin.launchd import _INSTALLED_LABELS, _OPTIONAL_LABELS, resolve_launchctl
from .config import _brain_home_dotenv, _brain_home_root, dotenv_chain

if TYPE_CHECKING:  # pragma: no cover — typing only; the real import is deferred
    from .cli import _DoctorCheck

#: The agents the ``daemons`` and ``daemon code`` rows inspect, each only when its plist
#: exists. The opt-in ones (RM-14b's ``com.brain.reranker``) are included because that
#: is-file filter already makes "never installed" mean "not checked".
DAEMON_LABELS = _INSTALLED_LABELS + _OPTIONAL_LABELS

#: Keys `$BRAIN_HOME/.env` must define for the launchd daemons to start.
#: ``DATABASE_URL`` is the one value :meth:`Config.load` hard-requires; a shell
#: that already exports it masks the omission, which is precisely how the
#: twelve-day outage stayed invisible to an interactive `brain doctor`.
REQUIRED_DOTENV_KEYS = ("DATABASE_URL",)

#: `launchctl list` is a local IPC round-trip; it should answer instantly.
_LAUNCHCTL_TIMEOUT_SECONDS = 5

#: Probing the daemons' interpreter costs one Python startup (~200ms). Bounded
#: generously so a cold filesystem cache degrades to a WARN, never a hang.
_INTERPRETER_PROBE_TIMEOUT_SECONDS = 20

def _default_launchd_dir() -> Path:
    """The LaunchAgents directory, honouring the ``BRAIN_LAUNCHD_DIR`` seam.

    Same resolution as :func:`brain.bin.launchd.install_main`, so doctor
    inspects exactly the directory the installer wrote to.
    """
    return Path(
        os.environ.get("BRAIN_LAUNCHD_DIR") or Path.home() / "Library" / "LaunchAgents"
    )


def runtime_doctor_checks(
    *,
    launchd_dir: Path | None = None,
    launchctl: str | None = None,
    platform: str | None = None,
    processes: Sequence[str] | None = None,
) -> list[_DoctorCheck]:
    """Build the ``config`` / ``daemons`` / ``vault watcher`` / ``daemon code`` checks.

    Takes no ``Config``: the one check that read it (the dormant wiki's build
    freshness, from ``cfg.vault_path``) was deleted with the wiki at R3, and
    with it the ``now`` seam it needed. Every keyword argument is a test seam
    with a production default resolved lazily, so tests never shell out to the
    real ``launchctl`` or ``ps``, nor read the real ``~/.brain``. ``processes``
    feeds the off-macOS watcher scan, the same seam
    ``maintenance.ingest_in_flight`` has.
    """
    # Deferred: doctor_watcher and doctor_delivery import this module's _check.
    from .doctor_apple_notes import apple_notes_doctor_check
    from .doctor_delivery import brief_delivery_doctor_check
    from .doctor_github import github_doctor_check
    from .doctor_ocr import ocr_doctor_check
    from .doctor_reranker import reranker_doctor_check
    from .doctor_watcher import retired_daemon_doctor_check, watcher_doctor_check

    resolved_dir = launchd_dir if launchd_dir is not None else _default_launchd_dir()
    resolved_platform = platform if platform is not None else sys.platform

    return [
        *dotenv_doctor_check(),
        *daemon_doctor_check(
            launchd_dir=resolved_dir,
            launchctl=launchctl if launchctl is not None else resolve_launchctl(),
            platform=resolved_platform,
        ),
        *watcher_doctor_check(
            launchd_dir=resolved_dir, platform=resolved_platform, processes=processes
        ),
        *retired_daemon_doctor_check(launchd_dir=resolved_dir, platform=resolved_platform),
        *daemon_code_doctor_check(
            launchd_dir=resolved_dir, platform=resolved_platform
        ),
        *brief_delivery_doctor_check(),
        *reranker_doctor_check(),
        *github_doctor_check(),
        *ocr_doctor_check(),
        *apple_notes_doctor_check(platform=resolved_platform),
    ]


# ---------------------------------------------------------------------------
# config — does $BRAIN_HOME/.env resolve AND load?
# ---------------------------------------------------------------------------


def _check(
    name: str,
    status: str,
    detail: str,
    remedy: str | None = None,
    notes: tuple[str, ...] = (),
) -> _DoctorCheck:
    """One doctor check rendered in the house ``{:<15}`` format.

    *notes* become indented ``—`` continuation lines below the headline, the
    same shape the ``vault drift`` and ``communities`` checks already use.
    """
    from .cli import _DoctorCheck, _DoctorLine

    if status == "ok":
        text = f"{name:<15} OK ({detail})"
        fg: str | None = None
    else:
        word = "FAIL" if status == "fail" else "WARN"
        suffix = f". Run: {remedy}" if remedy else ""
        text = f"{name:<15} {word} — {detail}{suffix}"
        fg = "red" if status == "fail" else "yellow"
    err = status == "fail"
    lines = [_DoctorLine(text=text, fg=fg, err=err)]
    lines.extend(
        _DoctorLine(text=f"{'':<15} — {note}", fg=fg, err=err) for note in notes
    )
    return _DoctorCheck(
        check=name,
        status=status,
        detail=detail,
        remedy=remedy,
        lines=tuple(lines),
    )


def dotenv_doctor_check() -> list[_DoctorCheck]:
    """Assert ``$BRAIN_HOME/.env`` resolves and loads with the required keys.

    Four distinct faults, four distinct remedies — collapsing them is what made
    the original outage so hard to diagnose:

    * **dangling symlink** — `brain setup` can point ``$BRAIN_HOME/.env`` at a
      dev checkout; move the checkout and the link breaks. Reported separately
      from "missing" because the fix is to repoint the link, and because
      :attr:`DotenvSource.exists` follows symlinks and so reports a dangling
      link as ``False`` — indistinguishable from absent without the extra
      :meth:`~pathlib.Path.is_symlink` probe.
    * **missing** — never provisioned.
    * **present but unparseable** — a directory, or bad permissions.
    * **present but incomplete** — loads, but lacks a key the daemons need.

    All four are FAIL, never WARN: each one means the launchd daemons cannot
    start, even though the interactive CLI may work perfectly from a shell that
    already exports the values.
    """
    target = _brain_home_dotenv()
    source = next((s for s in dotenv_chain() if s.path == target), None)

    if source is None:  # pragma: no cover — defensive; the chain always has it
        return [
            _check(
                "config",
                "warn",
                f"could not resolve $BRAIN_HOME/.env ({target})",
                "brain setup",
            )
        ]

    if not source.exists:
        if source.path.is_symlink():
            dest = os.readlink(source.path)
            # `brain setup` deliberately REPORTS a dangling link and refuses to
            # replace it (provision_brain_home_dotenv → PROVISION_DANGLING), so
            # naming it alone would be a no-op remedy that loops the user. The
            # link has to be removed first; verified end-to-end.
            return [
                _check(
                    "config",
                    "fail",
                    f"{source.path} is a DANGLING SYMLINK to {dest} — the "
                    f"target no longer exists, so every launchd daemon starts "
                    f"with no config",
                    f"rm {source.path} && brain setup"
                    f"  # setup will NOT replace the broken link on its own",
                )
            ]
        return [
            _check(
                "config",
                "fail",
                f"{source.path} is MISSING — the launchd daemons get no "
                f"environment and will fail silently",
                "brain setup",
            )
        ]

    if not source.loaded:
        # `brain setup` treats any existing path as PRESENT and leaves it
        # alone, so it cannot repair this either — the bad path must go first.
        return [
            _check(
                "config",
                "fail",
                f"{source.path} exists but could NOT be read (a directory, or "
                f"bad permissions?)",
                f"ls -ld {source.path}  # expect a readable file; then "
                f"chmod 600 it, or remove it and run brain setup",
            )
        ]

    try:
        keys = {k for k, v in dotenv_values(source.path).items() if v}
    except OSError:  # pragma: no cover — raced with the chain's own read
        keys = set()
    missing = [k for k in REQUIRED_DOTENV_KEYS if k not in keys]
    if missing:
        # Not `brain setup`: it short-circuits on an existing file
        # (PROVISION_PRESENT) and never edits its contents, so it would leave
        # the key just as absent. The file itself has to be edited.
        return [
            _check(
                "config",
                "fail",
                f"{source.path} is missing required key(s): "
                f"{', '.join(missing)} — your shell may export them, but the "
                f"launchd daemons will not see them",
                f"$EDITOR {source.path}  # add {', '.join(missing)} "
                f"(see .env.example)",
            )
        ]

    return [_check("config", "ok", f"{source.path} loaded")]


# ---------------------------------------------------------------------------
# daemons — are the LaunchAgents loaded, and did each last exit 0?
# ---------------------------------------------------------------------------


def _parse_launchctl_list(stdout: str) -> dict[str, tuple[int | None, int | None]]:
    """Map label → ``(pid, last_exit_status)`` from ``launchctl list`` output.

    The output is a tab-separated ``PID\\tStatus\\tLabel`` table with a header
    row. ``-`` in either numeric column (and any value that will not parse)
    becomes ``None`` rather than being dropped, so an unreadable field is never
    silently reported as healthy.

    The PID is load-bearing, not decoration: ``pid is None`` means the job is
    NOT RUNNING right now, which is what separates "a KeepAlive daemon was
    restarted after a signal" from "a KeepAlive daemon is dead". An earlier
    version of this parser discarded the PID and could not tell those apart.
    """
    parsed: dict[str, tuple[int | None, int | None]] = {}
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        raw_pid, raw_status, label = parts
        if label == "Label":  # header row
            continue

        def _int_or_none(raw: str) -> int | None:
            try:
                return int(raw)
            except ValueError:
                return None

        parsed[label] = (_int_or_none(raw_pid), _int_or_none(raw_status))
    return parsed


def _is_keepalive(launchd_dir: Path, label: str) -> bool:
    """True iff *label*'s plist declares ``KeepAlive`` (a resident daemon).

    Read from the plist rather than hardcoded, so adding a fourth agent cannot
    silently get the wrong severity. ``com.brain.{watcher,build}`` are
    KeepAlive; ``com.brain.brief`` is a one-shot StartCalendarInterval job for
    which "not running" is the normal state.
    """
    try:
        with (launchd_dir / f"{label}.plist").open("rb") as fh:
            return bool(plistlib.load(fh).get("KeepAlive"))
    except (OSError, plistlib.InvalidFileException, AttributeError, ValueError):
        return False


def _daemon_error_log(launchd_dir: Path, label: str) -> Path:
    """The stderr log launchd ACTUALLY writes for *label*.

    Read out of the installed plist's ``StandardErrorPath`` rather than
    recomputed from ``$BRAIN_HOME``, because the two can disagree: the plists
    are rendered once at install time with whatever ``$BRAIN_HOME`` was then,
    while a `brain doctor` run from a dev checkout resolves ``$BRAIN_HOME`` to
    the repo root. Recomputing sends the user to a path that does not exist —
    a remedy that silently wastes their time, which is the very failure mode
    this whole check exists to eliminate.

    Falls back to the conventional location when the plist is unreadable or
    omits the key.
    """
    try:
        with (launchd_dir / f"{label}.plist").open("rb") as fh:
            configured = plistlib.load(fh).get("StandardErrorPath")
    except (OSError, plistlib.InvalidFileException, AttributeError, ValueError):
        configured = None
    if isinstance(configured, str) and configured:
        return Path(configured)
    return _brain_home_root() / "logs" / f"{label}.err.log"


def daemon_doctor_check(
    *, launchd_dir: Path, launchctl: str, platform: str
) -> list[_DoctorCheck]:
    """Report whether each installed brain LaunchAgent is loaded and exited 0.

    Severity, deliberately:

    * **positive last exit** → FAIL. A job that exits non-zero is broken, and
      for the two ``KeepAlive`` daemons it means a crash loop. This is the exact
      signature of the twelve-day outage, so it must move the exit code. Every
      positive code is treated alike. (Exit 3 used to get a "MISCONFIGURED"
      wording of its own; that was the deleted ``brain.wiki.build_swap``'s
      contract, and no surviving daemon emits it, so it went with the wiki at
      R3.)
    * **installed but not loaded** → FAIL. The plist is on disk but launchd
      does not have it; nothing is running.
    * **negative last exit** (killed by a signal) → it depends, and the earlier
      blanket WARN here was wrong. ``launchctl`` reports only the signal
      NUMBER, and SIGTERM is what ``launchctl bootout``, a system shutdown, AND
      a Python subprocess-timeout kill all send — the number carries no
      information about cause, so classifying by signal is not possible from
      this data. ``launchctl list`` also exposes no history, so "three times
      today" is not observable either. What IS observable is whether the job is
      alive now, so:

      - **not running** and ``KeepAlive`` → FAIL. A resident daemon that is
        down is down, whatever killed it. (``com.brain.brief`` is a one-shot
        calendar job — not running is its normal state, so it is exempt.)
      - **running** → WARN. The restart recovered; the message still names the
        likely cause and the log. (Until phase 8 a stale wiki turned this into
        a FAIL; that coupling went at R2, and the wiki itself at R3.)
    * **no plists installed** → OK, "not installed". A fresh install that never
      opted into the daemons is not broken.
    * **non-macOS** → OK, not applicable. launchd is macOS-only; a Linux user
      must not see a red line for a subsystem that cannot exist.

    Only the INSTALLED labels are covered (``DAEMON_LABELS``); a leftover retired
    plist is :func:`brain.doctor_watcher.retired_daemon_doctor_check`'s to report.
    """
    if platform != "darwin":
        return [_check("daemons", "ok", "n/a — launchd is macOS-only")]

    installed = [
        label for label in DAEMON_LABELS if (launchd_dir / f"{label}.plist").is_file()
    ]
    if not installed:
        return [
            _check(
                "daemons",
                "ok",
                f"not installed — no brain LaunchAgents in {launchd_dir}",
            )
        ]

    try:
        result = subprocess.run(
            [launchctl, "list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LAUNCHCTL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [
            _check(
                "daemons",
                "warn",
                f"could not run `{launchctl} list`: {exc}",
                "xcode-select --install",
            )
        ]
    if result.returncode != 0:
        return [
            _check(
                "daemons",
                "warn",
                f"`{launchctl} list` exited {result.returncode}",
            )
        ]

    statuses = _parse_launchctl_list(result.stdout)
    failures: list[tuple[str, str]] = []
    warnings: list[tuple[str, str]] = []
    for label in installed:
        if label not in statuses:
            failures.append((label, "installed but NOT LOADED"))
            continue
        pid, status = statuses[label]
        if status is None:
            warnings.append((label, "status unreadable"))
        elif status < 0:
            signal_num = abs(status)
            if pid is None and _is_keepalive(launchd_dir, label):
                failures.append(
                    (
                        label,
                        f"killed by signal {signal_num} and NOT RUNNING "
                        f"(KeepAlive daemon is down)",
                    )
                )
            else:
                warnings.append(
                    (
                        label,
                        f"killed by signal {signal_num} then restarted (a repeat "
                        f"here means a self-inflicted kill — check the log)",
                    )
                )
        elif status > 0:
            failures.append((label, f"last exit {status}"))

    if failures:
        detail = "; ".join(f"{label} {why}" for label, why in failures)
        if warnings:
            detail += f" ({'; '.join(f'{lbl} {why}' for lbl, why in warnings)})"
        notes = tuple(
            f"{label}: {_daemon_error_log(launchd_dir, label)}"
            for label, _why in failures
        )
        remedy = "brain-install-launchd  # reload once the log above is addressed"
        return [_check("daemons", "fail", detail, remedy, notes=notes)]
    if warnings:
        return [
            _check(
                "daemons",
                "warn",
                "; ".join(f"{label} {why}" for label, why in warnings),
            )
        ]
    return [
        _check("daemons", "ok", f"{len(installed)} agent(s) loaded, last exit 0")
    ]


# ---------------------------------------------------------------------------
# daemon code — do the daemons import the same `brain` package as this CLI?
# ---------------------------------------------------------------------------


def _plist_brain_py(launchd_dir: Path, label: str) -> str | None:
    """The interpreter a plist pins via ``EnvironmentVariables.BRAIN_PY``."""
    try:
        with (launchd_dir / f"{label}.plist").open("rb") as fh:
            env = plistlib.load(fh).get("EnvironmentVariables") or {}
    except (OSError, plistlib.InvalidFileException, AttributeError, ValueError):
        return None
    value = env.get("BRAIN_PY")
    return value if isinstance(value, str) and value else None


def _package_dir_for(interpreter: str) -> str | None:
    """Where *interpreter* imports ``brain`` from, or ``None`` if it cannot."""
    try:
        result = subprocess.run(
            [interpreter, "-c", "import brain,os;print(os.path.dirname(brain.__file__))"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_INTERPRETER_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = result.stdout.strip()
    return out if result.returncode == 0 and out else None


def daemon_code_doctor_check(
    *, launchd_dir: Path, platform: str
) -> list[_DoctorCheck]:
    """WARN when the daemons import a DIFFERENT ``brain`` package than this CLI.

    The plists pin ``BRAIN_PY`` at install time. If the user later runs `brain`
    from somewhere else — a dev checkout, a second install — the CLI and the
    daemons execute different code, and every other check in this file
    describes the CLI's world rather than the daemons'. On this machine the
    daemons run a uv-tool install that is 76 files behind the checkout, so a
    green `brain doctor` was reporting on code the daemons never load. That is
    the same silent-drift class as the outage itself.

    WARN, never FAIL, and deliberately: running a dev-checkout CLI against
    pipx-installed daemons is a legitimate everyday workflow. Failing it would
    leave every developer's doctor permanently red, which teaches people to
    ignore doctor — reproducing the outage by a different route. Silence is
    wrong; red is also wrong; a yellow line the user can act on is right.

    Emits nothing at all when there is nothing to compare (non-macOS, no
    plists, no ``BRAIN_PY``) — the ``daemons`` check already covers those.
    """
    if platform != "darwin":
        return []
    installed = [
        label for label in DAEMON_LABELS if (launchd_dir / f"{label}.plist").is_file()
    ]
    if not installed:
        return []

    pinned = next(
        (py for py in (_plist_brain_py(launchd_dir, lbl) for lbl in installed) if py),
        None,
    )
    if pinned is None:
        return []

    ours = str(Path(__file__).resolve().parent)
    if Path(pinned).resolve() == Path(sys.executable).resolve():
        return [_check("daemon code", "ok", f"same interpreter as this CLI ({ours})")]

    theirs = _package_dir_for(pinned)
    if theirs is None:
        return [
            _check(
                "daemon code",
                "warn",
                f"the daemons' interpreter ({pinned}) cannot import `brain` — "
                f"they will fail on every run",
                "brain-install-launchd  # re-pin BRAIN_PY at a working install",
            )
        ]
    if Path(theirs).resolve() == Path(ours).resolve():
        return [_check("daemon code", "ok", f"same `brain` package as this CLI ({ours})")]

    return [
        _check(
            "daemon code",
            "warn",
            f"the daemons run a DIFFERENT `brain` than this CLI — daemons "
            f"import {theirs}, this CLI imports {ours}; checks above describe "
            f"THIS CLI's install, not the daemons'",
            "brain-install-launchd  # re-pin the daemons at this install, "
            "or run the CLI from the daemons' install",
        )
    ]
