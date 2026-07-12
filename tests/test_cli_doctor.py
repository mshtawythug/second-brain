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

from brain.cli import _check_age, _model_loaded, _ollama_loaded_models, app
from brain.db import DEFAULT_GRAPH_NAME, bootstrap_age, connect
from brain.errors import AgeBootstrapError

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
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


# ---------------------------------------------------------------------------
# Parallel external probes (Task 4.2) — the embedder HTTP/API, gws, and npx
# probes run concurrently with the serial DB block. Two invariants matter:
# (1) one probe's failure must not suppress the others, and (2) the printed
# order is fixed regardless of which probe finishes first.
# ---------------------------------------------------------------------------


def test_doctor_failing_probe_does_not_suppress_other_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected crash in one external probe surfaces as that check's FAIL
    without crashing doctor or hiding the other checks."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    boom = RuntimeError("simulated npx probe crash")
    with patch("brain.cli._probe_npx", side_effect=boom), _patch_httpx_client(
        _ok_ollama_transport()
    ):
        result = CliRunner().invoke(app, ["doctor"])
    combined = result.output + (result.stderr if result.stderr else "")
    # The crashed probe is isolated: doctor still ran the survivors.
    assert "postgres" in combined
    assert "ollama" in combined
    assert "gws CLI" in combined
    # The crash is surfaced as the npx check's own FAIL, non-zero exit.
    assert "quartz/npx" in combined
    assert "FAIL" in combined
    assert result.exit_code != 0


def test_doctor_external_probe_output_order_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parallelism must not reorder output: postgres → embedder → gws → npx."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert (
        out.index("postgres")
        < out.index("ollama")
        < out.index("gws CLI")
        < out.index("quartz/npx")
    )


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
    # Block the BRAIN_HOME source so a real ~/.brain/.env with VOYAGE_API_KEY
    # can't leak in and make this test pass for the wrong reason.
    monkeypatch.setattr(
        config_module, "_brain_home_dotenv", lambda: tmp_path / "no_brain_home.env"
    )
    monkeypatch.delenv("BRAIN_HOME", raising=False)
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


# ---------------------------------------------------------------------------
# Apache AGE line (wave G0-5) — surfaces the GraphRAG backend's health in
# ``brain doctor``. Soft check: every failure mode is a yellow WARN that never
# flips the exit code. Runs against the live AGE test instance (port 5434) per
# the drift-test precedent above.
# ---------------------------------------------------------------------------


def test_doctor_age_ok_when_extension_and_graph_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any", test_db: psycopg.Connection
) -> None:
    """AGE extension installed + ``brain_graph`` bootstrapped → ``age OK`` line.

    Setup: the per-test reset leaves ``age`` installed but drops the graph;
    bootstrap it so the canonical graph is present.
    Exercise: ``brain doctor`` (Ollama mocked OK).
    Verify: the OK line reports the extversion and graph presence, exit 0.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    bootstrap_age(test_db)
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "age             OK (age " in result.output
    assert f"graph {DEFAULT_GRAPH_NAME} present)" in result.output


def test_doctor_age_warns_when_graph_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any", test_db: psycopg.Connection
) -> None:
    """AGE present but ``brain_graph`` not bootstrapped → WARN with init hint.

    The per-test reset drops the graph and does NOT recreate it, so doctor must
    flag the absent graph and point at ``brain init`` — without failing.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "age             WARN" in result.output
    assert f"graph {DEFAULT_GRAPH_NAME} absent" in result.output
    assert "brain init" in result.output


