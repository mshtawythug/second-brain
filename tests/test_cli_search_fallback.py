"""``brain search`` / ``brain explain`` fall back to text-only when the embed fails.

Plan: ``docs/plans/2026-10-23-search-fts-fallback.md``. The notice goes to
stderr (class-only), results to stdout, and the exit code stays 0. All data is
synthetic.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document
from tests.embed_down import SECRET_DETAIL, BrokenEmbedder, QueryEmbedDownEmbedder

_NOTICE = "text-only (FTS) matches"


def _seed(conn: psycopg.Connection[Any], embedder: Any, count: int = 3) -> None:
    """Ingest ``count`` synthetic notes that all match the word 'harbour'."""
    for i in range(count):
        ingest_document(
            conn,
            embedder=embedder,
            doc=ExtractedDoc(
                title=f"Harbour note {i}",
                content=f"The harbour master reviewed the mooring plan {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:harbour-{i}",
            tags=["port"],
        )


@pytest.fixture
def down_embedder(
    test_db: psycopg.Connection[Any], fake_embedder: Any, patch_embedder: Any
) -> QueryEmbedDownEmbedder:
    """Seed the corpus, then install an embedder whose query embeds fail."""
    _seed(test_db, fake_embedder)
    down = QueryEmbedDownEmbedder(fake_embedder, transient=True)
    patch_embedder(down)
    return down


def test_search_table_falls_back_with_a_stderr_notice(
    down_embedder: QueryEmbedDownEmbedder,
) -> None:
    """REGRESSION: an unreachable embedder used to fail ``brain search``."""
    # Act
    result = CliRunner().invoke(app, ["search", "harbour", "--no-meta"])

    # Assert
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "Harbour note" in result.stdout
    assert _NOTICE in result.stderr
    assert "OllamaEmbedError" in result.stderr
    assert _NOTICE not in result.stdout
    assert SECRET_DETAIL not in result.output
    assert down_embedder.query_calls == 1


def test_search_bare_json_keeps_its_list_shape(
    down_embedder: QueryEmbedDownEmbedder,  # noqa: ARG001
) -> None:
    # Act
    result = CliRunner().invoke(app, ["search", "harbour", "--json"])

    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert {row["title"] for row in payload} == {
        "Harbour note 0", "Harbour note 1", "Harbour note 2",
    }
    assert _NOTICE in result.stderr
    assert SECRET_DETAIL not in result.output


def test_search_json_meta_envelope_carries_the_fallback_field(
    down_embedder: QueryEmbedDownEmbedder,  # noqa: ARG001
) -> None:
    # Act
    result = CliRunner().invoke(app, ["search", "harbour", "--json", "--meta"])

    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["fallback"] == "fts_only"
    assert payload["fallback_reason"] == "OllamaEmbedError"
    assert payload["fallback_transient"] is True
    assert _NOTICE in payload["fallback_note"]
    assert payload["fts_only"] is True
    assert payload["timing_ms"]["embed"] is None
    assert len(payload["results"]) == 3
    assert list(payload)[-1] == "results"
    assert SECRET_DETAIL not in result.output


def test_permanent_failure_notice_names_a_configuration_problem(
    test_db: psycopg.Connection[Any], fake_embedder: Any, patch_embedder: Any
) -> None:
    # Arrange
    _seed(test_db, fake_embedder)
    patch_embedder(QueryEmbedDownEmbedder(fake_embedder, transient=False))

    # Act
    result = CliRunner().invoke(app, ["search", "harbour", "--json", "--meta"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "brain doctor" in result.stderr
    assert "retry will not fix" in result.stderr
    payload = json.loads(result.stdout)
    assert payload["fallback_transient"] is False
    assert len(payload["results"]) == 3


def test_healthy_search_has_no_notice_and_null_fallback(
    test_db: psycopg.Connection[Any], fake_embedder: Any, patch_embedder: Any
) -> None:
    # Arrange
    _seed(test_db, fake_embedder)
    patch_embedder(fake_embedder)

    # Act
    result = CliRunner().invoke(app, ["search", "harbour", "--json", "--meta"])

    # Assert
    assert result.exit_code == 0, result.output
    assert _NOTICE not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["fallback"] is None
    assert payload["fallback_reason"] is None
    assert payload["fallback_transient"] is None
    assert payload["fallback_note"] is None
    assert payload["fts_only"] is False


def test_explain_falls_back_and_matched_filters_say_fts_only(
    down_embedder: QueryEmbedDownEmbedder,  # noqa: ARG001
) -> None:
    # Act
    result = CliRunner().invoke(app, ["explain", "harbour", "--json"])

    # Assert
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 3
    for row in rows:
        assert row["explain"]["matched_filters"]["fts_only"] is True
        assert row["explain"]["vector_rank"] is None
    assert _NOTICE in result.stderr
    assert SECRET_DETAIL not in result.output


def test_explain_table_falls_back(
    down_embedder: QueryEmbedDownEmbedder,  # noqa: ARG001
) -> None:
    # Act
    result = CliRunner().invoke(app, ["explain", "harbour"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "Explain results" in result.stdout
    assert "(no results)" not in result.stdout
    assert _NOTICE in result.stderr


def test_a_non_embed_error_still_fails(
    test_db: psycopg.Connection[Any], fake_embedder: Any, patch_embedder: Any
) -> None:
    # Arrange
    _seed(test_db, fake_embedder)
    patch_embedder(BrokenEmbedder(fake_embedder, transient=True))

    # Act
    result = CliRunner().invoke(app, ["search", "harbour"])

    # Assert
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert _NOTICE not in result.stderr
