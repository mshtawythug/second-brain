"""F18/F37: ``-h``/``--help`` on the daemon scripts runs NOTHING; other arguments exit 2.

Before the fix ``brain-up``, ``brain-down``, ``brain-status`` and
``brain-uninstall-launchd`` ignored argv, so ``--help`` ran them (bootouts, pkill
sweeps, plist deletion, shim installs). The shell scripts behind them, and
launchd's ``_brain-*-fg`` wrappers, did the same when run by hand.

Every run here is hermetic: a fresh env whose PATH starts with a tmp dir of
recording stubs (``launchctl``, ``pkill``, ``docker``, ``brain``, ``python``,
the ``brain-install-launchd`` that ``brain-up.sh`` calls), every pid file, home
and LaunchAgents dir under ``tmp_path``, ``BRAIN_SKIP_VENV_AUTOLOAD=1`` so the
watcher wrapper can never reach the checkout's real ``brain``, and the database
URLs pointed at a closed port. "Zero side effects" means: no stub was called,
every path under ``tmp_path`` is byte-for-byte where it was (no ``.shims/``, no
pid file, the decoy plist intact), and the test-owned ``sleep`` is alive.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

pytestmark = pytest.mark.nodb

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN = REPO_ROOT / "bin"
TEMPLATES_BIN = REPO_ROOT / "src" / "brain" / "templates" / "bin"

# The four entry points F18/F37 named, and the three that already parsed first
# (pinned so a refactor cannot regress them).
_F18_SCRIPTS = ("brain-up", "brain-down", "brain-status", "brain-uninstall-launchd")
_ALREADY_PARSING = ("brain-install-launchd", "brain-rebuild", "brain-monitor")
_STUBS = ("launchctl", "pkill", "docker", "brain", "python", "install-launchd")

# Every packaged shell script, found by glob so a new template is covered the
# day it lands, plus the dev-checkout copy of the watcher wrapper.
_SHELL_SCRIPTS = sorted(TEMPLATES_BIN.glob("*.sh")) + [BIN / "_brain-watcher-fg"]


class Sandbox(NamedTuple):
    """The hermetic env plus the handles the side-effect assertions need."""

    env: dict[str, str]
    stubs: Path
    root: Path
    sleeper: subprocess.Popen[bytes]


def _write_stub(stubs: Path, name: str) -> None:
    target = stubs / name
    target.write_text(f'#!/bin/sh\necho "$@" >> "{stubs}/{name}.calls"\nexit 0\n')
    target.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path: Path) -> Iterator[Sandbox]:
    """A tmp world with recording stubs, a decoy plist and a live pid to protect."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in _STUBS:
        _write_stub(stubs, name)
    brain_home = tmp_path / "brain_home"
    brain_home.mkdir()
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.brain.watcher.plist").write_text("<plist>decoy</plist>\n")
    (tmp_path / "vault").mkdir()
    pids = tmp_path / "run"
    pids.mkdir()
    sleeper = subprocess.Popen(["/bin/sleep", "60"])  # noqa: S603 — fixed args
    try:
        (pids / "brain-watch.pid").write_text(f"{sleeper.pid}\n")
        env = {
            "PATH": f"{stubs}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "BRAIN_HOME": str(brain_home),
            "BRAIN_LAUNCHCTL": str(stubs / "launchctl"),
            "BRAIN_PKILL": str(stubs / "pkill"),
            "BRAIN_LAUNCHD_DIR": str(agents),
            "BRAIN_INSTALL_LAUNCHD": str(stubs / "install-launchd"),
            "BRAIN_PY": str(stubs / "python"),
            "BRAIN_WATCH_PID": str(pids / "brain-watch.pid"),
            "BRAIN_BUILD_PID": str(pids / "brain-build.pid"),
            "BRAIN_WIKI_PID": str(pids / "brain-wiki.pid"),
            "BRAIN_VAULT_PATH": str(tmp_path / "vault"),
            "BRAIN_SKIP_VENV_AUTOLOAD": "1",
            "BRAIN_IGNORE_CWD_DOTENV": "1",
            "DATABASE_URL": "postgresql://localhost:1/none",
            "BRAIN_PROD_DATABASE_URL": "postgresql://localhost:1/none",
        }
        yield Sandbox(env=env, stubs=stubs, root=tmp_path, sleeper=sleeper)
    finally:
        # By PID only, never pkill/killall.
        with contextlib.suppress(ProcessLookupError):
            sleeper.kill()
        sleeper.wait(timeout=5)


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every path under *root* with its size and mtime (directories included)."""
    return {
        str(p.relative_to(root)): (p.lstat().st_size, p.lstat().st_mtime_ns)
        for p in root.rglob("*")
    }


def _run(cmd: list[str], sandbox: Sandbox) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — list-form, no shell
        cmd,
        env=sandbox.env,
        cwd=sandbox.root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _assert_no_side_effects(
    sandbox: Sandbox,
    before: dict[str, tuple[int, int]],
    result: subprocess.CompletedProcess[str],
) -> None:
    called = {p.name: p.read_text() for p in sandbox.stubs.glob("*.calls")}
    detail = f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert called == {}, f"a stub was called: {called!r}; {detail}"
    assert _snapshot(sandbox.root) == before, f"files under tmp_path changed; {detail}"
    assert sandbox.sleeper.poll() is None, f"the test-owned sleep was killed; {detail}"


# ---------------------------------------------------------------------------
# The console scripts, through the dev-checkout bin/ wrappers (the user's path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("name", _F18_SCRIPTS + _ALREADY_PARSING)
def test_console_script_help_runs_nothing(sandbox: Sandbox, name: str, flag: str) -> None:
    before = _snapshot(sandbox.root)

    result = _run([str(BIN / name), flag], sandbox)

    assert result.returncode == 0, result.stderr
    assert f"usage: {name}" in result.stdout
    _assert_no_side_effects(sandbox, before, result)


@pytest.mark.parametrize("args", [["--bogus"], ["extra"]], ids=["flag", "positional"])
@pytest.mark.parametrize("name", _F18_SCRIPTS + _ALREADY_PARSING)
def test_console_script_rejects_arguments(sandbox: Sandbox, name: str, args: list[str]) -> None:
    before = _snapshot(sandbox.root)

    result = _run([str(BIN / name), *args], sandbox)

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "unrecognized arguments" in result.stderr or "invalid choice" in result.stderr
    _assert_no_side_effects(sandbox, before, result)


# ---------------------------------------------------------------------------
# The shell scripts themselves, run by hand
# ---------------------------------------------------------------------------


def _script_name(path: Path) -> str:
    return path.name.removesuffix(".sh")


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("script", _SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_shell_script_help_runs_nothing(sandbox: Sandbox, script: Path, flag: str) -> None:
    before = _snapshot(sandbox.root)

    result = _run(["/bin/bash", str(script), flag], sandbox)

    assert result.returncode == 0, result.stderr
    assert f"usage: {_script_name(script)}" in result.stdout.lower()
    _assert_no_side_effects(sandbox, before, result)


@pytest.mark.parametrize(
    "args", [["--bogus"], ["extra"], ["--help", "extra"]], ids=["flag", "positional", "help-plus"]
)
@pytest.mark.parametrize("script", _SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_shell_script_rejects_arguments(sandbox: Sandbox, script: Path, args: list[str]) -> None:
    before = _snapshot(sandbox.root)

    result = _run(["/bin/bash", str(script), *args], sandbox)

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "error: unknown argument" in result.stderr
    _assert_no_side_effects(sandbox, before, result)


# ---------------------------------------------------------------------------
# The shared helper, and brain uninstall's in-process call
# ---------------------------------------------------------------------------


def test_parse_no_args_help_exits_0_with_usage(capsys: pytest.CaptureFixture[str]) -> None:
    from brain.bin._launcher import parse_no_args

    with pytest.raises(SystemExit) as exc:
        parse_no_args("brain-x", "Does x. Takes no arguments.", ["--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: brain-x")
    assert "Does x. Takes no arguments." in out


def test_parse_no_args_rejects_anything_else(capsys: pytest.CaptureFixture[str]) -> None:
    from brain.bin._launcher import parse_no_args

    with pytest.raises(SystemExit) as exc:
        parse_no_args("brain-x", "Does x.", ["--json"])

    assert exc.value.code == 2
    assert "unrecognized arguments: --json" in capsys.readouterr().err


def test_parse_no_args_accepts_no_arguments() -> None:
    from brain.bin._launcher import parse_no_args

    parse_no_args("brain-x", "Does x.", [])


def test_brain_uninstall_launchd_step_ignores_brains_own_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``brain uninstall`` calls ``uninstall_main`` in-process, where ``sys.argv``
    is brain's own. That call must not parse it: a ``SystemExit(2)`` there would
    escape ``except Exception`` and stop ``brain uninstall`` half-way."""
    from unittest.mock import patch

    from brain.uninstall import run_uninstall

    home = tmp_path / "brain_home"
    home.mkdir()
    monkeypatch.setenv("BRAIN_LAUNCHD_DIR", str(tmp_path / "LaunchAgents"))
    monkeypatch.setenv("BRAIN_LAUNCHCTL", str(tmp_path / "no-such-launchctl"))
    monkeypatch.setattr(sys, "argv", ["brain", "uninstall", "--yes"])
    calls: list[list[str]] = []

    def _record(cmd: list[str], *_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1, "", "")

    with patch("subprocess.run", side_effect=_record), patch.object(sys, "platform", "darwin"):
        run_uninstall(yes=True, brain_home=home, vault_path=tmp_path / "vault")

    assert "launchd plists removed" in capsys.readouterr().out
    assert any(c[:2] == [str(tmp_path / "no-such-launchctl"), "print"] for c in calls), calls
