"""Phase 8 T4: `brain doctor` reports the vault-sync watcher, a retired daemon, a dormant wiki.

- **A9's doctor half (Q8, R8-5).** No watcher → WARN, never FAIL. A machine that
  never opted into daemons is not broken. An installed watcher that is down
  stays a FAIL on the ``daemons`` line, so it is not reported twice. Off macOS
  there is no launchd, so a process scan, through a ``processes`` seam, looks
  for ``brain vault sync --watch``.
- **The retired ``com.brain.build`` (E8).** A leftover plist is one WARN from
  its own check, whether it is loaded or not, and never a FAIL or an OK from
  the ``daemons`` status loop.
- **Dormant wiki (R8-4).** At R2 nothing rebuilds the wiki by default, so its
  age is no longer a fault. Both former FAIL branches are OK, and both name
  ``brain-rebuild --only quartz``.
"""

from __future__ import annotations

import plistlib
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from brain import doctor_watcher, maintenance
from brain.bin.launchd import _INSTALLED_LABELS, _RETIRED_LABELS
from brain.config import Config
from brain.doctor_runtime import runtime_doctor_checks, wiki_freshness_doctor_check
from brain.doctor_watcher import (
    WATCHER_LABEL,
    WATCHER_STATES,
    is_vault_watcher,
    retired_daemon_doctor_check,
    watcher_doctor_check,
    watcher_state,
)
from tests.test_doctor_runtime import _write_build

_BUILD = "com.brain.build"
_WATCH_CMD = "/usr/bin/python3 /x/bin/brain vault sync --watch --vault /v"

#: Command lines that are NOT the watcher. One list, used by both the token
#: test (``is_vault_watcher``) and the state test (``watcher_state``), so the
#: two cannot drift apart the way a hand-copied subset did.
_NEAR_MISSES = (
    "/x/bin/brain vault sync --vault /v",
    "/x/bin/brain search 'vault sync --watch'",
    "/usr/bin/python3 -m brain.wiki.build_watcher --vault /v --keep 3",
    "/x/bin/brain ingest-stdin --source krisp",
    # Near-misses that pin the token ORDER (phase-8 audit): every word is
    # present, unquoted, but not as `brain vault sync ... --watch`.
    "/x/bin/brain search vault sync --watch",
    "/x/bin/brain vault export --watch",
    # The UI itself, which is always in the snapshot it reads.
    "/x/bin/brain ui",
)


def _plist(launchd_dir: Path, label: str) -> None:
    launchd_dir.mkdir(parents=True, exist_ok=True)
    payload = {"Label": label, "KeepAlive": True}
    (launchd_dir / f"{label}.plist").write_bytes(plistlib.dumps(payload))


class _Listed:
    """``launchctl list`` output for the labels given, all last-exit 0."""

    def __init__(self, *labels: str) -> None:
        rows = ["PID\tStatus\tLabel", *(f"4242\t0\t{label}" for label in labels)]
        self.stdout = "\n".join(rows) + "\n"
        self.returncode = 0


def _checks(
    tmp_path: Path, *, listed: tuple[str, ...], platform: str = "darwin", **kw: Any
) -> dict[str, list[Any]]:
    """``runtime_doctor_checks`` with ``launchctl list`` answering ``listed``."""
    cfg = Config(database_url="postgresql://synthetic/x", vault_path=tmp_path / "vault")
    with patch("brain.doctor_runtime.subprocess.run", return_value=_Listed(*listed)):
        checks = runtime_doctor_checks(
            cfg,
            launchd_dir=tmp_path / "agents",
            launchctl="launchctl",
            platform=platform,
            **kw,
        )
    grouped: dict[str, list[Any]] = {}
    for check in checks:
        grouped.setdefault(check.check, []).append(check)
    return grouped


# ---------------------------------------------------------------------------
# the vault-sync watcher (A9 / Q8)
# ---------------------------------------------------------------------------


def test_the_watcher_label_is_one_brain_installs() -> None:
    assert WATCHER_LABEL in _INSTALLED_LABELS


def test_macos_without_a_watcher_plist_warns_with_the_install_remedy(tmp_path: Path) -> None:
    (check,) = watcher_doctor_check(launchd_dir=tmp_path, platform="darwin")

    assert check.status == "warn"
    assert "external edits will not appear in search" in check.detail
    assert check.remedy == "brain-install-launchd"


def test_macos_with_the_watcher_installed_is_ok(tmp_path: Path) -> None:
    _plist(tmp_path, WATCHER_LABEL)

    (check,) = watcher_doctor_check(launchd_dir=tmp_path, platform="darwin")

    assert check.status == "ok"


