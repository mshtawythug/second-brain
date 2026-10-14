"""Guards: with only the conftest environment, nothing in the suite reaches ``launchctl``.

Phase 8 adds tests on every code path that runs ``launchctl``: the retired-label
bootout, the setup gate, and ``--daemons``. ``tests/conftest.py`` stands two
recording stubs in front of the real binary. ``BRAIN_LAUNCHCTL`` names one of
them, and a session dir prepended to ``PATH`` holds the second, named
``launchctl``. Both record their argv and exit non-zero.

These tests prove that an UN-doubled call lands on the ``BRAIN_LAUNCHCTL`` stub.
They also prove it without ever running the real binary, including under the
mutations that show they can fail. A mutation that loses the env stub sends a
bare ``launchctl`` to the ``PATH`` stub, and the guard goes red at "the env stub
recorded the call".
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from brain.bin.launchd import (
    LaunchdError,
    install_main,
    install_plists,
    resolve_launchctl,
    uninstall_plists,
)
from tests.test_bin_scripts import _make_env, _new_stub_dir

_SYSTEM_BIN_DIRS = frozenset({"/bin", "/usr/bin", "/sbin", "/usr/sbin"})
_SRC = Path(__file__).resolve().parent.parent / "src" / "brain"


def _env_stub() -> Path:
    """The ``BRAIN_LAUNCHCTL`` stub, checked BEFORE anything invokes it."""
    raw = os.environ.get("BRAIN_LAUNCHCTL")
    assert raw, "conftest must point BRAIN_LAUNCHCTL at the session recording stub"
    stub = Path(raw)
    assert stub.is_file(), f"BRAIN_LAUNCHCTL names a missing file: {stub}"
    assert os.access(stub, os.X_OK), f"BRAIN_LAUNCHCTL stub is not executable: {stub}"
    assert str(stub.parent) not in _SYSTEM_BIN_DIRS, f"BRAIN_LAUNCHCTL is a real binary: {stub}"
    return stub


def _path_stub() -> Path:
    """What a bare ``launchctl`` resolves to on this process's ``PATH``."""
    found = shutil.which("launchctl")
    assert found is not None, "conftest must shadow a bare `launchctl` on PATH"
    assert str(Path(found).parent) not in _SYSTEM_BIN_DIRS, (
        f"a bare `launchctl` resolves to the real binary {found}; "
        "conftest's PATH-shadow dir is missing"
    )
    return Path(found)


def _calls(stub: Path) -> list[str]:
    log = stub.parent / "launchctl.calls"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """Per-test launchd + brain-home dirs, and macOS, for one guard test.

    ``install_plists`` writes plists and shims BEFORE its bootstrap fails. On the
    session dirs those files would outlive the test and show every later
    ``brain doctor`` test an "installed" watcher. ``sys.platform`` is patched so
    ``_maybe_install_launchd``'s macOS gate does not skip the call on Linux CI.
    """
    launchd_dir = tmp_path / "LaunchAgents"
    brain_home = tmp_path / "brain_home"
    brain_home.mkdir()
    monkeypatch.setenv("BRAIN_LAUNCHD_DIR", str(launchd_dir))
    monkeypatch.setenv("BRAIN_HOME", str(brain_home))
    monkeypatch.setattr(sys, "platform", "darwin")
    return launchd_dir, brain_home


def _assert_only_the_env_stub_was_called(
    env_before: int, path_before: int, marker: str
) -> None:
    env_new = _calls(_env_stub())[env_before:]
    path_new = _calls(_path_stub())[path_before:]
    assert any(marker in line for line in env_new), (
        f"the BRAIN_LAUNCHCTL stub recorded no call naming {marker!r}: "
        f"env stub saw {env_new!r}, PATH stub saw {path_new!r}"
    )
    assert path_new == [], f"a bare `launchctl` was invoked: {path_new!r}"


def test_conftest_points_brain_launchctl_at_a_recording_stub() -> None:
    stub = _env_stub()

    assert resolve_launchctl() == str(stub)


def test_conftest_shadows_a_bare_launchctl_on_path() -> None:
    path_stub = _path_stub()

    assert path_stub != _env_stub(), "the two layers must be distinct stubs"


def test_undoubled_install_plists_lands_on_the_env_stub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launchd_dir, brain_home = _isolate(monkeypatch, tmp_path)
    assert resolve_launchctl() == str(_env_stub())
    env_before, path_before = len(_calls(_env_stub())), len(_calls(_path_stub()))

    with pytest.raises(LaunchdError, match="bootstrap failed"):
        install_plists(brain_home, launchd_dir)

    _assert_only_the_env_stub_was_called(env_before, path_before, str(launchd_dir))


def test_undoubled_uninstall_plists_lands_on_the_env_stub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launchd_dir, _ = _isolate(monkeypatch, tmp_path)
    launchd_dir.mkdir()
    env_before, path_before = len(_calls(_env_stub())), len(_calls(_path_stub()))

    uninstall_plists(launchd_dir)

    _assert_only_the_env_stub_was_called(env_before, path_before, "print gui/")


def test_install_main_lands_on_the_env_stub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launchd_dir, _ = _isolate(monkeypatch, tmp_path)
    env_before, path_before = len(_calls(_env_stub())), len(_calls(_path_stub()))

    with pytest.raises(LaunchdError, match="bootstrap failed"):
        install_main()

    _assert_only_the_env_stub_was_called(env_before, path_before, str(launchd_dir))


def test_setup_daemons_install_lands_on_the_env_stub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from brain.setup import _maybe_install_launchd

    launchd_dir, brain_home = _isolate(monkeypatch, tmp_path)
    env_before, path_before = len(_calls(_env_stub())), len(_calls(_path_stub()))

    with pytest.raises(LaunchdError, match="bootstrap failed"):
        _maybe_install_launchd(
            vault_path=tmp_path / "vault",
            brain_home=brain_home,
            dry_run=False,
            daemons=True,
        )

    _assert_only_the_env_stub_was_called(env_before, path_before, str(launchd_dir))


def test_no_source_file_invokes_launchctl_by_absolute_path() -> None:
    """Both stub layers are bypassed by an absolute path, so none may exist."""
    offenders = [
        str(path.relative_to(_SRC))
        for path in sorted(_SRC.rglob("*"))
        if path.is_file()
        and path.suffix in {".py", ".sh", ".j2"}
        and "/bin/launchctl" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_bin_script_env_resolves_launchctl_to_a_stub(tmp_path: Path) -> None:
    """``_make_env`` rebuilds ``PATH`` from scratch, dropping both conftest layers.

    Its own default, plus the stub ``stub_dir`` seeds, must stand in for them.
    Static: nothing is run.
    """
    stub_dir = _new_stub_dir(tmp_path)
    vault_dir = tmp_path / "vault"

    env = _make_env(stub_dir=stub_dir, vault_dir=vault_dir)
    found = shutil.which("launchctl", path=env["PATH"])

    assert found is not None and Path(found).parent == stub_dir, (
        f"a bare `launchctl` in a bin-script subprocess resolves to {found!r}"
    )
    assert env.get("BRAIN_LAUNCHCTL") == str(stub_dir / "launchctl")
