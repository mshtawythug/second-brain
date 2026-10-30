"""RM-33: the opt-in ``com.brain.dropfolders`` LaunchAgent (``brain ingest-folder`` every 10 min).

Installed only by ``brain-install-launchd --with-dropfolders``, then **sticky**: the flagless
install every ``brain-up`` runs re-renders and re-bootstraps an installed plist and never
removes it; only ``brain-uninstall-launchd`` does. ``brain doctor``'s ``daemons`` row checks it
only when its plist exists (AC7, AC8).

Every launchctl here is a recording stub passed explicitly; no real ``launchctl`` is run.
Paths are synthetic.
"""

from __future__ import annotations

import plistlib
import re
from importlib.resources import files
from pathlib import Path

import pytest

import brain
from brain.bin.launchd import (
    _ALL_LABELS,
    _DROPFOLDERS_LABEL,
    _INSTALLED_LABELS,
    _OPTIONAL_LABELS,
    install_main,
    install_plists,
    render_plist,
    uninstall_plists,
)
from brain.doctor_runtime import DAEMON_LABELS, daemon_doctor_check

_DROPFOLDERS = "com.brain.dropfolders"
_WRAPPER = Path(brain.__file__).parent / "templates" / "bin" / "_brain-dropfolders-fg.sh"


def _recorder(tmp_path: Path) -> tuple[Path, Path]:
    """A launchctl stub that records argv; ``print`` says "not loaded"."""
    log = tmp_path / "launchctl.calls"
    stub = tmp_path / "launchctl"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'case "$1" in\n'
        "  print) exit 1 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, log


def _calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _render() -> dict[str, object]:
    text = render_plist(
        _DROPFOLDERS,
        brain_home=Path("/fake/brain"),
        vault_path=Path("/fake/vault"),
        pipx_bin_dir=Path("/fake/pipx/bin"),
        brain_py=Path("/fake/python"),
    )
    return dict(plistlib.loads(text.encode()))


@pytest.fixture
def brain_home(tmp_path: Path) -> Path:
    home = tmp_path / "brain"
    home.mkdir()
    return home


# ---------------------------------------------------------------------------
# the label and the plist (AC7: it renders)
# ---------------------------------------------------------------------------


def test_the_label_is_optional_swept_and_checked_only_when_installed() -> None:
    assert _DROPFOLDERS_LABEL == _DROPFOLDERS
    assert _DROPFOLDERS in _OPTIONAL_LABELS
    assert _DROPFOLDERS not in _INSTALLED_LABELS
    assert _DROPFOLDERS in _ALL_LABELS
    assert _DROPFOLDERS in DAEMON_LABELS


def test_the_plist_runs_the_wrapper_every_ten_minutes_once() -> None:
    data = _render()

    assert data["Label"] == _DROPFOLDERS
    assert data["ProgramArguments"] == ["/fake/brain/.shims/_brain-dropfolders-fg"]
    assert data["StartInterval"] == 600
    assert "KeepAlive" not in data
    assert "RunAtLoad" not in data
    env = data["EnvironmentVariables"]
    assert isinstance(env, dict)
    assert env["BRAIN_HOME"] == "/fake/brain"
    assert env["BRAIN_IGNORE_CWD_DOTENV"] == "1"
    assert env["BRAIN_PY"] == "/fake/python"
    assert data["StandardErrorPath"] == "/fake/brain/logs/com.brain.dropfolders.err.log"


def test_the_template_takes_only_paths() -> None:
    text = (files("brain.templates.launchd") / f"{_DROPFOLDERS}.plist.j2").read_text()

    assert set(re.findall(r"\{\{ *(\w+) *\}\}", text)) == {
        "brain_home", "user_home", "pipx_bin_dir", "vault_path", "brain_py"
    }


def test_the_wrapper_runs_the_configured_folders_over_two_days() -> None:
    text = _WRAPPER.read_text(encoding="utf-8")

    assert text.rstrip().endswith('exec "$PY" -m brain ingest-folder --configured --since 2d')
    assert "set -euo pipefail" in text


# ---------------------------------------------------------------------------
# install / uninstall (AC7, AC8)
# ---------------------------------------------------------------------------


def test_a_flagless_first_install_does_not_install_it(tmp_path: Path, brain_home: Path) -> None:
    stub, log = _recorder(tmp_path)
    agents = tmp_path / "agents"

    install_plists(brain_home=brain_home, launchd_dir=agents, launchctl=str(stub))

    assert not (agents / f"{_DROPFOLDERS}.plist").exists()
    assert not any(_DROPFOLDERS in call for call in _calls(log))


def test_the_wrapper_shim_is_installed_with_the_others(tmp_path: Path, brain_home: Path) -> None:
    stub, _ = _recorder(tmp_path)

    install_plists(brain_home=brain_home, launchd_dir=tmp_path / "agents", launchctl=str(stub))

    shim = brain_home / ".shims" / "_brain-dropfolders-fg"
    assert shim.read_bytes() == _WRAPPER.read_bytes()
    assert shim.stat().st_mode & 0o111


