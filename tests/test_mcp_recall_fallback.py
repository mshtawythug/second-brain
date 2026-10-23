"""MCP ``brain_recall`` falls back to text-only when the query embed fails (RM-01).

The response carries the four always-present ``fallback*`` keys ``brain_search``
emits (``null`` when no fallback happened), never ``INTERNAL_ERROR`` for an
embed failure, and never the exception message. A database error still maps
exactly as before. All data is synthetic.
"""
from __future__ import annotations

import json
import os
from typing import Any

import psycopg
import pytest
from mcp.types import INTERNAL_ERROR

from brain import mcp_server
from brain.config import Config
from brain.ingest import ExtractedDoc, ingest_document
from brain.mcp_compat import MCPError
from brain.token_report import count_payload_tokens
from tests.embed_down import SECRET_DETAIL, QueryEmbedDownEmbedder

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)
_FALLBACK_KEYS = ("fallback", "fallback_reason", "fallback_transient", "fallback_note")


def _seed(conn: psycopg.Connection[Any], embedder: Any, count: int = 3) -> None:
    """Ingest ``count`` synthetic notes that all match the word 'estuary'."""
    for i in range(count):
        ingest_document(
            conn,
            embedder=embedder,
            doc=ExtractedDoc(
                title=f"Estuary note {i}",
                content=f"The estuary transect sampled the salt marsh {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:estuary-{i}",
        )


def _install(monkeypatch: pytest.MonkeyPatch, embedder: Any) -> None:
    state = mcp_server._State(
        cfg=Config(
            database_url=TEST_DATABASE_URL,
            recall_budget_tokens=2000,
            recall_passage_tokens=120,
            recall_max_candidates=25,
        ),
        embedder=embedder,
    )
    monkeypatch.setattr(mcp_server, "_state", state)


@pytest.fixture
def seeded(test_db: psycopg.Connection[Any], fake_embedder: Any) -> Any:
    _seed(test_db, fake_embedder)
    return fake_embedder


class _DatabaseDownEmbedder(QueryEmbedDownEmbedder):
    """A query embed that surfaces a ``psycopg`` error, not an embed one."""

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        if input_type == "query":
            raise psycopg.OperationalError(f"server closed {SECRET_DETAIL}")
        return self._inner.embed(texts, input_type=input_type)


@pytest.mark.parametrize("transient", [True, False])
def test_brain_recall_falls_back_and_says_so(
    monkeypatch: pytest.MonkeyPatch, seeded: Any, transient: bool
) -> None:
    """REGRESSION: an unreachable embedder used to return INTERNAL_ERROR."""
    # Arrange
    _install(monkeypatch, QueryEmbedDownEmbedder(seeded, transient=transient))

    # Act
    payload = mcp_server.brain_recall(query="estuary")

    # Assert
    assert {p["title"] for p in payload["passages"]} == {
        "Estuary note 0", "Estuary note 1", "Estuary note 2",
    }
    assert payload["fallback"] == "fts_only"
    assert payload["fallback_reason"] == "OllamaEmbedError"
    assert payload["fallback_transient"] is transient
    assert "text-only" in payload["fallback_note"]
    assert payload["context_block"].splitlines()[0].endswith(", text-only)")
    assert SECRET_DETAIL not in json.dumps(payload)


def test_permanent_failure_note_points_at_brain_doctor(
    monkeypatch: pytest.MonkeyPatch, seeded: Any
) -> None:
    # Arrange
    _install(monkeypatch, QueryEmbedDownEmbedder(seeded, transient=False))

    # Act
    payload = mcp_server.brain_recall(query="estuary")

    # Assert
    assert "brain doctor" in payload["fallback_note"]


def test_payload_tokens_still_counts_the_whole_payload(
    monkeypatch: pytest.MonkeyPatch, seeded: Any
) -> None:
    """The fallback keys are inside what ``payload_tokens`` prices."""
    # Arrange
    _install(monkeypatch, QueryEmbedDownEmbedder(seeded, transient=True))

    # Act
    payload = mcp_server.brain_recall(query="estuary")

    # Assert
    priced = {k: v for k, v in payload.items() if k != "payload_tokens"}
    assert set(_FALLBACK_KEYS) <= set(priced)
    assert payload["payload_tokens"] == count_payload_tokens(
        priced, cost=seeded.count_tokens
    )


def test_healthy_brain_recall_carries_null_fallback_keys(
    monkeypatch: pytest.MonkeyPatch, seeded: Any
) -> None:
    # Arrange
    _install(monkeypatch, seeded)

    # Act
    payload = mcp_server.brain_recall(query="estuary")

    # Assert
    assert all(payload[k] is None for k in _FALLBACK_KEYS)
    assert payload["passages"]
    assert "text-only" not in payload["context_block"].splitlines()[0]


def test_a_database_error_still_maps_to_the_db_error(
    monkeypatch: pytest.MonkeyPatch, seeded: Any
) -> None:
    # Arrange
    _install(monkeypatch, _DatabaseDownEmbedder(seeded, transient=True))

    # Act / Assert
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_recall(query="estuary")
    assert excinfo.value.error.code == INTERNAL_ERROR
    assert excinfo.value.error.message == "database error: OperationalError"
    assert SECRET_DETAIL not in excinfo.value.error.message
