"""Tests for the `brain doctor` CLI command."""
import contextlib
import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import _model_loaded, _ollama_loaded_models, app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _ok_ollama_transport(
    models: list[dict[str, str]] | None = None,
) -> httpx.MockTransport:
    """Mock transport that returns 200 OK on ``GET /api/tags``.

    Defaults to a model list containing the configured Qwen3 model so the
    happy-path doctor tests don't trip the new "model not loaded" warning.
    """
    payload_models = (
        models if models is not None else [{"name": "qwen3-embedding:8b"}]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": payload_models})

    return httpx.MockTransport(handler)


def _down_ollama_transport() -> httpx.MockTransport:
    """Mock transport that simulates a connection failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    return httpx.MockTransport(handler)


@contextlib.contextmanager
def _patch_httpx_client(transport: httpx.MockTransport) -> Iterator[None]:
    """Swap ``httpx.Client`` so ``brain doctor`` routes through ``transport``."""
    real_client = httpx.Client

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    with patch("brain.cli.httpx.Client", side_effect=factory):
        yield


def test_doctor_passes_when_env_and_db_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_doctor_pings_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Ollama responds 200, doctor prints ``ollama OK``."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "ollama" in result.output
    assert "OK" in result.output


def test_doctor_warns_when_model_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Daemon up but qwen3-embedding:8b not pulled → soft warning, exit 0."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    transport = _ok_ollama_transport(models=[{"name": "llama3:8b"}])
    with _patch_httpx_client(transport):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "ollama" in result.output
    assert "NOT loaded" in result.output
    assert "qwen3-embedding:8b" in result.output
    assert "ollama pull" in result.output


def test_doctor_reports_ollama_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the Ollama HTTP call raises, doctor reports FAIL and exits non-zero."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    with _patch_httpx_client(_down_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code != 0
    combined = result.output + (result.stderr if result.stderr else "")
    assert "ollama" in combined.lower()
    assert "FAIL" in combined


def test_doctor_reports_missing_pgvector(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the vector extension is not installed, doctor should fail with a clear message."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    # Fake a connection whose pg_extension lookup returns no row.
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = None
    fake_ctx = MagicMock()
    fake_ctx.__enter__.return_value = fake_conn
    fake_ctx.__exit__.return_value = False

    with patch("brain.cli.connect", return_value=fake_ctx), _patch_httpx_client(
        _ok_ollama_transport()
    ):
        result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code != 0
    assert "pgvector" in result.output


def test_doctor_reports_database_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """psycopg.Error raised by connect should produce a postgres FAIL line and exit 1."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    def boom(_url: str) -> None:
        raise psycopg.OperationalError("could not connect to server")

    with patch("brain.cli.connect", side_effect=boom), _patch_httpx_client(
        _ok_ollama_transport()
    ):
        result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code != 0
    assert "postgres" in result.output
    assert "FAIL" in result.output


def test_doctor_reports_missing_gws(monkeypatch: pytest.MonkeyPatch) -> None:
    """When gws is not on PATH, doctor should note Gmail ingestion is disabled but still exit 0."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    with patch("brain.cli.shutil.which", return_value=None), _patch_httpx_client(
        _ok_ollama_transport()
    ):
        result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "gws CLI" in result.output
    assert "missing" in result.output


def test_ollama_loaded_models_handles_malformed_payloads() -> None:
    """Defensive branches in _ollama_loaded_models must return [] silently."""
    # Top-level not a dict.
    assert _ollama_loaded_models([1, 2, 3]) == []
    # "models" not a list.
    assert _ollama_loaded_models({"models": "oops"}) == []
    # entries that aren't dicts / are missing "name" / have a non-str name
    # are dropped, but the well-formed entry survives.
    payload = {
        "models": [
            "not a dict",
            {"no_name": "key"},
            {"name": 123},
            {"name": "qwen3-embedding:8b"},
        ]
    }
    assert _ollama_loaded_models(payload) == ["qwen3-embedding:8b"]


def test_model_loaded_accepts_bare_repo_name() -> None:
    """_model_loaded matches a bare repo (no tag) against any tagged variant."""
    assert _model_loaded("qwen3-embedding", ["qwen3-embedding:8b"])
    assert _model_loaded("qwen3-embedding:8b", ["qwen3-embedding:8b"])
    assert not _model_loaded("qwen3-embedding:8b", ["llama3:8b"])


def test_doctor_reports_gws_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """When gws IS on PATH, doctor should print the OK line for it."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    with patch(
        "brain.cli.shutil.which", return_value="/usr/local/bin/gws"
    ), _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "gws CLI" in result.output
    assert "OK" in result.output