def test_an_installed_watcher_that_is_down_fails_once_on_the_daemons_line(
    tmp_path: Path,
) -> None:
    """Re-pinned (R8-5): installed but not loaded is a FAIL, reported by ``daemons`` only."""
    _plist(tmp_path / "agents", WATCHER_LABEL)

    checks = _checks(tmp_path, listed=())

    (daemons,) = checks["daemons"]
    assert daemons.status == "fail"
    assert f"{WATCHER_LABEL} installed but NOT LOADED" in daemons.detail
    (watcher,) = checks["vault watcher"]
    assert watcher.status == "ok"


@pytest.mark.parametrize(
    "cmd",
    [
        _WATCH_CMD,
        "/usr/bin/python3 -m brain vault sync --vault /v --watch",
        "brain vault sync --watch",
    ],
)
def test_the_process_scan_recognises_a_running_watcher(cmd: str) -> None:
    assert is_vault_watcher(cmd)


@pytest.mark.parametrize("cmd", _NEAR_MISSES)
def test_the_process_scan_ignores_everything_else(cmd: str) -> None:
    assert not is_vault_watcher(cmd)


def test_off_macos_a_running_watcher_is_ok(tmp_path: Path) -> None:
    (check,) = watcher_doctor_check(
        launchd_dir=tmp_path, platform="linux", processes=["/bin/zsh", _WATCH_CMD]
    )

    assert check.status == "ok"


def test_off_macos_no_watcher_warns_with_the_run_remedy(tmp_path: Path) -> None:
    (check,) = watcher_doctor_check(
        launchd_dir=tmp_path, platform="linux", processes=["/bin/zsh", "/x/bin/brain ui"]
    )

    assert check.status == "warn"
    assert "external edits will not appear in search" in check.detail
    assert check.remedy == "brain vault sync --watch"


def test_off_macos_an_unreadable_process_list_warns_unknown_not_stopped(
    tmp_path: Path,
) -> None:
    """Doctor and the UI agree when ``ps`` fails: the status is UNKNOWN.

    ``snapshot_processes`` returns ``[]`` only when ``ps`` could not be read.
    Doctor used to re-derive the scan and report that as "is not running",
    while the UI, through ``watcher_state``, said "unknown". Both now read the
    one rule.
    """
    (check,) = watcher_doctor_check(launchd_dir=tmp_path, platform="linux", processes=[])

    assert check.status == "warn"
    assert "could not be read" in check.detail
    assert "unknown" in check.detail
    assert "is not running" not in check.detail
    # Check first, THEN start: the watcher may well be running, and nothing
    # stops a second `brain vault sync --watch` from starting beside it.
    assert check.remedy == (
        "check with `ps`; if no `brain vault sync --watch` is running, "
        "start one with `brain vault sync --watch`"
    )


def test_off_macos_a_failing_ps_snapshot_warns_unknown_on_the_production_path(
    tmp_path: Path,
) -> None:
    """The real call passes no ``processes``: a failed ``ps`` must still read "unknown"."""
    with patch.object(doctor_watcher, "snapshot_processes", lambda: []):
        (check,) = watcher_doctor_check(launchd_dir=tmp_path, platform="linux")

    assert "could not be read" in check.detail


def test_runtime_doctor_checks_threads_the_processes_seam(tmp_path: Path) -> None:
    def _no_scan() -> list[str]:
        raise AssertionError("an explicit processes list must not be re-scanned")

    with patch.object(doctor_watcher, "snapshot_processes", _no_scan):
        running = _checks(tmp_path, listed=(), platform="linux", processes=[_WATCH_CMD])
        # Non-empty: an EMPTY snapshot means `ps` failed, which is "unknown".
        stopped = _checks(tmp_path, listed=(), platform="linux", processes=["/bin/zsh"])

    assert running["vault watcher"][0].status == "ok"
    assert stopped["vault watcher"][0].status == "warn"


def test_without_a_processes_list_the_scan_takes_a_snapshot(tmp_path: Path) -> None:
    with patch.object(doctor_watcher, "snapshot_processes", lambda: [_WATCH_CMD]):
        (check,) = watcher_doctor_check(launchd_dir=tmp_path, platform="linux")

    assert check.status == "ok"


