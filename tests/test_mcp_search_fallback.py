"""MCP ``brain_search`` falls back to text-only when the query embed fails.

Plan: ``docs/plans/2026-10-23-search-fts-fallback.md``. The response carries
four always-present ``fallback*`` keys (``null`` when no fallback happened).
The wire never carries the exception message. All data is synthetic.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from brain.config import Config
from brain.ingest import ExtractedDoc, ingest_document
from tests.embed_down import SECRET_DETAIL, BrokenEmbedder, QueryEmbedDownEmbedder

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)
_FALLBACK_KEYS = {"fallback", "fallback_reason", "fallback_transient", "fallback_note"}


def _seed(conn: psycopg.Connection[Any], embedder: Any, count: int = 3) -> None:
    """Ingest ``count`` synthetic notes that all match the word 'glacier'."""
    for i in range(count):
        ingest_document(
            conn,
            embedder=embedder,
            doc=ExtractedDoc(
                title=f"Glacier note {i}",
                content=f"The glacier survey measured the ice front {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:glacier-{i}",
            tags=["field"],
        )


def _install(monkeypatch: pytest.MonkeyPatch, embedder: Any) -> None:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL), embedder=embedder
    )
    monkeypatch.setattr(mcp_server, "_state", state)


@pytest.fixture
def seeded(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> Iterator[Any]:
    _seed(test_db, fake_embedder)
    yield fake_embedder


@pytest.mark.parametrize("transient", [True, False])
def test_brain_search_falls_back_and_says_so(
    monkeypatch: pytest.MonkeyPatch, seeded: Any, transient: bool
) -> None:
    """REGRESSION: an unreachable embedder used to return INTERNAL_ERROR."""
    # Arrange
    _install(monkeypatch, QueryEmbedDownEmbedder(seeded, transient=transient))

    # Act
    payload = mcp_server.brain_search(query="glacier")

    # Assert
    assert {r["title"] for r in payload["results"]} == {
        "Glacier note 0", "Glacier note 1", "Glacier note 2",
    }
    assert payload["fallback"] == "fts_only"
    assert payload["fallback_reason"] == "OllamaEmbedError"
    assert payload["fallback_transient"] is transient
    assert "text-only" in payload["fallback_note"]
    assert payload["fts_only"] is True
    assert payload["total_documents"] == 3
    assert SECRET_DETAIL not in json.dumps(payload)


def test_permanent_failure_note_points_at_brain_doctor(
    monkeypatch: pytest.MonkeyPatch, seeded: Any
) -> None:
    # Arrange
    _install(monkeypatch, QueryEmbedDownEmbedder(seeded, transient=False))

    # Act
    payload = mcp_server.brain_search(query="glacier")

    # Assert
    assert "brain doctor" in payload["fallback_note"]


def test_healthy_brain_search_carries_null_fallback_keys(
    monkeypatch: pytest.MonkeyPatch, seeded: Any
) -> None:
    # Arrange
    _install(monkeypatch, seeded)

    # Act
    payload = mcp_server.brain_search(query="glacier")

    # Assert
    assert set(payload) >= _FALLBACK_KEYS
    assert all(payload[k] is None for k in _FALLBACK_KEYS)
    assert payload["fts_only"] is False
    assert payload["results"]


def test_fallback_keys_do_not_leak_into_result_objects(
    monkeypatch: pytest.MonkeyPatch, seeded: Any
) -> None:
    # Arrange
    _install(monkeypatch, QueryEmbedDownEmbedder(seeded, transient=True))

    # Act
    payload = mcp_server.brain_search(query="glacier")

    # Assert
    for row in payload["results"]:
        assert set(row) == {
            "id", "title", "source_kind", "snippet", "score",
            "content_type", "tags",
        }


def test_a_non_embed_error_still_fails(
    monkeypatch: pytest.MonkeyPatch, seeded: Any
) -> None:
    # Arrange
    _install(monkeypatch, BrokenEmbedder(seeded, transient=True))

    # Act / Assert
    with pytest.raises(RuntimeError):
        mcp_server.brain_search(query="glacier")
