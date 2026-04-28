"""Tests for the `brain init` CLI command."""
import os
from unittest.mock import patch

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


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


def _wipe_db() -> None:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


def test_init_default_arctic_produces_1024_column(
    monkeypatch: pytest.MonkeyPatch,
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
