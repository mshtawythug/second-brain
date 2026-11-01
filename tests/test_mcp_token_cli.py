"""``brain mcp token``: mint, rotate, show and describe the bearer token (RM-46).

``BRAIN_HOME`` is a ``tmp_path`` in every test, so the owner's real token file
is never read or written; the default path is asserted to be under it.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.mcp_auth import fingerprint, read_token
from tests.conftest import isolate_config_dotenv

pytestmark = pytest.mark.nodb

runner = CliRunner()


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    isolate_config_dotenv(monkeypatch, tmp_path)
    brain_home = tmp_path / "brain_home"
    monkeypatch.setenv("BRAIN_HOME", str(brain_home))
    monkeypatch.delenv("BRAIN_MCP_TOKEN", raising=False)
    monkeypatch.delenv("BRAIN_MCP_TOKEN_PATH", raising=False)
    return brain_home


def test_mcp_is_a_registered_sub_app() -> None:
    result = runner.invoke(app, ["mcp", "--help"])

    assert result.exit_code == 0
    assert "token" in result.output


def test_rotate_creates_the_default_file_at_mode_0600(home: Path) -> None:
    result = runner.invoke(app, ["mcp", "token", "--rotate"])

    path = home / "mcp_token"
    assert result.exit_code == 0, result.output
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    secret = read_token(path).secret
    assert secret not in result.output
    assert fingerprint(secret) in result.output


def test_rotate_replaces_an_existing_token(home: Path) -> None:
    runner.invoke(app, ["mcp", "token", "--rotate"])
    old = read_token(home / "mcp_token").secret

    result = runner.invoke(app, ["mcp", "token", "--rotate"])

    assert result.exit_code == 0
    assert read_token(home / "mcp_token").secret != old
    assert sorted(p.name for p in home.iterdir()) == ["mcp_token"]


def test_describe_hides_the_secret_by_default(home: Path) -> None:
    runner.invoke(app, ["mcp", "token", "--rotate"])
    secret = read_token(home / "mcp_token").secret

    result = runner.invoke(app, ["mcp", "token"])

    assert result.exit_code == 0
    assert secret not in result.output
    assert "0600" in result.output
    assert fingerprint(secret) in result.output
    assert "--show" in result.output


def test_show_prints_the_secret(home: Path) -> None:
    runner.invoke(app, ["mcp", "token", "--rotate"])
    secret = read_token(home / "mcp_token").secret

    result = runner.invoke(app, ["mcp", "token", "--show"])

    assert result.exit_code == 0
    assert secret in result.output


def test_json_shape_without_show(home: Path) -> None:
    runner.invoke(app, ["mcp", "token", "--rotate"])
    path = home / "mcp_token"

    result = runner.invoke(app, ["mcp", "token", "--json"])

    payload = json.loads(result.output)
    assert set(payload) == {"path", "exists", "mode", "mode_ok", "fingerprint", "token"}
    assert payload["path"] == str(path)
    assert payload["exists"] is True
    assert payload["mode"] == "0600"
    assert payload["mode_ok"] is True
    assert payload["token"] is None


def test_json_with_show_carries_the_token(home: Path) -> None:
    runner.invoke(app, ["mcp", "token", "--rotate"])

    result = runner.invoke(app, ["mcp", "token", "--json", "--show"])

    payload = json.loads(result.output)
    assert payload["token"] == read_token(home / "mcp_token").secret


def test_no_token_yet_names_the_fix(home: Path) -> None:
    result = runner.invoke(app, ["mcp", "token"])

    assert result.exit_code == 1
    assert "brain mcp token --rotate" in result.output


def test_no_token_yet_as_json_says_it_does_not_exist(home: Path) -> None:
    result = runner.invoke(app, ["mcp", "token", "--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 1
    assert payload["exists"] is False
    assert payload["fingerprint"] is None


def test_path_option_and_env_path_are_honoured(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "explicit_token"
    from_env = tmp_path / "env_token"
    monkeypatch.setenv("BRAIN_MCP_TOKEN_PATH", str(from_env))

    first = runner.invoke(app, ["mcp", "token", "--rotate", "--path", str(explicit)])
    second = runner.invoke(app, ["mcp", "token", "--rotate"])

    assert first.exit_code == 0 and second.exit_code == 0
    assert explicit.is_file() and from_env.is_file()
    assert not (home / "mcp_token").exists()


def test_a_loose_mode_is_flagged(home: Path) -> None:
    runner.invoke(app, ["mcp", "token", "--rotate"])
    (home / "mcp_token").chmod(0o644)

    result = runner.invoke(app, ["mcp", "token", "--json"])

    payload = json.loads(result.output)
    assert payload["mode"] == "0644"
    assert payload["mode_ok"] is False


def test_an_unsafe_file_exits_2(home: Path, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.write_text("A" * 43 + "\n", encoding="ascii")
    home.mkdir(parents=True)
    (home / "mcp_token").symlink_to(real)

    result = runner.invoke(app, ["mcp", "token"])

    assert result.exit_code == 2
    assert "symbolic link" in result.output


def test_a_malformed_env_token_exits_2_without_echoing_it(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_MCP_TOKEN", "weak-synthetic")

    result = runner.invoke(app, ["mcp", "token"])

    assert result.exit_code == 2
    assert "BRAIN_MCP_TOKEN" in result.output
    assert "weak-synthetic" not in result.output
