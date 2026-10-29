"""``format_search.explanation_json``: the one projection of a ``SearchExplanation`` (RM-12).

``brain explain --json`` and MCP ``brain_explain`` both emit it, so the two cannot describe a
ranking differently. The key ORDER is pinned too: the CLI's payload was byte-identical before the
move, and must stay so.
"""
from __future__ import annotations

import dataclasses

import pytest

from brain.format_search import explanation_json
from brain.search import SearchExplanation

pytestmark = pytest.mark.nodb

#: Every field of ``SearchExplanation``, in the order ``brain explain --json`` has always printed.
EXPLAIN_KEYS = [
    "fts_rank",
    "fts_score",
    "fts_rrf_contribution",
    "vector_rank",
    "vector_cosine",
    "vector_rrf_contribution",
    "rrf_score",
    "recency_age_days",
    "recency_boost",
    "final_score",
    "best_chunk_id",
    "best_chunk_index",
    "matched_filters",
    "reranker_score",
]


def _explanation() -> SearchExplanation:
    return SearchExplanation(
        fts_rank=1,
        fts_score=0.5,
        fts_rrf_contribution=1 / 61,
        vector_rank=None,
        vector_cosine=None,
        vector_rrf_contribution=0.0,
        rrf_score=1 / 61,
        recency_age_days=3.0,
        recency_boost=1.1,
        final_score=1.1 / 61,
        best_chunk_id="00000000-0000-4000-8000-000000000001",
        best_chunk_index=0,
        matched_filters={"source_kind": "manual"},
        reranker_score=None,
    )


def test_none_projects_to_none() -> None:
    # Act / Assert
    assert explanation_json(None) is None


def test_every_field_is_projected_in_the_cli_order() -> None:
    # Arrange
    explanation = _explanation()

    # Act
    projected = explanation_json(explanation)

    # Assert
    assert projected is not None
    assert list(projected) == EXPLAIN_KEYS
    assert projected == dataclasses.asdict(explanation)


def test_the_projection_covers_every_dataclass_field() -> None:
    """A field added to ``SearchExplanation`` must be added to the projection (and this list)."""
    # Act
    fields = [field.name for field in dataclasses.fields(SearchExplanation)]

    # Assert
    assert fields == EXPLAIN_KEYS
