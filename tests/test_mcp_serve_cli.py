"""``brain mcp serve``'s command line: flags, refusals and exit codes (RM-46).

No test here opens a listening socket: refusals happen in ``validate_options``
before any bind, and the one test of a successful start records the options
``serve_http`` would have served with (a ``monkeypatch`` test double). The real
server is exercised by ``tests/test_mcp_http_protocol.py``. ``BRAIN_HOME`` is a
``tmp_path``, so no real token file is read.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from brain import mcp_http
from brain.cli import app
from brain.config import Config
from brain.mcp_auth import generate_token, write_token
from tests.conftest import isolate_config_dotenv

pytestmark = pytest.mark.nodb

runner = CliRunner()


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    isolate_config_dotenv(monkeypatch, tmp_path)
    brain_home = tmp_path / "brain_home"
    monkeypatch.setenv("BRAIN_HOME", str(brain_home))
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic@127.0.0.1:1/none")
    monkeypatch.setenv("BRAIN_EMBEDDER", "none")
    monkeypatch.delenv("BRAIN_MCP_TOKEN", raising=False)
    monkeypatch.delenv("BRAIN_MCP_TOKEN_PATH", raising=False)
    return brain_home


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[mcp_http.HttpServeOptions]:
    seen: list[mcp_http.HttpServeOptions] = []

    def fake_serve(opts: mcp_http.HttpServeOptions, cfg: Config, **_: Any) -> None:
        seen.append(opts)

    monkeypatch.setattr(mcp_http, "serve_http", fake_serve)
    return seen


def test_serve_is_listed_with_its_safety_flags() -> None:
    result = runner.invoke(app, ["mcp", "serve", "--help"])

    assert result.exit_code == 0
    for flag in ("--allow-remote", "--allow-writes", "--include-confidential", "--no-auth",
                 "--allowed-host", "--call-timeout", "--max-body-bytes", "--token-file"):
        assert flag in result.output


def test_a_wide_open_bind_exits_2_before_binding(home: Path) -> None:
    result = runner.invoke(app, ["mcp", "serve", "--host", "0.0.0.0"])

    assert result.exit_code == 2
    assert "refusing to bind 0.0.0.0" in result.output
    assert "--allow-remote" in result.output


def test_allow_remote_without_a_token_exits_2(home: Path) -> None:
    result = runner.invoke(app, ["mcp", "serve", "--host", "0.0.0.0", "--allow-remote"])

    assert result.exit_code == 2
    assert "token is mandatory" in result.output


def test_loopback_without_a_token_or_no_auth_exits_2(home: Path) -> None:
    result = runner.invoke(app, ["mcp", "serve"])

    assert result.exit_code == 2
    assert "brain mcp token --rotate" in result.output


def test_an_unsafe_token_file_exits_2(home: Path, tmp_path: Path) -> None:
    real = tmp_path / "real"
    write_token(real, generate_token())
    home.mkdir(parents=True)
    (home / "mcp_token").symlink_to(real)

    result = runner.invoke(app, ["mcp", "serve"])

    assert result.exit_code == 2
    assert "symbolic link" in result.output


def test_a_malformed_env_token_exits_2(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_MCP_TOKEN", "weak-synthetic")

    result = runner.invoke(app, ["mcp", "serve"])

    assert result.exit_code == 2
    assert "weak-synthetic" not in result.output


def test_a_config_failure_exits_1(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    result = runner.invoke(app, ["mcp", "serve", "--no-auth"])

    assert result.exit_code == 1
    assert "DATABASE_URL" in result.output


def test_defaults_map_to_a_loopback_read_only_server(
    home: Path, recorded: list[mcp_http.HttpServeOptions]
) -> None:
    write_token(home / "mcp_token", generate_token())

    result = runner.invoke(app, ["mcp", "serve"])

    assert result.exit_code == 0, result.output
    (opts,) = recorded
    assert opts.host == "127.0.0.1"
    assert opts.port == 8787
    assert opts.path == "/mcp"
    assert opts.allow_writes is False
    assert opts.include_confidential is False
    assert opts.allow_remote is False
    assert opts.no_auth is False
    assert opts.token is not None and opts.token.path == home / "mcp_token"


def test_every_flag_reaches_the_options(
    home: Path, tmp_path: Path, recorded: list[mcp_http.HttpServeOptions]
) -> None:
    token_file = tmp_path / "explicit_token"
    write_token(token_file, generate_token())

    result = runner.invoke(
        app,
        [
            "mcp", "serve", "--host", "192.0.2.10", "--port", "9000", "--path", "/brain",
            "--allow-remote", "--allow-writes", "--include-confidential",
            "--allowed-host", "brain.tailnet.example.com", "--allowed-host", "192.0.2.11",
            "--token-file", str(token_file), "--max-body-bytes", "2048",
            "--call-timeout", "30", "--log-level", "debug",
        ],
    )

    assert result.exit_code == 0, result.output
    (opts,) = recorded
    assert (opts.host, opts.port, opts.path) == ("192.0.2.10", 9000, "/brain")
    assert opts.allow_remote and opts.allow_writes and opts.include_confidential
    assert opts.allowed_hosts == ("brain.tailnet.example.com", "192.0.2.11")
    assert opts.token is not None and opts.token.path == token_file
    assert (opts.max_body_bytes, opts.call_timeout_s, opts.log_level) == (2048, 30.0, "DEBUG")


def test_no_auth_does_not_read_any_token(
    home: Path, monkeypatch: pytest.MonkeyPatch, recorded: list[mcp_http.HttpServeOptions]
) -> None:
    monkeypatch.setenv("BRAIN_MCP_TOKEN_PATH", str(home / "does-not-matter"))

    result = runner.invoke(app, ["mcp", "serve", "--no-auth"])

    assert result.exit_code == 0, result.output
    assert recorded[0].token is None and recorded[0].no_auth is True
