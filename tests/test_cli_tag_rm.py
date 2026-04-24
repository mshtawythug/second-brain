"""Tests for the `brain tag` and `brain rm` CLI commands."""
import os
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _seed(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    *,
    title: str = "A",
    content: str = "x",
    tags: list[str] | None = None,
) -> str:
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="txt",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=tags if tags is not None else ["one"],
    )
    assert result.document_id is not None
    return result.document_id


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")


def _tags_for(doc_id: str) -> list[str]:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT tags FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
    assert row is not None
    return list(row[0] or [])


def _doc_count(doc_id: str) -> int:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT count(*) FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_tag_add_and_remove(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _set_env(monkeypatch)
    doc_id = _seed(test_db, fake_embedder)
    result = CliRunner().invoke(app, ["tag", doc_id[:8], "+two", "-one"])
    assert result.exit_code == 0, result.output
    assert sorted(_tags_for(doc_id)) == ["two"]


def test_tag_requires_plus_or_minus(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _set_env(monkeypatch)
    doc_id = _seed(test_db, fake_embedder)
    # Pass a mod without +/- — should fail with a BadParameter.
    result = CliRunner().invoke(app, ["tag", doc_id[:8], "plain"])
    assert result.exit_code != 0
    assert "+tag" in result.output or "-tag" in result.output


def test_tag_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _set_env(monkeypatch)
    doc_id = _seed(test_db, fake_embedder, tags=["one"])
    # Adding an existing tag should not create a duplicate.
    result = CliRunner().invoke(app, ["tag", doc_id[:8], "+one"])
    assert result.exit_code == 0, result.output
    assert sorted(_tags_for(doc_id)) == ["one"]


def test_rm_deletes_document_and_chunks(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _set_env(monkeypatch)
    doc_id = _seed(test_db, fake_embedder)
    result = CliRunner().invoke(app, ["rm", doc_id[:8]], input="y\n")
    assert result.exit_code == 0, result.output
    assert _doc_count(doc_id) == 0
    # Chunks cascade-delete with the parent document.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        chunk_row = conn.execute(
            "SELECT count(*) FROM chunks WHERE document_id=%s", (doc_id,)
        ).fetchone()
    assert chunk_row is not None
    assert chunk_row[0] == 0


def test_rm_yes_flag_skips_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _set_env(monkeypatch)
    doc_id = _seed(test_db, fake_embedder)
    # No stdin input provided — must succeed because --yes bypasses the prompt.
    result = CliRunner().invoke(app, ["rm", doc_id[:8], "--yes"])
    assert result.exit_code == 0, result.output
    assert _doc_count(doc_id) == 0


def test_rm_abort_preserves_document(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _set_env(monkeypatch)
    doc_id = _seed(test_db, fake_embedder)
    result = CliRunner().invoke(app, ["rm", doc_id[:8]], input="n\n")
    # typer.confirm(abort=True) exits non-zero when the user declines.
    assert result.exit_code != 0
    assert _doc_count(doc_id) == 1
