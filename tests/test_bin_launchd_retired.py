"""Phase 8 T2: ``com.brain.build`` is retired at R2.

It is never installed again, an existing install is booted out and its plist
deleted on the next ``brain-install-launchd``, and ``brain-uninstall-launchd``
still sweeps it. Its template and ``_brain-build-fg`` wrapper were deleted with
the wiki at R3 (phase 9); the sweep stays for upgraded machines (R9-1).

Every launchctl here is a recording stub passed explicitly. The conftest stub
(``tests/test_launchctl_isolation.py``) is the backstop, not the double.
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path
from unittest.mock import patch

from brain.bin.launchd import (
    _ALL_LABELS,
    _INSTALLED_LABELS,
    _RETIRED_LABELS,
    install_plists,
    uninstall_plists,
)
from brain.doctor_runtime import daemon_code_doctor_check, daemon_doctor_check

_WATCHER = "com.brain.watcher"
_BUILD = "com.brain.build"
_BRIEF = "com.brain.brief"


def _recorder(tmp_path: Path, *, print_ok: bool = False, bootout_rc: int = 0) -> tuple[Path, Path]:
    """A launchctl stub that records argv; ``print`` and ``bootout`` exit as told."""
    log = tmp_path / "launchctl.calls"
    stub = tmp_path / "launchctl"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'case "$1" in\n'
        f"  print) exit {0 if print_ok else 1} ;;\n"
        f"  bootout) exit {bootout_rc} ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, log


def _calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _plists(launchd_dir: Path) -> list[str]:
    return sorted(p.name for p in launchd_dir.glob("*.plist"))


def _install(tmp_path: Path, stub: Path) -> Path:
    launchd_dir = tmp_path / "agents"
    install_plists(
        brain_home=tmp_path / "brain", launchd_dir=launchd_dir, launchctl=str(stub)
    )
    return launchd_dir


def test_the_build_daemon_is_retired_not_installed() -> None:
    assert _INSTALLED_LABELS == (_WATCHER, _BRIEF)
    assert _RETIRED_LABELS == (_BUILD,)
    # The opt-in labels (RM-14b's reranker, RM-33's drop folders) sit between the
    # installed and the retired labels.
    assert _ALL_LABELS == (
        _WATCHER, _BRIEF, "com.brain.reranker", "com.brain.dropfolders", _BUILD
    )


def test_install_writes_and_bootstraps_only_the_watcher_and_brief(tmp_path: Path) -> None:
    """E5: exactly two plists, exactly two bootstraps, none of them the build daemon."""
    stub, log = _recorder(tmp_path)

    launchd_dir = _install(tmp_path, stub)

    assert _plists(launchd_dir) == [f"{_BRIEF}.plist", f"{_WATCHER}.plist"]
    bootstraps = [c for c in _calls(log) if c.startswith("bootstrap ")]
    assert len(bootstraps) == 2, bootstraps
    assert any(f"{_WATCHER}.plist" in c for c in bootstraps)
    assert any(f"{_BRIEF}.plist" in c for c in bootstraps)
    assert not [c for c in bootstraps if _BUILD in c]


def test_install_shims_only_the_installed_wrappers(tmp_path: Path) -> None:
    """E5: nothing installed runs ``_brain-build-fg`` (deleted at R3), so it is not shimmed."""
    stub, _ = _recorder(tmp_path)

    _install(tmp_path, stub)

    shims = tmp_path / "brain" / ".shims"
    assert (shims / "_brain-watcher-fg").is_file()
    assert (shims / "_brain-brief-fg").is_file()
    assert not (shims / "_brain-build-fg").exists()


def test_install_boots_out_and_deletes_a_leftover_build_daemon(tmp_path: Path) -> None:
    """E5: the R2 migration. A pre-phase-8 install loses its build daemon."""
    stub, log = _recorder(tmp_path)
    launchd_dir = tmp_path / "agents"
    launchd_dir.mkdir()
    (launchd_dir / f"{_BUILD}.plist").write_text("<plist/>", encoding="utf-8")

    _install(tmp_path, stub)

    assert not (launchd_dir / f"{_BUILD}.plist").exists()
    assert f"bootout gui/{os.getuid()}/{_BUILD}" in _calls(log)


def test_a_failing_retired_bootout_does_not_fail_the_install(tmp_path: Path) -> None:
    """"Not loaded" is the normal answer for a retired job; it must not abort."""
    stub, _ = _recorder(tmp_path, bootout_rc=113)
    launchd_dir = tmp_path / "agents"
    launchd_dir.mkdir()
    (launchd_dir / f"{_BUILD}.plist").write_text("<plist/>", encoding="utf-8")

    _install(tmp_path, stub)

    assert _plists(launchd_dir) == [f"{_BRIEF}.plist", f"{_WATCHER}.plist"]


def test_a_second_install_is_idempotent(tmp_path: Path) -> None:
    stub, log = _recorder(tmp_path)

    launchd_dir = _install(tmp_path, stub)
    first = {name: (launchd_dir / name).read_bytes() for name in _plists(launchd_dir)}
    _install(tmp_path, stub)

    assert {name: (launchd_dir / name).read_bytes() for name in _plists(launchd_dir)} == first
    assert len([c for c in _calls(log) if c.startswith("bootstrap ")]) == 4


def test_uninstall_still_sweeps_the_retired_build_daemon(tmp_path: Path) -> None:
    """E6: an install from before phase 8 is fully removable."""
    stub, log = _recorder(tmp_path, print_ok=True)
    launchd_dir = tmp_path / "agents"
    launchd_dir.mkdir()
    for label in (_WATCHER, _BUILD, _BRIEF):
        (launchd_dir / f"{label}.plist").write_text("<plist/>", encoding="utf-8")

    uninstall_plists(launchd_dir=launchd_dir, launchctl=str(stub))

    assert _plists(launchd_dir) == []
    for label in (_WATCHER, _BUILD, _BRIEF):
        assert f"bootout gui/{os.getuid()}/{label}" in _calls(log), label


# ---------------------------------------------------------------------------
# The doctor reads the INSTALLED labels only. A leftover build plist is
# reported by the dedicated retired-label check (T4), never by these two.
# ---------------------------------------------------------------------------


def _write_plist(launchd_dir: Path, label: str, *, brain_py: str | None = None) -> None:
    payload: dict[str, object] = {"Label": label, "KeepAlive": True}
    if brain_py is not None:
        payload["EnvironmentVariables"] = {"BRAIN_PY": brain_py}
    (launchd_dir / f"{label}.plist").write_bytes(plistlib.dumps(payload))


class _Listed:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.returncode = 0


def test_the_daemons_status_loop_ignores_a_leftover_build_plist(tmp_path: Path) -> None:
    """Not loaded and not counted: covering it would FAIL "installed but NOT LOADED"."""
    for label in (_WATCHER, _BUILD, _BRIEF):
        _write_plist(tmp_path, label)
    listed = f"PID\tStatus\tLabel\n4242\t0\t{_WATCHER}\n-\t0\t{_BRIEF}\n"

    with patch("brain.doctor_runtime.subprocess.run", return_value=_Listed(listed)):
        (check,) = daemon_doctor_check(
            launchd_dir=tmp_path, launchctl="launchctl", platform="darwin"
        )

    assert check.status == "ok", check.detail
    assert "2 agent(s)" in check.detail
    assert _BUILD not in check.detail


def test_the_daemon_code_check_ignores_a_leftover_build_plist(tmp_path: Path) -> None:
    """Its pinned interpreter is not one any installed job runs."""
    _write_plist(tmp_path, _BUILD, brain_py="/synthetic/other-install/bin/python")

    assert daemon_code_doctor_check(launchd_dir=tmp_path, platform="darwin") == []