def test_snapshot_processes_is_the_one_ps_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shared with ``ingest_in_flight`` rather than copied (plan T4)."""
    assert doctor_watcher.snapshot_processes is maintenance.snapshot_processes
    monkeypatch.setattr(
        maintenance, "snapshot_processes", lambda: ["/x/bin/brain ingest /some/file.md"]
    )

    assert maintenance.ingest_in_flight() is True


# ---------------------------------------------------------------------------
# the retired com.brain.build (E8)
# ---------------------------------------------------------------------------


def test_build_is_the_retired_label() -> None:
    assert _RETIRED_LABELS == (_BUILD,)


@pytest.mark.parametrize("loaded", [True, False], ids=["loaded", "not-loaded"])
def test_a_leftover_build_plist_is_one_warning_from_its_own_check(
    tmp_path: Path, loaded: bool
) -> None:
    agents = tmp_path / "agents"
    for label in (*_INSTALLED_LABELS, _BUILD):
        _plist(agents, label)
    listed = (*_INSTALLED_LABELS, _BUILD) if loaded else _INSTALLED_LABELS

    checks = _checks(tmp_path, listed=listed)

    assert len(checks.get("retired daemon", [])) == 1, sorted(checks)
    (retired,) = checks["retired daemon"]
    assert retired.status == "warn"
    assert _BUILD in retired.detail
    assert retired.remedy == "brain-install-launchd"
    about_build = [
        c for group in checks.values() for c in group if _BUILD in c.detail
    ]
    assert about_build == [retired], "only the retired-daemon check may name it"


def test_no_leftover_means_no_retired_daemon_line(tmp_path: Path) -> None:
    _plist(tmp_path, WATCHER_LABEL)

    assert retired_daemon_doctor_check(launchd_dir=tmp_path, platform="darwin") == []


def test_the_retired_check_is_macos_only(tmp_path: Path) -> None:
    _plist(tmp_path, _BUILD)

    assert retired_daemon_doctor_check(launchd_dir=tmp_path, platform="linux") == []


# ---------------------------------------------------------------------------
# dormant wiki freshness (R8-4)
# ---------------------------------------------------------------------------


def test_an_old_wiki_build_is_dormant_not_failing(tmp_path: Path) -> None:
    """The 12-day age that used to FAIL is the expected state at R2."""
    _write_build(tmp_path / ".quartz" / "builds", "20260726-090000-aaaaaa", age_days=12,
                 complete=True)

    (check,) = wiki_freshness_doctor_check(tmp_path, now=time.time())

    assert check.status == "ok"
    assert "dormant" in check.detail
    assert "brain-rebuild --only quartz" in check.detail
    assert check.remedy == "brain-rebuild --only quartz"


def test_a_workspace_that_never_built_is_dormant_not_failing(tmp_path: Path) -> None:
    """The former "NO completed build" FAIL, and its bare ``brain-rebuild`` remedy."""
    _write_build(tmp_path / ".quartz" / "builds", "20260807-090000-ffffff", age_days=0,
                 complete=False)

    (check,) = wiki_freshness_doctor_check(tmp_path, now=time.time())

    assert check.status == "ok"
    assert "dormant" in check.detail and "never built" in check.detail
    assert "brain-rebuild --only quartz" in check.detail
    assert check.remedy == "brain-rebuild --only quartz"


def test_a_dormant_wiki_never_turns_the_daemons_line_red(tmp_path: Path) -> None:
    """The ``wiki_stale`` coupling is gone: a signal-restarted daemon over an old wiki WARNs."""
    vault = tmp_path / "vault"
    _write_build(vault / ".quartz" / "builds", "20260726-090000-aaaaaa", age_days=30,
                 complete=True)
    _plist(tmp_path / "agents", WATCHER_LABEL)
    killed = _Listed(WATCHER_LABEL)
    killed.stdout = f"PID\tStatus\tLabel\n4242\t-15\t{WATCHER_LABEL}\n"
    cfg = Config(database_url="postgresql://synthetic/x", vault_path=vault)

    with patch("brain.doctor_runtime.subprocess.run", return_value=killed):
        checks = runtime_doctor_checks(
            cfg, launchd_dir=tmp_path / "agents", launchctl="launchctl", platform="darwin"
        )

    by_name = {c.check: c for c in checks}
    assert by_name["daemons"].status == "warn", by_name["daemons"].detail
    assert by_name["wiki build"].status == "ok"
    assert "wiki" not in by_name["daemons"].detail.lower()


# --------------------------------------------------------------------------
# watcher_state — the UI status line's question (A9's UI half)
# --------------------------------------------------------------------------


def test_watcher_state_is_running_when_a_watcher_is_in_the_snapshot() -> None:
    assert watcher_state(["/bin/zsh", _WATCH_CMD]) == "running"


@pytest.mark.parametrize("cmd", _NEAR_MISSES)
def test_watcher_state_is_not_running_for_a_snapshot_without_one(cmd: str) -> None:
    assert watcher_state(["/bin/zsh", cmd]) == "not_running"


def test_an_empty_snapshot_is_unavailable_not_not_running() -> None:
    """``snapshot_processes`` returns ``[]`` only when ``ps`` failed.

    A real snapshot always holds at least the asking process, so ``[]`` means
    the check could not run — and reporting that as "not running" would claim
    a fact nobody observed.
    """
    assert watcher_state([]) == "unavailable"


def test_watcher_states_is_the_closed_vocabulary() -> None:
    assert WATCHER_STATES == ("running", "not_running", "unavailable")
