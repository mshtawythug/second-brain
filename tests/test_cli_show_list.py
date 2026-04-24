"""Tests for the `brain show` and `brain list` CLI commands."""
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
    content: str = "alpha bravo",
    tags: list[str] | None = None,
    source_kind: str = "manual",
    source_external_id: str | None = None,
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
        source_kind=source_kind,
        source_external_id=source_external_id,
        tags=tags,
    )
    assert result.document_id is not None
    return result.document_id


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")


def test_show_full_text(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _set_env(monkeypatch)
    doc_id = _seed(test_db, fake_embedder)
    result = CliRunner().invoke(app, ["show", doc_id[:8]])
    assert result.exit_code == 0, result.output
    assert "alpha bravo" in result.output


def test_show_unknown_id_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    _set_env(monkeypatch)
    result = CliRunner().invoke(app, ["show", "00000000"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_show_short_prefix_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    _set_env(monkeypatch)
    result = CliRunner().invoke(app, ["show", "abc"])
    assert result.exit_code != 0
    assert "6 characters" in result.output.lower()


def test_show_ambiguous_prefix_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    _set_env(monkeypatch)
    # Craft two documents with UUIDs sharing an 8-character prefix to force
    # the ambiguous branch deterministically (random UUIDs would not collide).
    shared_prefix = "abcdef12"
    id_a = f"{shared_prefix}-0000-4000-8000-000000000001"
    id_b = f"{shared_prefix}-0000-4000-8000-000000000002"
    for doc_id, title, content_hash in [
        (id_a, "Doc A", "hash-a"),
        (id_b, "Doc B", "hash-b"),
    ]:
        test_db.execute(
            """
            INSERT INTO documents (id, title, content, content_hash, content_type)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (doc_id, title, "body", content_hash, "txt"),
        )
    result = CliRunner().invoke(app, ["show", shared_prefix])
    assert result.exit_code != 0
    assert "ambiguous" in result.output.lower()


def test_show_json_output(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _set_env(monkeypatch)
    doc_id = _seed(
        test_db,
        fake_embedder,
        title="Title",
        content="body text",
        tags=["x", "y"],
    )
    result = CliRunner().invoke(app, ["show", doc_id[:8], "--json"])
    assert result.exit_code == 0, result.output
    # Rich's print_json may format across lines; check that key fields appear.
    assert "Title" in result.stdout
    assert "body text" in result.stdout
    assert doc_id in result.stdout


def test_list_returns_titles(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _set_env(monkeypatch)
    _seed(test_db, fake_embedder)
    result = CliRunner().invoke(app, ["list"])
    assert result.exit_code == 0, result.output
    assert "A" in result.output


def test_list_source_filter(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _set_env(monkeypatch)
    _seed(test_db, fake_embedder, title="Manual doc", content="m-content")
    _seed(
        test_db,
        fake_embedder,
        title="Krisp doc",
        content="k-content",
        source_kind="krisp",
        source_external_id="kk-1",
    )
    result = CliRunner().invoke(app, ["list", "--source", "krisp"])
    assert result.exit_code == 0, result.output
    assert "Krisp doc" in result.output
    assert "Manual doc" not in result.output


def test_list_tag_filter(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _set_env(monkeypatch)
    _seed(test_db, fake_embedder, title="Tagged", content="t-content", tags=["work"])
    _seed(test_db, fake_embedder, title="Untagged", content="u-content", tags=[])
    result = CliRunner().invoke(app, ["list", "--tag", "work"])
    assert result.exit_code == 0, result.output
    assert "Tagged" in result.output
    assert "Untagged" not in result.output


def test_list_limit(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _set_env(monkeypatch)
    for i in range(5):
        _seed(test_db, fake_embedder, title=f"doc {i}", content=f"body-{i}")
    result = CliRunner().invoke(app, ["list", "--limit", "2"])
    assert result.exit_code == 0, result.output
    # Count non-blank output lines; each doc is one line.
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 2


def test_list_json_output(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _set_env(monkeypatch)
    _seed(test_db, fake_embedder, title="JsonDoc", content="j-content", tags=["x"])
    result = CliRunner().invoke(app, ["list", "--json"])
    assert result.exit_code == 0, result.output
    assert "JsonDoc" in result.stdout
    assert "x" in result.stdout
