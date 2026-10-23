"""``brain recall`` falls back to text-only when the query embed fails (RM-01).

The notice goes to stderr (class-only), the context block to stdout, and the
exit code stays 0. ``--json`` carries the same four ``fallback*`` keys
``brain search --json --meta`` emits. All data is synthetic.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document
from tests.embed_down import SECRET_DETAIL, QueryEmbedDownEmbedder

_NOTICE = "text-only (FTS) matches"


def _seed(conn: psycopg.Connection[Any], embedder: Any, count: int = 3) -> None:
    """Ingest ``count`` synthetic notes that all match the word 'meadow'."""
    for i in range(count):
        ingest_document(
            conn,
            embedder=embedder,
            doc=ExtractedDoc(
                title=f"Meadow note {i}",
                content=f"The meadow survey counted the wildflower plots {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:meadow-{i}",
        )


@pytest.fixture
def seeded(test_db: psycopg.Connection[Any], fake_embedder: Any) -> Any:
    _seed(test_db, fake_embedder)
    return fake_embedder


def test_transient_failure_prints_passages_and_a_stderr_notice(
    seeded: Any, patch_embedder: Any
) -> None:
    """REGRESSION: an unreachable embedder used to traceback out of ``brain recall``."""
    # Arrange
    down = QueryEmbedDownEmbedder(seeded, transient=True)
    patch_embedder(down)

    # Act
    result = CliRunner().invoke(app, ["recall", "meadow"])

    # Assert
    assert result.exit_code == 0, result.output
    assert result.exception is None
    header = result.stdout.splitlines()[0]
    assert header.startswith("# recall: meadow")
    assert header.endswith(", text-only)")
    assert "Meadow note" in result.stdout
    assert _NOTICE in result.stderr
    assert "comes back when the embedder does" in result.stderr
    assert "OllamaEmbedError" in result.stderr
    assert _NOTICE not in result.stdout
    assert SECRET_DETAIL not in result.output
    assert down.query_calls == 1


def test_permanent_failure_notice_names_configuration_and_doctor(
    seeded: Any, patch_embedder: Any
) -> None:
    # Arrange
    patch_embedder(QueryEmbedDownEmbedder(seeded, transient=False))

    # Act
    result = CliRunner().invoke(app, ["recall", "meadow"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "configuration problem" in result.stderr
    assert "brain doctor" in result.stderr
    assert "Meadow note" in result.stdout


def test_json_carries_the_four_keys_on_a_fallback(
    seeded: Any, patch_embedder: Any
) -> None:
    # Arrange
    patch_embedder(QueryEmbedDownEmbedder(seeded, transient=True))

    # Act
    result = CliRunner().invoke(app, ["recall", "meadow", "--json"])

    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["fallback"] == "fts_only"
    assert payload["fallback_reason"] == "OllamaEmbedError"
    assert payload["fallback_transient"] is True
    assert _NOTICE in payload["fallback_note"]
    assert len(payload["passages"]) == 3
    assert _NOTICE in result.stderr
    assert SECRET_DETAIL not in result.output


def test_json_carries_the_four_keys_as_null_on_a_normal_run(
    seeded: Any, patch_embedder: Any
) -> None:
    # Arrange
    patch_embedder(seeded)

    # Act
    result = CliRunner().invoke(app, ["recall", "meadow", "--json"])

    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["fallback"] is None
    assert payload["fallback_reason"] is None
    assert payload["fallback_transient"] is None
    assert payload["fallback_note"] is None
    assert _NOTICE not in result.stderr


def test_healthy_block_header_has_no_marker(seeded: Any, patch_embedder: Any) -> None:
    # Arrange
    patch_embedder(seeded)

    # Act
    result = CliRunner().invoke(app, ["recall", "meadow"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "text-only" not in result.stdout.splitlines()[0]
    assert result.stderr == ""
