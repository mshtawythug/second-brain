"""MCP ``brain_ask``: text-only retrieval when the embedder is down, clean errors (RM-02).

Criterion 7: on a fallback the payload carries the four ``fallback*`` keys
``brain_search`` emits; every ``ask`` failure family is ``INTERNAL_ERROR`` with
its fixed, class-only sentence; ``ValueError`` and ``psycopg.Error`` keep their
mappings. No error ever carries an exception message. All data is synthetic.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

from brain import chat, mcp_server
from brain.ask_errors import describe_ask_failure
from brain.config import Config
from brain.embeddings import OllamaEmbedError
from brain.errors import (
    ChatOutputTruncated,
    EnrichmentError,
    GraphBackendError,
    GraphTenantError,
    OllamaContextOverflow,
    OllamaUnavailable,
)
from brain.mcp_compat import MCPError
from tests.conftest import TEST_DATABASE_URL
from tests.embed_down import SECRET_DETAIL, QueryEmbedDownEmbedder

_FALLBACK_KEYS = ("fallback", "fallback_reason", "fallback_transient", "fallback_note")
#: Placed in every raised exception's message; it must never reach the wire.
_SENTINEL = "synthetic-ask-detail-91c2"


def _chat(prompt: str, *, schema: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    if "sub_queries" in schema:
        return {"sub_queries": ["meadow survey"]}
    if "sufficient" in schema:
        return {"sufficient": True, "follow_up_queries": []}
    return {"answer": "The survey counted wildflower plots [1]."}


def _install(monkeypatch: pytest.MonkeyPatch, embedder: Any, tmp_path: Path) -> None:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=embedder,
    )
    monkeypatch.setattr(mcp_server, "_state", state)


@pytest.fixture
def meadow(seed_doc: Callable[..., str]) -> str:
    return seed_doc(
        title="Meadow survey note",
        content="The meadow survey counted the wildflower plots by section.",
    )


@pytest.mark.parametrize("no_loop", [True, False], ids=["no-loop", "loop"])
def test_brain_ask_falls_back_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
    meadow: str,
    fake_embedder: Any,
    tmp_path: Path,
    no_loop: bool,
) -> None:
    """REGRESSION: an unreachable embedder used to escape ``brain_ask`` unmapped."""
    # Arrange
    _install(monkeypatch, QueryEmbedDownEmbedder(fake_embedder, transient=True), tmp_path)
    monkeypatch.setattr(chat, "chat_json", _chat)

    # Act
    payload = mcp_server.brain_ask(question="meadow survey", no_loop=no_loop)

    # Assert
    assert [c["document_id"] for c in payload["citations"]] == [meadow]
    assert payload["fallback"] == "fts_only"
    assert payload["fallback_reason"] == "OllamaEmbedError"
    assert payload["fallback_transient"] is True
    assert "text-only" in payload["fallback_note"]
    assert "fallback_used" in payload
    assert SECRET_DETAIL not in json.dumps(payload)


def test_healthy_brain_ask_carries_null_fallback_keys(
    monkeypatch: pytest.MonkeyPatch, meadow: str, fake_embedder: Any, tmp_path: Path
) -> None:
    # Arrange
    _install(monkeypatch, fake_embedder, tmp_path)
    monkeypatch.setattr(chat, "chat_json", _chat)

    # Act
    payload = mcp_server.brain_ask(question="meadow survey", no_loop=True)

    # Assert
    assert all(payload[key] is None for key in _FALLBACK_KEYS)
    assert payload["citations"]


@pytest.mark.parametrize(
    "exc",
    [
        OllamaUnavailable(_SENTINEL),
        OllamaContextOverflow(_SENTINEL),
        ChatOutputTruncated(_SENTINEL, partial=_SENTINEL, num_predict=512),
        EnrichmentError(_SENTINEL),
    ],
    ids=["unavailable", "context-overflow", "output-truncated", "enrichment"],
)
def test_each_chat_failure_is_internal_error_with_its_sentence(
    monkeypatch: pytest.MonkeyPatch,
    meadow: str,  # noqa: ARG001 — seeds the corpus
    fake_embedder: Any,
    tmp_path: Path,
    exc: BaseException,
) -> None:
    # Arrange
    _install(monkeypatch, fake_embedder, tmp_path)

    def _chat_down(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise exc

    monkeypatch.setattr(chat, "chat_json", _chat_down)

    # Act / Assert
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_ask(question="meadow survey", no_loop=True)
    assert excinfo.value.error.code == INTERNAL_ERROR
    assert excinfo.value.error.message == describe_ask_failure(exc)
    assert _SENTINEL not in excinfo.value.error.message


@pytest.mark.parametrize(
    "exc",
    [
        OllamaEmbedError(_SENTINEL, transient=True),
        OllamaEmbedError(_SENTINEL, transient=False),
        GraphTenantError(_SENTINEL),
        GraphBackendError(_SENTINEL),
    ],
    ids=["embed-transient", "embed-permanent", "graph-tenant", "graph-backend"],
)
def test_a_graph_leg_failure_is_internal_error_with_its_sentence(
    monkeypatch: pytest.MonkeyPatch,
    meadow: str,  # noqa: ARG001 — seeds the corpus
    fake_embedder: Any,
    tmp_path: Path,
    exc: BaseException,
) -> None:
    # Arrange
    _install(monkeypatch, fake_embedder, tmp_path)
    monkeypatch.setattr(chat, "chat_json", _chat)

    def _graph_down(*args: Any, **kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr("brain.graph_rag.graph_rag_search", _graph_down)

    # Act / Assert
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_ask(question="meadow survey", no_loop=True, mode="local")
    assert excinfo.value.error.code == INTERNAL_ERROR
    assert excinfo.value.error.message == describe_ask_failure(exc)
    assert _SENTINEL not in excinfo.value.error.message


def test_a_database_error_keeps_its_mapping(
    monkeypatch: pytest.MonkeyPatch,
    meadow: str,  # noqa: ARG001 — seeds the corpus
    fake_embedder: Any,
    tmp_path: Path,
) -> None:
    # Arrange
    _install(monkeypatch, fake_embedder, tmp_path)

    def _db_down(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise psycopg.OperationalError(f"server closed {_SENTINEL}")

    monkeypatch.setattr(chat, "chat_json", _db_down)

    # Act / Assert
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_ask(question="meadow survey", no_loop=True)
    assert excinfo.value.error.code == INTERNAL_ERROR
    assert excinfo.value.error.message == "database error: OperationalError"


def test_a_value_error_inside_the_run_keeps_its_invalid_params_mapping(
    monkeypatch: pytest.MonkeyPatch,
    meadow: str,  # noqa: ARG001 — seeds the corpus
    fake_embedder: Any,
    tmp_path: Path,
) -> None:
    """A ``ValueError`` raised inside the try reaches ``brain_ask``'s own ``except ValueError``.

    ``ASK_FAILURES`` sits in front of it; this proves that mapping still runs.
    """
    # Arrange
    _install(monkeypatch, fake_embedder, tmp_path)

    def _chat_value_error(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ValueError("synthetic")

    monkeypatch.setattr(chat, "chat_json", _chat_value_error)

    # Act / Assert
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_ask(question="meadow survey", no_loop=True)
    assert excinfo.value.error.code == INVALID_PARAMS
    assert excinfo.value.error.message == "synthetic"


def test_an_unknown_mode_is_rejected_before_the_run(
    monkeypatch: pytest.MonkeyPatch, fake_embedder: Any, tmp_path: Path
) -> None:
    # Arrange
    _install(monkeypatch, fake_embedder, tmp_path)

    # Act / Assert
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_ask(question="q", mode="bogus")
    assert excinfo.value.error.code == INVALID_PARAMS
