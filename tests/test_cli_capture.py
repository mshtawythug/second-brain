"""Integration tests for `brain capture` against the real Postgres test DB.

All content is synthetic. Captures run with ``--no-enrich`` so the suite never
depends on a live Ollama; one dedicated test asserts the ``--no-enrich``
summary-NULL contract explicitly.
"""
import os
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.config import ConfigError

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _patch_embedder(monkeypatch: pytest.MonkeyPatch, fake_embedder: object) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)


def _documents(conn: psycopg.Connection) -> list[tuple[str, list[str], str | None]]:
    return [
        (str(row[0]), list(row[1]), row[2])
        for row in conn.execute(
            "SELECT id, tags, summary FROM documents ORDER BY ingested_at"
        ).fetchall()
    ]


def test_capture_creates_inbox_document(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`brain capture --text` inserts one document tagged `inbox`."""
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = CliRunner().invoke(
        app, ["capture", "--text", "capture this thought about project-ko", "--no-enrich"]
    )

    assert result.exit_code == 0, result.output
    assert "captured" in result.output
    docs = _documents(test_db)
    assert len(docs) == 1
    assert docs[0][1] == ["inbox"]


def test_recapture_same_text_skips_and_preserves_inbox(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Re-capturing identical text is a content-hash no-op; inbox is preserved."""
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    text = "a repeated capture body"

    first = CliRunner().invoke(app, ["capture", "--text", text, "--no-enrich"])
    assert first.exit_code == 0, first.output
    before = _documents(test_db)
    assert len(before) == 1

    second = CliRunner().invoke(app, ["capture", "--text", text, "--no-enrich"])
    assert second.exit_code == 0, second.output
    assert "already captured" in second.output

    after = _documents(test_db)
    assert len(after) == 1
    assert after[0][0] == before[0][0]  # same UUID — no new row
    assert after[0][1] == ["inbox"]


def test_recapture_force_creates_new_uuid(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`--force` replaces the row under a new UUID, still tagged `inbox`."""
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    text = "forced re-capture body"

    CliRunner().invoke(app, ["capture", "--text", text, "--no-enrich"])
    before = _documents(test_db)
    assert len(before) == 1
    original_id = before[0][0]

    forced = CliRunner().invoke(
        app, ["capture", "--text", text, "--no-enrich", "--force"]
    )
    assert forced.exit_code == 0, forced.output

    after = _documents(test_db)
    assert len(after) == 1
    assert after[0][0] != original_id  # new UUID
    assert after[0][1] == ["inbox"]


def test_no_enrich_leaves_summary_null(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`--no-enrich` skips summarization, so `documents.summary` stays NULL."""
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = CliRunner().invoke(
        app, ["capture", "--text", "unenriched capture body", "--no-enrich"]
    )
    assert result.exit_code == 0, result.output

    docs = _documents(test_db)
    assert len(docs) == 1
    assert docs[0][2] is None


def test_extra_tags_union_with_inbox(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Extra `--tag` values are applied alongside the always-on `inbox` tag."""
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = CliRunner().invoke(
        app,
        ["capture", "--text", "tagged capture", "--no-enrich", "--tag", "idea"],
    )
    assert result.exit_code == 0, result.output

    docs = _documents(test_db)
    assert len(docs) == 1
    assert set(docs[0][1]) == {"inbox", "idea"}


def test_empty_text_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Whitespace-only content fails fast with exit code 1 and no row written."""
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = CliRunner().invoke(app, ["capture", "--text", "   ", "--no-enrich"])

    assert result.exit_code == 1
    assert "empty" in result.output.lower()
    assert _documents(test_db) == []


def test_inbox_warn_threshold_zero_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """An invalid BRAIN_CAPTURE_INBOX_WARN_THRESHOLD fails at config load."""
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_CAPTURE_INBOX_WARN_THRESHOLD", "0")

    result = CliRunner().invoke(
        app, ["capture", "--text", "anything", "--no-enrich"]
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)
