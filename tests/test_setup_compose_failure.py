"""`brain setup` when `docker compose up -d` is refused (F17, 0.4.0 QA row C02).

QA saw a raw ``CalledProcessError`` traceback: ``_compose_up`` ran ``up -d`` with
``check=True`` and nothing caught it, and its stderr was not captured, so the cause was
nowhere on screen. Setup now names the exit code, quotes the tail of compose's stderr, and
prints a hint, through the existing ``SetupError`` handler (exit 1).

**No real docker is ever run.** The end-to-end test puts a stub ``docker`` first on
``PATH``. The stub answers every argv itself, logs it, and execs nothing. ``HOME``,
``BRAIN_HOME`` and the vault are all under ``tmp_path``.
"""
from __future__ import annotations

import os
import socket
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from brain import _compose
from brain.cli import app

_STUB = """#!/bin/sh
# Synthetic docker for tests: never execs anything, logs every argv.
echo "$*" >> "{log}"
case "$*" in
  info*) exit 0 ;;
  *" pull "*) echo "stub: pull refused" >&2; exit 1 ;;
  *" up -d"*)
    echo "synthetic: Bind for 127.0.0.1:{port} failed: port is already allocated" >&2
    echo "synthetic: Error response from daemon" >&2
    exit 17 ;;
  *) echo "stub: unexpected docker call: $*" >&2; exit 99 ;;
esac
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def stub_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, int]:
    """A stub ``docker`` first on PATH; returns (its argv log, the port it says is taken)."""
    port = _free_port()
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    log = tmp_path / "docker-calls.log"
    docker = bin_dir / "docker"
    docker.write_text(_STUB.format(log=log, port=port), encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BRAIN_COMPOSE_PROJECT", "brain-test-never-used")
    # run_setup writes these into os.environ directly; registering them with
    # monkeypatch first makes the teardown restore the originals.
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path / "brain-home"))
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    return log, port


def test_a_refused_compose_up_names_its_stderr_and_exits_nonzero(
    tmp_path: Path, stub_docker: tuple[Path, int]
) -> None:
    log, port = stub_docker
    argv = [
        "setup", "--profile", "minimal", "--non-interactive",
        "--brain-home", str(tmp_path / "brain-home"), "--vault", str(tmp_path / "vault"),
        "--skip-skill", "--no-daemons", "--port", str(port),
    ]

    with (
        patch("brain.setup.ensure_shim"),
        # Fresh-install shape: no checkout .env for setup to link to.
        patch("brain.config._project_dotenv", return_value=tmp_path / "no-such.env"),
    ):
        result = CliRunner().invoke(app, argv, env={"COLUMNS": "300"})

    output = result.output
    assert result.exit_code == 1, output
    assert "Traceback" not in output
    assert "CalledProcessError" not in output
    assert "exit 17" in output
    assert "port is already allocated" in output
    assert "Error response from daemon" in output
    assert "brain setup" in output  # the hint says what to re-run
    assert str(port) in output
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[-1].endswith("up -d"), f"nothing may run after the refusal: {calls}"


def test_compose_failure_keeps_the_last_ten_nonempty_lines() -> None:
    stderr = "\n".join(f"line {n}" for n in range(1, 16)) + "\n\n\n"

    message = _compose.compose_failure(["docker", "compose", "up", "-d"], 3, stderr, pg_port=5499)

    assert "exit 3" in message
    assert "line 6" in message and "line 15" in message
    assert "line 5" not in message
    assert "5499" in message


def test_compose_failure_strips_terminal_control_characters() -> None:
    message = _compose.compose_failure(["docker"], 1, "bad \x1b[31mred\x1b[0m thing\n", pg_port=1)

    assert "\x1b" not in message
    assert "red" in message


def test_compose_failure_without_stderr_still_hints() -> None:
    message = _compose.compose_failure(["docker", "compose", "up", "-d"], 1, None, pg_port=1)

    assert "(no stderr)" in message
    assert "brain setup" in message
