"""CLI surface for F9: ``--updated-after`` / ``--updated-before``.

``brain search`` and ``brain explain`` both carry the pair, and both accept
the same two date formats as ``--after`` / ``--before``. The help text has to
spell out the distinction between the two pairs — a user who reaches for
``--after`` expecting "notes I edited since" gets silently wrong answers on
any corpus containing email or transcripts, because ``coalesce(sent_at,
ingested_at)`` prefers ``sent_at``.

All documents are synthetic.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

_EDITED_RECENTLY = datetime(2026, 7, 20, 9, 0, 0, tzinfo=UTC)
_EDITED_LONG_AGO = datetime(2024, 2, 1, 9, 0, 0, tzinfo=UTC)


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    fake_embedder: object,
) -> None:
    """Seed one recently-edited and one long-untouched synthetic document."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    for title, updated_at in (
        ("Touched last week", _EDITED_RECENTLY),
        ("Untouched for years", _EDITED_LONG_AGO),
    ):
        result = ingest_document(
            test_db,
            embedder=fake_embedder,  # type: ignore[arg-type]
            doc=ExtractedDoc(
                title=title,
                content=f"{title}: shared probe term",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:{title}",
        )
        assert result.document_id is not None
        test_db.execute(
            "UPDATE documents SET updated_at = %s WHERE id = %s",
            (updated_at, result.document_id),
        )


def test_search_updated_after_filters_results(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    fake_embedder: object,
) -> None:
    """RED-FIRST: ``--updated-after`` does not exist yet (Typer exits 2)."""
    # Arrange
    _setup(monkeypatch, test_db, fake_embedder)

    # Act
    result = CliRunner().invoke(
        app, ["search", "probe", "--updated-after", "2026-07-01", "--json"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    titles = {row["title"] for row in json.loads(result.stdout)}
    assert titles == {"Touched last week"}


def test_search_updated_before_filters_results(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    fake_embedder: object,
) -> None:
    # Arrange
    _setup(monkeypatch, test_db, fake_embedder)

    # Act
    result = CliRunner().invoke(
        app, ["search", "probe", "--updated-before", "2026-07-01", "--json"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    titles = {row["title"] for row in json.loads(result.stdout)}
    assert titles == {"Untouched for years"}


def test_search_accepts_the_datetime_format_too(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    fake_embedder: object,
) -> None:
    """Same two ``formats`` as ``--after`` — date and date-time both parse."""
    # Arrange
    _setup(monkeypatch, test_db, fake_embedder)

    # Act
    result = CliRunner().invoke(
        app,
        ["search", "probe", "--updated-after", "2026-07-01T00:00:00", "--json"],
    )

    # Assert
    assert result.exit_code == 0, result.output
    titles = {row["title"] for row in json.loads(result.stdout)}
    assert titles == {"Touched last week"}


def test_explain_carries_the_pair_for_parity(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    fake_embedder: object,
) -> None:
    """``brain explain`` exposes every filter ``brain search`` does."""
    # Arrange
    _setup(monkeypatch, test_db, fake_embedder)

    # Act
    result = CliRunner().invoke(
        app, ["explain", "probe", "--updated-after", "2026-07-01", "--json"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert {row["title"] for row in payload} == {"Touched last week"}
    matched = payload[0]["explain"]["matched_filters"]
    assert matched["updated_after"] == "2026-07-01T00:00:00"


@pytest.mark.parametrize("command", ["search", "explain"])
def test_help_states_the_after_vs_updated_after_distinction(command: str) -> None:
    """The help text must name the other pair, not just describe itself.

    Without this, ``--after`` and ``--updated-after`` read as synonyms and the
    user picks the wrong one.
    """
    # Arrange / Act
    result = CliRunner().invoke(app, [command, "--help"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "--updated-after" in result.output
    assert "--updated-before" in result.output
    assert "edited" in result.output.lower()
