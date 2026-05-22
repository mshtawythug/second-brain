"""Tests for the `brain init` CLI command."""
import os
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest
from typer.testing import CliRunner

from brain import config as config_module
from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture()
def isolated_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Block .env file sources so delenv tests aren't undone by T1.0 setdefault."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: tmp_path / "project.env")
    monkeypatch.setattr(
        config_module, "_brain_home_dotenv", lambda: tmp_path / "brain_home.env"
    )
    monkeypatch.delenv("BRAIN_HOME", raising=False)


def test_init_applies_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    # wipe schema first
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert "001_init.sql" in result.output

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='documents'"
        ).fetchone()
        assert row is not None
        assert row[0] == 1


def test_init_reports_no_migrations_when_none_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    with patch("brain.cli.run_migrations", return_value=[]):
        result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert "no migrations to apply" in result.output


# ---------------------------------------------------------------------------
# Phase 3.5: ``brain init`` reconciles ``chunks.embedding`` column with the
# active backend's native dim. One test per backend confirms the column is
# at the expected size after init.
# ---------------------------------------------------------------------------


def _column_dim() -> int:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        row = conn.execute(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
        ).fetchone()
    assert row is not None
    formatted = str(row[0])
    return int(formatted[len("vector(") : -1])


def _column_dim_of(table: str, column: str) -> int:
    """Generalized variant of :func:`_column_dim` for any ``<table>.<column>``."""
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        row = conn.execute(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = %s::regclass AND attname = %s",
            (table, column),
        ).fetchone()
    assert row is not None
    formatted = str(row[0])
    return int(formatted[len("vector(") : -1])


def _wipe_db() -> None:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


def test_init_default_arctic_produces_1024_column(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dotenv: None,
) -> None:
    """Default ``BRAIN_EMBEDDER=arctic`` → column resized to vector(1024)."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.delenv("BRAIN_EMBEDDER", raising=False)
    _wipe_db()

    result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert "arctic" in result.output
    assert "dim=1024" in result.output
    assert _column_dim() == 1024


def test_init_qwen3_produces_4096_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BRAIN_EMBEDDER=qwen3`` → column stays at vector(4096) (migration default)."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_EMBEDDER", "qwen3")
    _wipe_db()

    result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert "qwen3" in result.output
    assert "dim=4096" in result.output
    assert _column_dim() == 4096


# ---------------------------------------------------------------------------
# G0-2: ``brain init`` bootstraps the Apache AGE extension + ``brain_graph``
# idempotently after relational migrations.
# ---------------------------------------------------------------------------


def _brain_graph_exists() -> bool:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s", ("brain_graph",)
        ).fetchone()
    return row is not None


def _drop_brain_graph() -> None:
    """Drop ``brain_graph`` if present so a test can assert init *creates* it."""
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("LOAD 'age'")
        row = conn.execute(
            "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s", ("brain_graph",)
        ).fetchone()
        if row is not None:
            conn.execute("SELECT ag_catalog.drop_graph('brain_graph', true)")


def test_init_bootstraps_age_graph_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """init creates ``brain_graph`` on first run, no-ops (and never errors) after.

    Also exercises the create_graph existence guard end-to-end: a second init
    finds the graph present and reports it without raising.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    _drop_brain_graph()
    assert not _brain_graph_exists()

    first = CliRunner().invoke(app, ["init"])
    assert first.exit_code == 0, first.output
    assert "graph           brain_graph created (age)" in first.output
    assert _brain_graph_exists()

    # Second run: graph already present → guarded no-op, still exit 0.
    second = CliRunner().invoke(app, ["init"])
    assert second.exit_code == 0, second.output
    assert "graph           brain_graph present (age)" in second.output
    assert _brain_graph_exists()


# ---------------------------------------------------------------------------
# G0 wave-boundary fix #1/#2: init must not crash on a stock-pgvector DB (AGE
# not installable) and must reconcile graph_entities.embedding when AGE *is*
# available. The AGE test image makes AGE available, so the no-AGE path is
# simulated by stubbing the availability probe.
# ---------------------------------------------------------------------------


def test_init_reconciles_graph_entities_embedding_dim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix #2: init resizes graph_entities.embedding to the active embedder dim.

    On the AGE test image AGE is available, so init bootstraps the graph AND
    reconciles graph_entities.embedding (migration-012 default vector(1024)) to
    the embedder's native dim — mirroring the chunks.embedding reconcile. With
    qwen3 (4096) both columns must end at vector(4096).
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_EMBEDDER", "qwen3")
    _wipe_db()

    result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert _column_dim_of("graph_entities", "embedding") == 4096
    # chunks.embedding reconcile is unchanged.
    assert _column_dim() == 4096


def test_init_skips_age_bootstrap_when_age_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix #1: init succeeds (no crash) on a DB where AGE isn't installable.

    Simulates a stock-pgvector image (pre-cutover prod) by stubbing the
    availability probe to False. init must SKIP the AGE bootstrap AND the
    graph_entities reconcile, print a friendly skip line, and still exit 0.
    With qwen3 (4096): had the graph reconcile NOT been skipped,
    graph_entities.embedding would be 4096 — asserting it stays at the
    migration default vector(1024) proves the reconcile was gated with the
    bootstrap. chunks.embedding (not AGE-gated) is still resized to 4096.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_EMBEDDER", "qwen3")
    _wipe_db()

    with (
        patch("brain.cli.age_extension_available", return_value=False),
        patch("brain.cli.bootstrap_age") as mock_bootstrap,
    ):
        result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert "AGE not available" in result.output
    assert "skipped" in result.output
    # The bootstrap was gated out entirely.
    mock_bootstrap.assert_not_called()
    # graph_entities reconcile skipped with the bootstrap → stays at 1024.
    assert _column_dim_of("graph_entities", "embedding") == 1024
    # chunks.embedding reconcile is NOT AGE-gated → still resized to 4096.
    assert _column_dim() == 4096


def test_init_voyage_produces_1024_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BRAIN_EMBEDDER=voyage`` (with key) → column resized to vector(1024).

    Stubs ``make_embedder`` so the init path exercises
    ``ensure_embedding_column`` without instantiating a real Voyage SDK
    client (which would require network).
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_EMBEDDER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "vk-test")
    _wipe_db()

    class _FakeVoyageEmbedder:
        dim = 1024

        def embed(  # pragma: no cover - never called from init
            self, texts: list[str], *, input_type: str = "document"
        ) -> list[list[float]]:
            return [[0.0] * self.dim for _ in texts]

        def count_tokens(self, text: str) -> int:  # pragma: no cover
            return len(text)

    with patch("brain.cli.make_embedder", return_value=_FakeVoyageEmbedder()):
        result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert "voyage" in result.output
    assert "dim=1024" in result.output
    assert _column_dim() == 1024