def test_doctor_age_warns_when_available_but_not_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any", test_db: psycopg.Connection
) -> None:
    """AGE installable (control file present) but not yet installed → init hint.

    Drops the extension on the AGE test image so ``age`` is absent from
    ``pg_extension`` but still present in ``pg_available_extensions`` — i.e. an
    AGE-capable DB that simply hasn't run ``brain init`` yet. doctor must point
    at ``brain init``, NOT at an image cut-over. Restores the extension after.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    test_db.execute("DROP EXTENSION IF EXISTS age CASCADE")
    try:
        with _patch_httpx_client(_ok_ollama_transport()):
            result = CliRunner().invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "age             WARN" in result.output
        assert "available but not installed" in result.output
        assert "brain init" in result.output
        # Must NOT mislead toward an image rebuild — AGE is installable here.
        assert "lacks Apache AGE" not in result.output
    finally:
        test_db.execute("CREATE EXTENSION IF NOT EXISTS age CASCADE")


def test_doctor_age_warns_when_age_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any"
) -> None:
    """AGE neither installed NOR installable → WARN pointing at the image cut-over.

    Simulates a stock-pgvector image: ``_installed_extension_versions`` reports
    no ``age`` row AND ``age_extension_available`` is False (no control file).
    doctor must recommend rebuilding/cutting over to the AGE image, not
    ``brain init`` (which would fail to install a non-existent extension).
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    with (
        patch(
            "brain.cli._installed_extension_versions",
            return_value={"vector": "0.8.0", "pgcrypto": "1.3"},
        ),
        patch("brain.cli.age_extension_available", return_value=False),
        _patch_httpx_client(_ok_ollama_transport()),
    ):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "age             WARN" in result.output
    assert "lacks Apache AGE" in result.output
    assert "docs/specs/2026-05-20-graphrag-age-image.md" in result.output


def test_doctor_age_warns_when_availability_probe_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any"
) -> None:
    """The availability probe raising must WARN, not flip doctor to FAIL/exit 1.

    The AGE check is WARN-only. With ``age`` absent from ``pg_extension``, a
    ``psycopg.Error`` from ``age_extension_available`` must be caught and
    degraded to a WARN — never escape to doctor()'s outer DB handler (which would
    exit 1). Patches the probe seam to raise; asserts WARN + exit 0.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    boom = psycopg.OperationalError("simulated pg_available_extensions failure")
    with (
        patch(
            "brain.cli._installed_extension_versions",
            return_value={"vector": "0.8.0", "pgcrypto": "1.3"},
        ),
        patch("brain.cli.age_extension_available", side_effect=boom),
        _patch_httpx_client(_ok_ollama_transport()),
    ):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "age             WARN" in result.output
    assert "couldn't determine AGE availability" in result.output


def test_doctor_age_warns_when_load_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any", test_db: psycopg.Connection
) -> None:
    """``LOAD 'age'`` raising AgeBootstrapError → WARN about the missing library.

    The extension row is present (so the probe passes) but the shared library
    fails to load; doctor must surface a preload remediation, never fail. Uses
    ``unittest.mock.patch`` on the cli's ``load_age`` seam (an allowed test
    double with automatic cleanup, not banned monkey-patching).
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    bootstrap_age(test_db)
    boom = AgeBootstrapError("failed to LOAD Apache AGE: simulated")
    with patch("brain.cli.load_age", side_effect=boom), _patch_httpx_client(
        _ok_ollama_transport()
    ):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "age             WARN" in result.output
    assert "`LOAD 'age'` failed" in result.output
    assert "isn't loadable in this database/image" in result.output
    assert "docs/specs/2026-05-20-graphrag-age-image.md" in result.output


def test_doctor_age_warns_when_extension_probe_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any"
) -> None:
    """A ``psycopg.Error`` from the ``pg_extension`` probe → WARN, exit 0.

    Patches the cli's ``_installed_extension_versions`` seam to raise; doctor
    must surface a probe-failed WARN without failing the run.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    boom = psycopg.OperationalError("simulated pg_extension probe failure")
    with patch(
        "brain.cli._installed_extension_versions", side_effect=boom
    ), _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "age             WARN" in result.output
    assert "extension probe failed" in result.output


def test_doctor_age_warns_when_support_extensions_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any"
) -> None:
    """``age`` present but ``vector``/``pgcrypto`` absent → WARN naming them.

    Patches the extension probe to report only ``age`` so the support-extension
    branch fires (it cannot be reproduced on the live DB without a destructive
    drop of vector/pgcrypto, which other code depends on).
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    with patch(
        "brain.cli._installed_extension_versions",
        return_value={"age": "1.5.0"},
    ), _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "age             WARN" in result.output
    assert "missing extension(s): vector, pgcrypto" in result.output
    assert "brain init" in result.output


