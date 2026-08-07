"""The `brain ui` Typer surface.

Nothing here binds a socket or blocks: ``serve`` and the browser opener are
injected, so the command's argument handling, gating and panel output are tested
without a server ever starting.

The sub-app is registered into a throwaway ``typer.Typer`` rather than imported
from ``brain.cli``. That is deliberate — ``cli.py`` is coordinator-owned and the
two-line registration has not landed yet, so testing through a local app proves
``register_ui_commands`` works and keeps these tests green either way.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from brain.cli_ui import register_ui_commands
from brain.ui.security import is_loopback
from brain.ui.server import DEFAULT_PORT, resolve_port

runner = CliRunner()


@pytest.fixture
def app() -> typer.Typer:
    root = typer.Typer()
    register_ui_commands(root)
    return root


def test_ui_command_is_registered(app: typer.Typer) -> None:
    result = runner.invoke(app, ["ui", "--help"])
    assert result.exit_code == 0
    for flag in (
        "--port",
        "--host",
        "--read-only",
        "--token",
        "--auto-port",
        "--include-confidential",
    ):
        assert flag in result.output


def test_help_does_not_need_a_database(app: typer.Typer) -> None:
    """`--help` must work on a machine with no Postgres running at all."""
    result = runner.invoke(app, ["ui", "--help"])
    assert result.exit_code == 0


def test_non_loopback_without_a_token_is_a_usage_error(app: typer.Typer) -> None:
    """Exit 2, before any config load or bind.

    The corpus has no authentication; binding it to every interface without a
    shared secret would publish the user's entire brain to the local network.
    """
    result = runner.invoke(app, ["ui", "--host", "0.0.0.0", "--no-open"])
    assert result.exit_code == 2


def test_default_bind_is_loopback() -> None:
    from brain.cli_ui import ui

    defaults = ui.__defaults__ or ()
    assert any(
        getattr(d, "default", None) == "127.0.0.1" for d in defaults
    ), "the --host default must be 127.0.0.1"


def test_default_port_avoids_every_port_the_repo_uses() -> None:
    """55432 prod, 5434 test, 55433 demo, 8080 wiki/Caddy, 11434 Ollama."""
    assert DEFAULT_PORT == 8765
    assert DEFAULT_PORT not in {55432, 5434, 55433, 8080, 11434}


def test_loopback_detection_rejects_all_interfaces() -> None:
    assert is_loopback("127.0.0.1")
    assert not is_loopback("0.0.0.0")


def test_resolve_port_returns_a_free_port() -> None:
    assert resolve_port(DEFAULT_PORT + 4000, auto=True) >= DEFAULT_PORT + 4000


def test_resolve_port_bumps_past_a_busy_one() -> None:
    import socket

    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        busy = sock.getsockname()[1]
        sock.listen(1)
        assert resolve_port(busy, auto=True) != busy


def test_no_auto_port_refuses_with_the_remediation_text() -> None:
    import socket

    from brain.errors import BrainError

    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        busy = sock.getsockname()[1]
        sock.listen(1)
        with pytest.raises(BrainError) as excinfo:
            resolve_port(busy, auto=False)
    message = str(excinfo.value)
    assert "already in use" in message
    assert "--no-auto-port" in message
    assert "--port" in message


def test_missing_vault_exits_one(
    app: typer.Typer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing vault is a config problem, reported before anything binds."""
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "does-not-exist"))
    result = runner.invoke(app, ["ui", "--no-open"])
    assert result.exit_code == 1


def test_describe_db_never_leaks_credentials() -> None:
    """The panel prints dbname @ host:port — never the URL, never the password."""
    from brain.cli_ui import _describe_db

    described = _describe_db("postgresql://brain:sup3rs3cret@localhost:55432/second_brain")
    assert described == "second_brain @ localhost:55432"
    assert "sup3rs3cret" not in described
    assert "postgresql://" not in described


def test_importing_brain_cli_does_not_pull_starlette_or_uvicorn() -> None:
    """Import-cost discipline, asserted in a clean interpreter.

    ``cli_ui`` imports ``brain.ui.server`` — and through it starlette and
    uvicorn — only inside the command body. A module-scope import would add
    that cost to EVERY ``brain`` invocation, including ``brain --help``.
    Run in a subprocess because this test session has already imported both.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import brain.cli, sys;"
            "print('starlette' in sys.modules, 'uvicorn' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False False", (
        f"brain.cli now imports a web dependency at module scope: {result.stdout!r}"
    )
