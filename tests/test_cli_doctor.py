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

    Defaults to a model list containing the arctic model (the default
    backend) so the happy-path doctor tests don't trip the "model not
    loaded" warning. Tests targeting other backends should pass an
    explicit model list.
    """
    payload_models = (
        models
        if models is not None
        else [{"name": "snowflake-arctic-embed2"}, {"name": "qwen3-embedding:8b"}]
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
    """Daemon up but the active backend's model not pulled → soft warning, exit 0.

    Default backend is arctic, so the doctor expects ``snowflake-arctic-embed2``
    to be loaded and warns when it isn't.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    transport = _ok_ollama_transport(models=[{"name": "llama3:8b"}])
    with _patch_httpx_client(transport):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "ollama" in result.output
    assert "NOT loaded" in result.output
    assert "snowflake-arctic-embed2" in result.output
    assert "ollama pull" in result.output


def test_doctor_qwen3_backend_checks_qwen3_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BRAIN_EMBEDDER=qwen3`` → doctor expects ``qwen3-embedding:8b`` loaded."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_EMBEDDER", "qwen3")
    transport = _ok_ollama_transport(models=[{"name": "llama3:8b"}])
    with _patch_httpx_client(transport):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "NOT loaded" in result.output
    assert "qwen3-embedding:8b" in result.output


def test_doctor_voyage_backend_checks_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BRAIN_EMBEDDER=voyage`` with key set → ``voyage OK``, no Ollama check."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_EMBEDDER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    # _patch_httpx_client is *not* used — voyage path must not call Ollama.
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "voyage" in result.output
    assert "api key set" in result.output
    # Ollama check skipped — no "ollama" line on the voyage path.
    assert "ollama" not in result.output


def test_doctor_voyage_backend_fails_when_key_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any"
) -> None:
    """``BRAIN_EMBEDDER=voyage`` with no key → FAIL on the voyage line, exit 1.

    Isolates from the dev's project ``.env`` by chdir-ing into a tmp dir and
    pointing :func:`brain.config._project_dotenv` at a non-existent file —
    otherwise the project's real ``VOYAGE_API_KEY`` would leak in via dotenv.
    """
    from brain import config as config_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        config_module, "_project_dotenv", lambda: tmp_path / "no.env"
    )
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_EMBEDDER", "voyage")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code != 0
    combined = result.output + (result.stderr if result.stderr else "")
    assert "voyage" in combined
    assert "FAIL" in combined
    assert "VOYAGE_API_KEY" in combined


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

    # ``shutil.which`` is consulted for both ``gws`` and ``npx``; both
    # missing keeps doctor at exit 0 (gws warns, npx soft-warns) and
    # avoids accidentally matching the npx side of the new doctor flow.
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

    # ``shutil.which`` is called for gws AND npx — return a path for
    # gws and ``None`` for npx so the new Quartz check soft-warns
    # without trying to subprocess-run ``gws --version``.
    def _which(binary: str) -> str | None:
        if binary == "gws":
            return "/usr/local/bin/gws"
        return None

    with patch(
        "brain.cli.shutil.which", side_effect=_which
    ), _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "gws CLI" in result.output
    assert "OK" in result.output


# ---------------------------------------------------------------------------
# Vault drift line — surfaces ``_ingested/`` mirror DB-vs-disk drift in
# ``brain doctor``. Informational only (never flips exit code).
# ---------------------------------------------------------------------------


def _write_orphan(path: "Any", *, doc_id: str, title: str = "orphan") -> None:
    """Drop a stub orphan mirror file with the given fresh UUID into ``path``."""
    from brain.vault.frontmatter import dump_frontmatter

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        dump_frontmatter({"id": doc_id, "title": title}, "orphan body\n"),
        encoding="utf-8",
    )


def test_doctor_reports_zero_drift_clean_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any", test_db: psycopg.Connection
) -> None:
    """Empty vault + no ingested rows → ``vault drift     OK (...)`` with zeros."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    vault = tmp_path / "vault"
    (vault / "_ingested" / "manual").mkdir(parents=True)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "vault drift" in result.output
    assert "OK (0 mirrors, 0 NULL vault_path, 0 orphan files, 0 ghost rows)" in (
        result.output
    )
    # No suggested-fix hints when clean.
    assert "prune-orphans" not in result.output
    assert "vault export --force" not in result.output


def test_doctor_reports_drift_when_orphans_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any", test_db: psycopg.Connection
) -> None:
    """An orphan file under ``_ingested/`` triggers the yellow drift hint.

    Setup: a vault dir whose ``_ingested/manual/`` holds one file with a
    fresh UUID that has no matching DB row.
    Exercise: ``brain doctor``.
    Verify: the drift line names the orphan count and the prune-orphans
    suggested fix appears in the output.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    vault = tmp_path / "vault"
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    _write_orphan(
        vault / "_ingested" / "manual" / "orphan.md",
        doc_id="22222222-2222-4222-8222-222222222222",
    )
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "vault drift" in result.output
    # The exact counts: 0 ingested rows in DB, 0 NULL, 1 orphan, 0 ghost.
    assert "1 orphan files" in result.output
    assert "drift detected" in result.output
    assert "prune-orphans" in result.output
