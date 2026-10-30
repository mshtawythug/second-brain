"""``brain search --entity`` / ``--entity-type`` (RM-24 ACs 1-4), through the real CLI.

Synthetic notes and entities on the test DB. The graph flag is set per test with
``monkeypatch.setenv``, because the suite forces it off by default.
"""
from __future__ import annotations

import json
import os
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.entity_filter import GRAPH_REQUIRED_MESSAGE
from brain.ingest import Embedder
from tests.entity_filter_seed import SHARED_TERM, seed_document, seed_entity, seed_mention

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def corpus(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    fake_embedder: Embedder,
) -> dict[str, Any]:
    """Four notes, two of which mention the entity; the graph is on."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "true")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    docs = [seed_document(test_db, fake_embedder, f"Survey note {n}") for n in range(4)]
    entity = seed_entity(test_db, name="Quillmoor", canonical_key="quillmoor")
    for doc in docs[:2]:
        seed_mention(test_db, entity, doc)
    return {"docs": docs, "mentioned": set(docs[:2])}


def _search(*args: str) -> Any:
    return CliRunner().invoke(
        app, ["search", SHARED_TERM, "--fts-only", "--limit", "20", *args]
    )


def test_only_the_entity_documents_are_returned(corpus: dict[str, Any]) -> None:
    result = _search("--entity", "Quillmoor", "--json")

    assert result.exit_code == 0, result.output
    assert {row["id"] for row in json.loads(result.stdout)} == corpus["mentioned"]


def test_total_and_facets_agree_with_the_filtered_set(corpus: dict[str, Any]) -> None:
    result = _search("--entity", "quillmoor", "--json", "--meta", "--facets")

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["total_documents"] == len(corpus["mentioned"])
    assert sum(b["count"] for b in envelope["facets"]["content_type"]) == len(
        corpus["mentioned"]
    )
    assert {row["id"] for row in envelope["results"]} == corpus["mentioned"]


def test_an_unknown_entity_is_a_usage_error(corpus: dict[str, Any]) -> None:  # noqa: ARG001
    result = _search("--entity", "Nobody Here")

    assert result.exit_code == 2
    assert "brain graphrag entities" in result.output


def test_an_ambiguous_entity_is_a_usage_error(
    corpus: dict[str, Any],  # noqa: ARG001
    test_db: psycopg.Connection[Any],
) -> None:
    seed_entity(test_db, name="Quillmoor", canonical_key="quillmoor", entity_type="topic")

    result = _search("--entity", "Quillmoor")

    assert result.exit_code == 2
    assert "--entity-type" in result.output


def test_the_entity_type_settles_the_ambiguity(
    corpus: dict[str, Any], test_db: psycopg.Connection[Any]
) -> None:
    seed_entity(test_db, name="Quillmoor", canonical_key="quillmoor", entity_type="topic")

    result = _search("--entity", "Quillmoor", "--entity-type", "project", "--json")

    assert result.exit_code == 0, result.output
    assert {row["id"] for row in json.loads(result.stdout)} == corpus["mentioned"]


def test_graph_off_is_a_usage_error_with_the_graph_message(
    corpus: dict[str, Any],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "false")

    result = _search("--entity", "Quillmoor")

    assert result.exit_code == 2
    # Rich wraps the usage panel, so compare with the whitespace collapsed.
    assert "BRAIN_GRAPH_ENABLED=true" in " ".join(result.output.split())
    assert GRAPH_REQUIRED_MESSAGE.split(":")[0] in " ".join(result.output.split())


def test_no_entity_leaves_the_search_unfiltered(corpus: dict[str, Any]) -> None:
    result = _search("--json")

    assert result.exit_code == 0, result.output
    assert {row["id"] for row in json.loads(result.stdout)} == set(corpus["docs"])
