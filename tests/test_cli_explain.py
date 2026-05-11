"""Tests for the `brain explain` CLI command."""

import json
import os

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    *,
    n_docs: int = 1,
) -> None:
    """Wire the test DB + fake embedder and seed n_docs documents."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    for i in range(n_docs):
        ingest_document(
            test_db,
            embedder=fake_embedder,  # type: ignore[arg-type]
            doc=ExtractedDoc(
                title=f"Explain Doc {i}",
                content=f"explain test document number {i} with company-id content",
                content_type="txt",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
        )


# ---------------------------------------------------------------------------
# Happy path — table output
# ---------------------------------------------------------------------------


def test_brain_explain_runs_and_prints_table(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """brain explain runs successfully and prints a Rich table."""
    _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["explain", "company-id"])
    assert result.exit_code == 0, result.output
    # Table headers should be present.
    assert "FTS#" in result.output
    assert "Vec#" in result.output
    assert "Final" in result.output


def test_brain_explain_verbose_shows_filters_column(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """--verbose flag adds a Filters column to the table."""
    _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["explain", "company-id", "--verbose"])
    assert result.exit_code == 0, result.output
    # Rich may truncate "Filters" to "Filt…" in narrow terminals — check prefix.
    assert "Filt" in result.output


def test_brain_explain_default_table_hides_filters_column(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Without --verbose the Filters column must NOT appear."""
    _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["explain", "company-id"])
    assert result.exit_code == 0, result.output
    # "Filt" covers both "Filters" and Rich's truncated "Filt…".
    assert "Filt" not in result.output


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def test_brain_explain_json_payload_shape(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """brain explain --json emits valid JSON with the explain field populated."""
    _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["explain", "company-id", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) >= 1
    first = payload[0]
    assert "id" in first
    assert "title" in first
    ex = first.get("explain")
    assert ex is not None, "explain field is missing from JSON output"
    # Check expected explain fields are present.
    for field in (
        "fts_rank",
        "fts_rrf_contribution",
        "vector_rrf_contribution",
        "rrf_score",
        "recency_boost",
        "final_score",
        "best_chunk_id",
        "best_chunk_index",
        "matched_filters",
        "reranker_score",
    ):
        assert field in ex, f"explain.{field} missing from JSON output"


# ---------------------------------------------------------------------------
# Source filter passthrough
# ---------------------------------------------------------------------------


def test_brain_explain_passes_source_filter_through(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """--source krisp filters to krisp documents only."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    # Seed one krisp and one manual doc.
    ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title="Krisp Meeting",
            content="krisp meeting notes agenda minutes",
            content_type="transcript",
            source_path=None,
            metadata={},
        ),
        source_kind="krisp",
        source_external_id="krisp:explain-filter-test",
    )
    ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title="Manual Note",
            content="krisp notes in a manual document",
            content_type="txt",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    result = CliRunner().invoke(app, ["explain", "krisp", "--source", "krisp", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    for item in payload:
        assert item["source_kind"] == "krisp", f"unexpected source_kind: {item['source_kind']}"


# ---------------------------------------------------------------------------
# Limit flag
# ---------------------------------------------------------------------------


def test_brain_explain_default_limit_is_10(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Default limit is 10 results (wider than brain search's 5)."""
    _setup(monkeypatch, test_db, fake_embedder, n_docs=15)
    result = CliRunner().invoke(app, ["explain", "explain test", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) <= 10


def test_brain_explain_respects_limit_flag(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """--limit 3 returns at most 3 results."""
    _setup(monkeypatch, test_db, fake_embedder, n_docs=10)
    result = CliRunner().invoke(app, ["explain", "explain test", "--limit", "3", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) <= 3


# ---------------------------------------------------------------------------
# Empty results
# ---------------------------------------------------------------------------


def test_brain_explain_empty_results_message(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """A query that matches nothing prints '(no results)' and exits 0."""
    _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(
        app,
        # Use --fts-only with a guaranteed-absent term so the fake embedder's
        # vector leg (which always matches) doesn't produce results.
        ["explain", "xyzzy-no-match-unique-99999", "--fts-only"],
    )
    assert result.exit_code == 0, result.output
    assert "(no results)" in result.output