def test_with_dropfolders_installs_and_bootstraps_it(tmp_path: Path, brain_home: Path) -> None:
    stub, log = _recorder(tmp_path)
    agents = tmp_path / "agents"

    install_plists(
        brain_home=brain_home, launchd_dir=agents, launchctl=str(stub), with_dropfolders=True
    )

    plist = plistlib.loads((agents / f"{_DROPFOLDERS}.plist").read_bytes())
    assert plist["StartInterval"] == 600
    calls = _calls(log)
    assert any(c.startswith("bootstrap") and _DROPFOLDERS in c for c in calls)
    for label in _INSTALLED_LABELS:
        assert any(c.startswith("bootstrap") and label in c for c in calls), label


def test_a_later_flagless_install_keeps_and_re_renders_it(tmp_path: Path, brain_home: Path) -> None:
    """AC8: what ``brain-up`` runs on every start keeps it and brings a template change."""
    stub, log = _recorder(tmp_path)
    agents = tmp_path / "agents"
    install_plists(
        brain_home=brain_home, launchd_dir=agents, launchctl=str(stub), with_dropfolders=True
    )
    plist_path = agents / f"{_DROPFOLDERS}.plist"
    plist_path.write_text("<stale/>", encoding="utf-8")  # an older template's output
    log.unlink()

    install_plists(brain_home=brain_home, launchd_dir=agents, launchctl=str(stub))

    assert plistlib.loads(plist_path.read_bytes())["Label"] == _DROPFOLDERS
    calls = _calls(log)
    assert any(c.startswith("bootout") and _DROPFOLDERS in c for c in calls)
    assert any(c.startswith("bootstrap") and _DROPFOLDERS in c for c in calls)


def test_the_two_optional_labels_are_independent(
    tmp_path: Path, brain_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing the drop-folder job never installs the reranker, nor the reverse."""
    stub, _ = _recorder(tmp_path)
    agents = tmp_path / "agents"
    monkeypatch.delenv("BRAIN_RERANK_GGUF", raising=False)

    install_plists(
        brain_home=brain_home, launchd_dir=agents, launchctl=str(stub), with_dropfolders=True
    )
    install_plists(brain_home=brain_home, launchd_dir=agents, launchctl=str(stub))

    assert (agents / f"{_DROPFOLDERS}.plist").exists()
    assert not (agents / "com.brain.reranker.plist").exists()


def test_only_uninstall_removes_it(tmp_path: Path, brain_home: Path) -> None:
    stub, _ = _recorder(tmp_path)
    agents = tmp_path / "agents"
    install_plists(
        brain_home=brain_home, launchd_dir=agents, launchctl=str(stub), with_dropfolders=True
    )

    uninstall_plists(launchd_dir=agents, launchctl=str(stub))

    assert sorted(agents.glob("*.plist")) == []


def test_install_main_parses_with_dropfolders(
    tmp_path: Path, brain_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = tmp_path / "agents"
    stub, _ = _recorder(tmp_path)
    monkeypatch.setenv("BRAIN_HOME", str(brain_home))
    monkeypatch.setenv("BRAIN_LAUNCHD_DIR", str(agents))
    monkeypatch.setenv("BRAIN_LAUNCHCTL", str(stub))

    install_main([])
    assert not (agents / f"{_DROPFOLDERS}.plist").exists()

    install_main(["--with-dropfolders"])
    assert (agents / f"{_DROPFOLDERS}.plist").exists()

    install_main([])  # brain-up: flagless, and it stays
    assert (agents / f"{_DROPFOLDERS}.plist").exists()


# ---------------------------------------------------------------------------
# doctor's daemons row checks it only when its plist exists (AC7)
# ---------------------------------------------------------------------------


def _list_stub(tmp_path: Path, rows: dict[str, int]) -> Path:
    body = "PID\\tStatus\\tLabel\\n" + "".join(f"-\\t{st}\\t{lb}\\n" for lb, st in rows.items())
    stub = tmp_path / "launchctl-list"
    stub.write_text(f"#!/bin/sh\nprintf '{body}'\n", encoding="utf-8")
    stub.chmod(0o755)
    return stub


def test_doctor_does_not_report_it_when_never_installed(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "com.brain.watcher.plist").write_bytes(plistlib.dumps({"KeepAlive": True}))

    (check,) = daemon_doctor_check(
        launchd_dir=agents,
        launchctl=str(_list_stub(tmp_path, {"com.brain.watcher": 0})),
        platform="darwin",
    )

    assert check.status == "ok"
    assert _DROPFOLDERS not in check.detail


def test_doctor_checks_an_installed_job(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / f"{_DROPFOLDERS}.plist").write_bytes(plistlib.dumps({"StartInterval": 600}))

    (check,) = daemon_doctor_check(
        launchd_dir=agents,
        launchctl=str(_list_stub(tmp_path, {"com.brain.watcher": 0})),
        platform="darwin",
    )

    assert check.status == "fail"
    assert f"{_DROPFOLDERS} installed but NOT LOADED" in check.detail
