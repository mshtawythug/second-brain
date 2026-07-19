"""``hybrid_search`` auto-degrades to FTS-only when the embedder makes no vectors.

The degradation is duck-typed on ``produces_embeddings`` INSIDE
``hybrid_search`` so every caller — the CLI, the MCP server, and any library
consumer — inherits it. These tests lock:

- A raising, no-vector embedder yields FTS results with no embed call (A7/A8).
- A normal embedder (no flag) still runs the vector leg — the coercion must not
  disable hybrid search for arctic / qwen3 / voyage.
- ``brain search`` / ``brain explain`` degrade AND print the one-line hint.
"""
from __future__ import annotations

import os
from typing import Any

import psycopg
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document
from brain.search import hybrid_search
from tests.conftest import CountingEmbedder, FakeEmbedder

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


class _RaisingNoVectorEmbedder(FakeEmbedder):
    """A backend that declares no vectors and blows up if ``embed()`` is called.

    ``produces_embeddings = False`` mirrors :class:`brain.embeddings.NullEmbedder`
    but on an arbitrary subclass — proving the coercion keys off the duck-typed
    flag, not the concrete type. ``embed()`` raising is the tripwire: a degraded
    search must never reach it.
    """

    produces_embeddings = False

    def embed(
        self, texts: list[str], input_type: str = "document"
    ) -> list[list[float]]:
        raise AssertionError(
            "embed() must not be called when the embedder produces no vectors"
        )


def _seed(conn: psycopg.Connection, *, title: str, content: str) -> str:
    """Ingest one manual doc (real FakeEmbedder vectors) and return its id."""
    result = ingest_document(
        conn,
        embedder=FakeEmbedder(),
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=[],
    )
    assert result.document_id is not None
    return result.document_id


# ---------------------------------------------------------------------------
# A7/A8 — direct hybrid_search degradation
# ---------------------------------------------------------------------------


def test_hybrid_search_degrades_to_fts_when_embedder_makes_no_vectors(
    test_db: psycopg.Connection,
) -> None:
    """A no-vector embedder degrades to FTS-only — results returned, no embed call.

    ``fts_only`` is left at its default ``False``; without the coercion the
    embedder's ``embed()`` would raise. A non-empty, exception-free result set
    proves ``hybrid_search`` degraded internally.
    """
    doc_id = _seed(
        test_db,
        title="Quarterly roadmap",
        content="Synthetic body about the quarterly planning roadmap and goals.",
    )
    results = hybrid_search(
        test_db,
        embedder=_RaisingNoVectorEmbedder(),
        query="quarterly planning roadmap",
    )
    assert doc_id in [r.document_id for r in results]


def test_hybrid_search_normal_embedder_still_runs_vector_leg(
    test_db: psycopg.Connection,
) -> None:
    """Regression: an embedder WITHOUT the flag keeps the vector leg (embed called).

    Guards against the coercion accidentally disabling hybrid search for the
    real backends, which never declare ``produces_embeddings``.
    """
    _seed(
        test_db,
        title="Quarterly roadmap",
        content="Synthetic body about the quarterly planning roadmap and goals.",
    )
    counting = CountingEmbedder(FakeEmbedder())
    hybrid_search(test_db, embedder=counting, query="quarterly planning roadmap")
    assert counting.embed_calls >= 1


# ---------------------------------------------------------------------------
# A8 — CLI search / explain degrade AND print the one-line hint
# ---------------------------------------------------------------------------

_HINT = "semantic search off (BRAIN_EMBEDDER=none)"


def test_cli_search_degrades_and_prints_hint(
    test_db: psycopg.Connection,
    patch_embedder: Any,
) -> None:
    """``brain search`` under a no-vector embedder still finds docs + warns once."""
    _seed(
        test_db,
        title="Quarterly roadmap",
        content="Synthetic body about the quarterly planning roadmap and goals.",
    )
    patch_embedder(_RaisingNoVectorEmbedder())
    result = CliRunner().invoke(app, ["search", "quarterly planning roadmap"])
    assert result.exit_code == 0, result.output
    assert "Quarterly roadmap" in result.output
    assert _HINT in result.output


def test_cli_explain_degrades_and_prints_hint(
    test_db: psycopg.Connection,
    patch_embedder: Any,
) -> None:
    """``brain explain`` degrades identically and prints the same hint."""
    _seed(
        test_db,
        title="Quarterly roadmap",
        content="Synthetic body about the quarterly planning roadmap and goals.",
    )
    patch_embedder(_RaisingNoVectorEmbedder())
    result = CliRunner().invoke(app, ["explain", "quarterly planning roadmap"])
    assert result.exit_code == 0, result.output
    assert _HINT in result.output


def test_cli_search_no_hint_with_normal_embedder(
    test_db: psycopg.Connection,
    patch_embedder: Any,
) -> None:
    """A normal embedder prints NO degradation hint (regression guard)."""
    _seed(
        test_db,
        title="Quarterly roadmap",
        content="Synthetic body about the quarterly planning roadmap and goals.",
    )
    patch_embedder(FakeEmbedder())
    result = CliRunner().invoke(app, ["search", "quarterly planning roadmap"])
    assert result.exit_code == 0, result.output
    assert _HINT not in result.output