def test_doctor_age_warns_when_load_age_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any"
) -> None:
    """Defensive branch: extension row present but ``load_age`` returns False.

    Simulates the extension being reported by ``pg_extension`` while the shared
    library is unavailable at LOAD time; doctor treats it as the image-missing
    case (WARN), never a failure.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    present = {"age": "1.5.0", "vector": "0.8.0", "pgcrypto": "1.3"}
    with patch(
        "brain.cli._installed_extension_versions", return_value=present
    ), patch("brain.cli.load_age", return_value=False), _patch_httpx_client(
        _ok_ollama_transport()
    ):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "age             WARN" in result.output
    assert "lacks Apache AGE" in result.output


def test_doctor_age_warns_when_graph_probe_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any", test_db: psycopg.Connection
) -> None:
    """A ``psycopg.Error`` from the ``ag_catalog.ag_graph`` probe → WARN, exit 0.

    Extensions are present and ``LOAD 'age'`` runs for real (``test_db``
    guarantees ``age`` is installed); only the graph-catalog probe is patched to
    raise, exercising the graph-probe-failure branch.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    boom = psycopg.OperationalError("simulated ag_graph probe failure")
    with patch(
        "brain.cli._age_graph_present", side_effect=boom
    ), _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "age             WARN" in result.output
    assert "graph catalog probe failed" in result.output


def test_check_age_rolls_back_after_load_failure(
    test_db: psycopg.Connection,
) -> None:
    """``_check_age`` clears an aborted txn left by a LOAD failure (isolation).

    Guards the ``AgeBootstrapError`` handler's rollback: ``load_age`` is mocked
    to poison the transaction (run a bad statement, swallow the error WITHOUT
    rolling back) then raise — the exact "aborted txn left behind" case. After
    ``_check_age`` returns (WARN, never raising), a follow-up query on the SAME
    connection must succeed, proving the AGE failure cannot poison a later
    doctor check.

    Runs on a non-autocommit connection (where an aborted txn is observable);
    ``test_db`` guarantees the ``age`` extension is present so the probe reaches
    ``load_age``.
    """

    def _poison_then_raise(conn_arg: psycopg.Connection) -> bool:
        # Abort the transaction and deliberately do NOT roll back, mimicking a
        # failure path that leaves the connection unusable.
        with contextlib.suppress(psycopg.Error):
            conn_arg.execute("SELECT * FROM _g05_nonexistent_table")
        raise AgeBootstrapError("simulated LOAD failure leaving an aborted txn")

    with connect(TEST_DATABASE_URL) as conn:
        assert conn.autocommit is False
        with patch("brain.cli.load_age", side_effect=_poison_then_raise):
            _check_age(conn)  # must WARN + roll back, never raise
        # The aborted txn was cleared — a follow-up query succeeds.
        row = conn.execute("SELECT 1").fetchone()
        assert row == (1,)


# ---------------------------------------------------------------------------
# Graph drift line (wave G2-h) — relational↔AGE entity/edge parity. Gated on
# BRAIN_GRAPH_ENABLED; soft WARN that never flips doctor's exit code. Built from
# the synthetic person triangle reused from test_cli_graphrag_search.
# ---------------------------------------------------------------------------


def test_doctor_graph_drift_ok_when_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any", test_db: psycopg.Connection
) -> None:
    """A consistent relational + AGE mirror prints ``graph drift     OK`` counts."""
    from tests.test_cli_graphrag_search import _build, _seed_triangle

    _seed_triangle(test_db)
    _build(test_db)  # 3 person entities + 3 CO_OCCURS edges in 'default'
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "true")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert (
        "graph drift     OK (entities rel=3 age=3, co_occurs rel=3 age=3, "
        "tenant 'default')" in result.output
    )


def test_doctor_graph_drift_detected_when_mirror_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any", test_db: psycopg.Connection
) -> None:
    """Relational rows present but AGE mirror dropped → drift WARN + rebuild hint."""
    from brain.graph_rag.backends import AgeBackend
    from tests.test_cli_graphrag_search import _build, _seed_triangle

    _seed_triangle(test_db)
    _build(test_db)
    # Drop the AGE mirror (relational source-of-truth + watermark intact).
    AgeBackend().drop_graph(test_db, "default")

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "true")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    # Soft check: drift never flips the exit code.
    assert result.exit_code == 0, result.output
    assert "graph drift     drift detected" in result.output
    assert "entities rel=3 age=0" in result.output
    assert "co_occurs rel=3 age=0" in result.output
    assert "graphrag build --force" in result.output


