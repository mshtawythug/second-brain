"""MCP ``brain_search(entity=..., entity_type=...)`` (RM-24 ACs 1-4), against the test DB.

Synthetic notes and entities. Resolution errors and graph-off map to ``INVALID_PARAMS``.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from mcp.types import INVALID_PARAMS

from brain import mcp_server
from brain.config import Config
from brain.entity_filter import GRAPH_REQUIRED_MESSAGE
from brain.ingest import Embedder
from brain.mcp_compat import MCPError
from tests.entity_filter_seed import SHARED_TERM, seed_document, seed_entity, seed_mention

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _install_state(
    monkeypatch: pytest.MonkeyPatch, embedder: Embedder, *, graph_enabled: bool
) -> None:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, graph_enabled=graph_enabled),
        embedder=embedder,
    )
    monkeypatch.setattr(mcp_server, "_state", state)


@pytest.fixture
def corpus(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    fake_embedder: Embedder,
) -> Iterator[dict[str, Any]]:
    """Four notes, two of which mention the entity; the graph is on."""
    _install_state(monkeypatch, fake_embedder, graph_enabled=True)
    docs = [seed_document(test_db, fake_embedder, f"Survey note {n}") for n in range(4)]
    entity = seed_entity(test_db, name="Quillmoor", canonical_key="quillmoor")
    for doc in docs[:2]:
        seed_mention(test_db, entity, doc)
    yield {"docs": docs, "mentioned": set(docs[:2])}


def _search(**kwargs: Any) -> dict[str, Any]:
    return mcp_server.brain_search(query=SHARED_TERM, limit=20, fts_only=True, **kwargs)


def test_only_the_entity_documents_are_returned(corpus: dict[str, Any]) -> None:
    got = _search(entity="Quillmoor")

    assert {row["id"] for row in got["results"]} == corpus["mentioned"]


def test_total_and_facets_agree_with_the_filtered_set(corpus: dict[str, Any]) -> None:
    got = _search(entity="quillmoor", facets=True)

    assert got["total_documents"] == len(corpus["mentioned"])
    assert sum(b["count"] for b in got["facets"]["content_type"]) == len(corpus["mentioned"])


def test_an_unknown_entity_is_invalid_params(corpus: dict[str, Any]) -> None:  # noqa: ARG001
    with pytest.raises(MCPError) as exc_info:
        _search(entity="Nobody Here")

    assert exc_info.value.error.code == INVALID_PARAMS
    assert "brain graphrag entities" in exc_info.value.error.message


def test_an_ambiguous_entity_is_invalid_params(
    corpus: dict[str, Any],  # noqa: ARG001
    test_db: psycopg.Connection[Any],
) -> None:
    seed_entity(test_db, name="Quillmoor", canonical_key="quillmoor", entity_type="topic")

    with pytest.raises(MCPError) as exc_info:
        _search(entity="Quillmoor")

    assert exc_info.value.error.code == INVALID_PARAMS
    assert "topic" in exc_info.value.error.message


def test_entity_type_settles_the_ambiguity(
    corpus: dict[str, Any], test_db: psycopg.Connection[Any]
) -> None:
    seed_entity(test_db, name="Quillmoor", canonical_key="quillmoor", entity_type="topic")

    got = _search(entity="Quillmoor", entity_type="project")

    assert {row["id"] for row in got["results"]} == corpus["mentioned"]


def test_graph_off_is_invalid_params_with_the_graph_message(
    corpus: dict[str, Any],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: Embedder,
) -> None:
    _install_state(monkeypatch, fake_embedder, graph_enabled=False)

    with pytest.raises(MCPError) as exc_info:
        _search(entity="Quillmoor")

    assert exc_info.value.error.code == INVALID_PARAMS
    assert exc_info.value.error.message == GRAPH_REQUIRED_MESSAGE


def test_no_entity_leaves_the_search_unfiltered(corpus: dict[str, Any]) -> None:
    got = _search()

    assert {row["id"] for row in got["results"]} == set(corpus["docs"])
