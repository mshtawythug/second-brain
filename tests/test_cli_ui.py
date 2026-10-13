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


# --------------------------------------------------------------------------
# Phase 7 / Q1 — `--lan`, the wildcard-bind notice, and the opened URL
# --------------------------------------------------------------------------

LAN_IP = "192.0.2.44"  # TEST-NET-1 (RFC 5737): synthetic


class _Started:
    """What the command handed to the (replaced) server layer."""

    def __init__(self) -> None:
        self.context_kwargs: dict[str, object] = {}
        self.serve_kwargs: dict[str, object] = {}


@pytest.fixture
def started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _Started:
    """Run `brain ui` up to the bind without binding anything.

    ``resolve_port``, ``build_context`` and ``serve`` are replaced with pytest
    ``monkeypatch`` (standard test doubles, CLAUDE.md rule 13). The command
    imports them from ``brain.ui.server`` inside its body, so replacing the
    module attributes is what it sees.
    """
    from types import SimpleNamespace

    import brain.ui.server as server

    record = _Started()
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    monkeypatch.setattr(
        "brain.cli_ui._build_embedder", lambda cfg: object()
    )
    monkeypatch.setattr(server, "resolve_port", lambda port, **kw: port)

    def fake_build_context(cfg: object, **kwargs: object) -> object:
        record.context_kwargs = kwargs
        return SimpleNamespace(
            serve_confidential_bodies=False, notices=(), token=kwargs["token"]
        )

    def fake_serve(context: object, **kwargs: object) -> None:
        record.serve_kwargs = kwargs

    monkeypatch.setattr(server, "build_context", fake_build_context)
    monkeypatch.setattr(server, "serve", fake_serve)
    monkeypatch.setattr(
        "brain.ui.lan.detect_lan_address", lambda *a, **k: LAN_IP
    )
    return record


def test_lan_binds_the_lan_address_with_a_minted_token(
    app: typer.Typer, started: _Started
) -> None:
    """E7: the convenience never skips the token rule — it mints one."""
    result = runner.invoke(app, ["ui", "--lan", "--no-open"])

    assert result.exit_code == 0, result.output
    assert started.context_kwargs["host"] == LAN_IP
    token = started.context_kwargs["token"]
    assert isinstance(token, str) and len(token) >= 32, (
        "--lan must always run with a token; an empty one would bind the LAN "
        "unauthenticated"
    )
    assert f"http://{LAN_IP}:8765/#t={token}" in result.output
    assert started.serve_kwargs["host"] == LAN_IP


def test_lan_uses_a_supplied_token(app: typer.Typer, started: _Started) -> None:
    result = runner.invoke(
        app, ["ui", "--lan", "--token", "a-stable-secret", "--no-open"]
    )
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["token"] == "a-stable-secret"


def test_lan_tells_the_user_to_open_the_url_on_another_device(
    app: typer.Typer, started: _Started
) -> None:
    result = runner.invoke(app, ["ui", "--lan", "--no-open"])
    assert "on your phone" in result.output


def test_lan_and_host_together_is_a_usage_error(
    app: typer.Typer, started: _Started
) -> None:
    """Exit 2 before anything is resolved — two answers to one question."""
    result = runner.invoke(
        app, ["ui", "--lan", "--host", "192.0.2.9", "--token", "x", "--no-open"]
    )
    assert result.exit_code == 2
    assert started.context_kwargs == {}


def test_lan_without_a_network_exits_one_with_the_override(
    app: typer.Typer, started: _Started, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brain.ui.lan import LanAddressError

    def no_route(*args: object, **kwargs: object) -> str:
        raise LanAddressError("--lan found no network route\n  Pass --host …")

    monkeypatch.setattr("brain.ui.lan.detect_lan_address", no_route)
    result = runner.invoke(app, ["ui", "--lan", "--no-open"])
    assert result.exit_code == 1
    assert started.context_kwargs == {}


def test_wildcard_bind_warns_that_other_devices_are_refused(
    app: typer.Typer, started: _Started
) -> None:
    """E9: the recipe the docs used to ship serves no phone; say so at startup."""
    result = runner.invoke(
        app, ["ui", "--host", "0.0.0.0", "--token", "x", "--no-open"]
    )
    assert result.exit_code == 0, result.output
    assert "--lan" in result.output
    assert "400" in result.output


def test_a_lan_bind_does_not_print_the_wildcard_warning(
    app: typer.Typer, started: _Started
) -> None:
    result = runner.invoke(app, ["ui", "--lan", "--no-open"])
    assert "Host allowlist" not in result.output


def test_browser_url_carries_the_token_whenever_one_is_set() -> None:
    """The opened tab needs the fragment too, or every API call is a 403.

    Regression: ``serve`` opened ``http://host:port/`` with no ``#t=``, so a
    tokened server opened a page that could not load a single note, and the
    panel printed the fragment only for non-loopback binds.
    """
    from brain.ui.server import browser_url

    assert browser_url("127.0.0.1", 8765, "") == "http://127.0.0.1:8765/"
    assert browser_url(LAN_IP, 8766, "s3cret") == f"http://{LAN_IP}:8766/#t=s3cret"
    assert browser_url("127.0.0.1", 8765, "s3cret").endswith("/#t=s3cret")


def test_serve_opens_the_tokened_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``serve`` must hand the opener the same URL the panel prints.

    uvicorn's ``Server`` is replaced with a double whose ``run`` performs the
    startup hook and returns, so nothing binds.
    """
    import asyncio
    import contextlib
    from dataclasses import replace

    import uvicorn

    from brain.ui.context import UiContext
    from brain.ui.server import serve

    class _Server:
        def __init__(self, config: object) -> None:
            self.config = config

        async def startup(self, *args: object, **kwargs: object) -> None:
            return None

        def run(self) -> None:
            asyncio.run(self.startup())

    monkeypatch.setattr(uvicorn, "Server", _Server)

    @contextlib.contextmanager
    def conn_factory():  # type: ignore[no-untyped-def]
        yield None

    context = UiContext(
        cfg=object(),  # type: ignore[arg-type]
        conn_factory=conn_factory,
        embedder=object(),
        search_fn=lambda *a, **k: [],
        token="s3cret",
        allowed_origin=f"http://{LAN_IP}:8765",
        allowed_hosts=frozenset({LAN_IP}),
    )
    opened: list[str] = []
    # FTS-only shape, so the boot-time warm-up is skipped rather than faked.
    context = replace(context, embedder=None)
    serve(context, host=LAN_IP, port=8765, open_browser=True, opener=opened.append)

    assert opened == [f"http://{LAN_IP}:8765/#t=s3cret"]