def test_doctor_no_graph_drift_line_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any", test_db: psycopg.Connection
) -> None:
    """With BRAIN_GRAPH_ENABLED off, doctor emits no ``graph drift`` line."""
    from tests.test_cli_graphrag_search import _build, _seed_triangle

    _seed_triangle(test_db)
    _build(test_db)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    # Graph forced disabled (default is now ON after 2026-05-26 flip).
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "false")
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "graph drift" not in result.output


# ---------------------------------------------------------------------------
# Community line (wave G3-g) — graph_communities/members counts + a stale
# source_graph_hash check (stored fingerprint vs the recomputed current graph
# hash). Gated on BRAIN_GRAPH_ENABLED + AGE; soft WARN that never flips the exit
# code. Reuses the two-triangle community corpus from test_cli_graphrag_search.
# ---------------------------------------------------------------------------


def _build_default_communities(test_db: psycopg.Connection) -> None:
    """Seed the two-triangle corpus + AGE graph and build its communities.

    The corpus yields exactly two size-3 communities (six members) in tenant
    ``default``; the AGE graph is bootstrapped so the sibling ``graph drift``
    check probes cleanly. Summaries are NOT needed — the community line reads
    only counts + the stored ``source_graph_hash`` fingerprint.
    """
    from brain.config import Config
    from brain.graph_rag.communities import build_communities
    from tests.test_cli_graphrag_search import _seed_communities_corpus

    _seed_communities_corpus(test_db)
    bootstrap_age(test_db)
    cfg = Config(database_url=TEST_DATABASE_URL, graph_tenant_id="default")
    result = build_communities(test_db, cfg, tenant="default", force=True)
    assert result.communities_total == 2  # two triangles → two communities


def test_doctor_community_counts_ok_when_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any", test_db: psycopg.Connection
) -> None:
    """Built + fingerprint-current communities print the ``communities OK`` line."""
    _build_default_communities(test_db)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "true")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert (
        "communities     OK (2 communities, 6 members, tenant 'default', "
        "fingerprint current)" in result.output
    )


def test_doctor_community_stale_when_graph_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any", test_db: psycopg.Connection
) -> None:
    """Mutating the live graph after a build → ``stale`` WARN + refresh hint.

    Deleting the weak bridge edge changes the recomputed ``source_graph_hash``
    while the stored community fingerprint stays put — exactly the staleness the
    check exists to surface. Soft check: it never flips doctor's exit code.
    """
    _build_default_communities(test_db)
    # Drop the weak bridge so the recomputed graph hash diverges from the stored
    # community fingerprint (counts are unchanged — communities aren't rebuilt).
    test_db.execute(
        "DELETE FROM graph_relationships "
        "WHERE tenant_id = 'default' AND weight < 0.1"
    )
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "true")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert (
        "communities     stale (2 communities, 6 members, tenant 'default', "
        "fingerprint stale)" in result.output
    )
    assert "graphrag communities refresh" in result.output


def test_doctor_community_none_built_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any", test_db: psycopg.Connection
) -> None:
    """Graph enabled + AGE present but no communities → ``none built`` hint, exit 0."""
    from tests.test_cli_graphrag_search import _seed_communities_corpus

    # Seed the relational graph but never build communities.
    _seed_communities_corpus(test_db)
    bootstrap_age(test_db)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "true")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "communities     OK (0 communities, 0 members, tenant 'default')" in (
        result.output
    )
    assert "none built" in result.output
    assert "graphrag communities build" in result.output


def test_doctor_no_community_line_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Any", test_db: psycopg.Connection
) -> None:
    """With BRAIN_GRAPH_ENABLED off, doctor emits no ``communities`` line."""
    _build_default_communities(test_db)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    # Graph forced disabled (default is now ON after 2026-05-26 flip).
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "false")
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "communities" not in result.output
