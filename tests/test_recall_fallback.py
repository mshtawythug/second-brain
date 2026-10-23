"""``recall()`` falls back to text-only matching when the query embed fails (RM-01).

Spec: ``docs/specs/2026-10-23-roadmap-completion-design.md``, ticket RM-01. The
policy is :mod:`brain.search_fallback`'s; recall only routes its one search
through it and carries the outcome on ``RecallResult.fallback``, so the context
block's header and the JSON projection can say the passages are lexical only.

The unit tests build ``RecallResult`` by hand (no DB); the rest run ``recall``
against the 5434 test database with ``tests/embed_down.py``'s failing embedder.
All data is synthetic.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest

from brain.config import Config
from brain.embeddings import NullEmbedder, OllamaEmbedContextOverflow
from brain.ingest import Embedder, ExtractedDoc, ingest_document
from brain.recall import (
    _ENVELOPE_TOKENS,
    _HEADER_QUERY_MAX_TOKENS,
    RecallResult,
    _header_query,
    _render_header,
    recall,
)
from brain.search_fallback import SearchFallback
from tests.conftest import TEST_DATABASE_URL
from tests.embed_down import SECRET_DETAIL, BrokenEmbedder, QueryEmbedDownEmbedder

_FALLBACK_KEYS = ("fallback", "fallback_reason", "fallback_transient", "fallback_note")

_BODY = (
    "The lighthouse keeper logged the lamp maintenance, the fog signal tests "
    "and the supply boat schedule for the winter season. "
) * 30

#: Every word occurs in ``_BODY``, so the FTS leg matches it; long enough that,
#: echoed whole, the ``# recall:`` header alone would cost more than the 48-token
#: reserve.
_LONG_QUERY = (
    "lighthouse keeper logged lamp maintenance fog signal tests supply boat "
    "schedule winter season "
) * 3

_TRANSIENT = SearchFallback(reason="OllamaEmbedError", transient=True)


def _cfg() -> Config:
    return Config(
        database_url=TEST_DATABASE_URL,
        recall_passage_tokens=120,
        vector_sim_floor=0.0,
        recency_halflife_days=None,
    )


def _seed(conn: psycopg.Connection[Any], embedder: Any, count: int = 6) -> None:
    """Ingest ``count`` long synthetic notes that all match 'lighthouse'."""
    for i in range(count):
        ingest_document(
            conn,
            embedder=embedder,
            doc=ExtractedDoc(
                title=f"Lighthouse log {i}",
                content=f"{_BODY} Entry {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:lighthouse-{i}",
        )


def _recall(
    conn: psycopg.Connection[Any],
    embedder: Embedder,
    *,
    query: str = "lighthouse",
    budget_tokens: int = 2000,
) -> RecallResult:
    return recall(
        conn,
        _cfg(),
        embedder=embedder,
        query=query,
        budget_tokens=budget_tokens,
        max_candidates=25,
    )


class _OverflowEmbedder(QueryEmbedDownEmbedder):
    """A query embed that fails because the query exceeds the embed window."""

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        if input_type == "query":
            raise OllamaEmbedContextOverflow(f"input too long {SECRET_DETAIL}")
        return self._inner.embed(texts, input_type=input_type)


# ---------------------------------------------------------------------------
# RecallResult, by hand
# ---------------------------------------------------------------------------


def _hand_result(fallback: SearchFallback | None = None) -> RecallResult:
    return RecallResult(
        query="lamp maintenance",
        budget_tokens=500,
        used_tokens=0,
        candidates_considered=0,
        dropped=0,
        truncated=False,
        fts_count=0,
        passages=[],
        fallback=fallback,
    )


def test_fallback_defaults_to_none_so_existing_constructors_stay_valid() -> None:
    # Act
    result = RecallResult(
        query="q",
        budget_tokens=1,
        used_tokens=0,
        candidates_considered=0,
        dropped=0,
        truncated=False,
        fts_count=None,
        passages=[],
    )

    # Assert
    assert result.fallback is None


def test_header_says_text_only_exactly_when_a_fallback_happened() -> None:
    # Act
    plain = _hand_result().context_block().splitlines()[0]
    fell_back = _hand_result(_TRANSIENT).context_block().splitlines()[0]

    # Assert
    assert "text-only" not in plain
    assert fell_back.endswith(", text-only)")


def test_to_dict_carries_the_four_keys_as_null_on_a_normal_run() -> None:
    # Act
    payload = _hand_result().to_dict()

    # Assert
    assert {key: payload[key] for key in _FALLBACK_KEYS} == dict.fromkeys(_FALLBACK_KEYS)


def test_to_dict_carries_the_fallback_when_one_happened() -> None:
    # Act
    payload = _hand_result(_TRANSIENT).to_dict()

    # Assert
    assert payload["fallback"] == "fts_only"
    assert payload["fallback_reason"] == "OllamaEmbedError"
    assert payload["fallback_transient"] is True
    assert payload["fallback_note"] == _TRANSIENT.note
    json.dumps(payload)


# ---------------------------------------------------------------------------
# The header's query echo is capped, so the header fits its reserve
# ---------------------------------------------------------------------------


def _fake_cost(text: str) -> int:
    """``tests.conftest.FakeEmbedder.count_tokens``: one token per four chars."""
    return max(1, len(text) // 4)


@pytest.mark.parametrize("tokens", [_HEADER_QUERY_MAX_TOKENS - 1, _HEADER_QUERY_MAX_TOKENS])
def test_a_query_at_or_under_the_cap_is_echoed_whole(tokens: int) -> None:
    # Arrange
    query = "abcd" * tokens
    assert _fake_cost(query) == tokens

    # Act / Assert
    assert _header_query(query, cost=_fake_cost) == query


def test_a_query_one_token_over_the_cap_is_cut_with_one_ellipsis() -> None:
    # Arrange
    query = "abcd" * (_HEADER_QUERY_MAX_TOKENS + 1)

    # Act
    echoed = _header_query(query, cost=_fake_cost)

    # Assert
    assert echoed.endswith("…")
    assert echoed.count("…") == 1
    assert query.startswith(echoed[:-1])
    assert _fake_cost(echoed) <= _HEADER_QUERY_MAX_TOKENS


@pytest.mark.parametrize(
    "cost", [_fake_cost, NullEmbedder().count_tokens], ids=["fake", "cl100k"]
)
def test_worst_case_header_fits_the_envelope_reserve(cost: Any) -> None:
    """Asserted directly, so a future header edit cannot silently overrun it.

    Worst case: a query cut to the cap, four-digit counts, a seven-digit token
    total, and every optional suffix (``dropped``, ``truncated``, ``text-only``).
    """
    # Arrange
    capped = _header_query("lighthouse keeper " * 40, cost=cost)

    # Act
    header = _render_header(
        capped,
        passages=9999,
        used_tokens=9_999_999,
        dropped=9999,
        truncated=True,
        text_only=True,
    )

    # Assert
    assert cost(capped) <= _HEADER_QUERY_MAX_TOKENS
    assert cost(header) <= _ENVELOPE_TOKENS


def test_json_keeps_the_full_query_while_the_header_is_capped(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    # Arrange
    _seed(test_db, fake_embedder)

    # Act
    result = _recall(test_db, fake_embedder, query=_LONG_QUERY)

    # Assert
    assert result.to_dict()["query"] == _LONG_QUERY
    assert "header_query" not in result.to_dict()
    header = result.context_block().splitlines()[0]
    assert "… (" in header
    assert _LONG_QUERY not in header


# ---------------------------------------------------------------------------
# recall(), against the test database
# ---------------------------------------------------------------------------


def test_transient_embed_failure_returns_text_only_passages(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """REGRESSION: an unreachable embedder used to raise out of ``recall``."""
    # Arrange
    _seed(test_db, fake_embedder)
    down = QueryEmbedDownEmbedder(fake_embedder, transient=True)
    healthy_fts_count = _recall(test_db, fake_embedder).fts_count

    # Act
    result = _recall(test_db, down)

    # Assert
    assert result.passages, "the text-only retry must still return passages"
    assert {p.title for p in result.passages} <= {f"Lighthouse log {i}" for i in range(6)}
    assert result.fallback == SearchFallback(reason="OllamaEmbedError", transient=True)
    # The lexical-miss signal is the retry's own count, not a sum of attempts.
    assert result.fts_count == healthy_fts_count
    assert "comes back when the embedder does" in result.fallback.note
    assert down.query_calls == 1
    assert SECRET_DETAIL not in json.dumps(result.to_dict())
    assert SECRET_DETAIL not in result.context_block()


def test_permanent_embed_failure_note_names_configuration_and_doctor(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    # Arrange
    _seed(test_db, fake_embedder)

    # Act
    result = _recall(test_db, QueryEmbedDownEmbedder(fake_embedder, transient=False))

    # Assert
    assert result.passages
    assert result.fallback is not None
    assert result.fallback.transient is False
    assert "configuration problem" in result.fallback.note
    assert "brain doctor" in result.fallback.note


def test_context_overflow_falls_back_with_its_own_note(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    # Arrange
    _seed(test_db, fake_embedder)

    # Act
    result = _recall(test_db, _OverflowEmbedder(fake_embedder, transient=False))

    # Assert
    assert result.passages
    assert result.fallback is not None
    assert result.fallback.context_overflow is True
    assert "shorten the query" in result.fallback.note
    assert SECRET_DETAIL not in json.dumps(result.to_dict())


def test_healthy_recall_has_no_fallback(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    # Arrange
    _seed(test_db, fake_embedder)

    # Act
    result = _recall(test_db, fake_embedder)

    # Assert
    assert result.passages
    assert result.fallback is None
    assert "text-only" not in result.context_block().splitlines()[0]


def test_a_non_embed_error_still_propagates(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Only ``EmbedError`` falls back; a programming error is not swallowed."""
    # Arrange
    _seed(test_db, fake_embedder)

    # Act / Assert
    with pytest.raises(RuntimeError, match="synthetic programming error"):
        _recall(test_db, BrokenEmbedder(fake_embedder, transient=True))


@pytest.mark.parametrize("budget", range(300, 1500, 23))
def test_long_query_with_marker_still_fits_the_whole_budget(
    test_db: psycopg.Connection[Any], fake_embedder: Any, budget: int
) -> None:
    """The budget is honoured end to end, header and ``, text-only`` included.

    REGRESSION: the header echoes the query, and the 48-token reserve held back
    for it could not cover a long one until the echo was capped.
    """
    # Arrange
    _seed(test_db, fake_embedder)
    down = QueryEmbedDownEmbedder(fake_embedder, transient=True)

    # Act
    result = _recall(test_db, down, query=_LONG_QUERY, budget_tokens=budget)

    # Assert
    block = result.context_block()
    assert result.fallback is not None
    assert result.passages
    assert block.splitlines()[0].endswith(", text-only)")
    assert fake_embedder.count_tokens(block) <= budget


@pytest.mark.parametrize("budget", range(300, 1500, 23))
def test_long_query_fits_the_whole_budget_without_a_fallback_too(
    test_db: psycopg.Connection[Any], fake_embedder: Any, budget: int
) -> None:
    """The same overrun, on a healthy run: it predates the marker.

    REGRESSION: the fixed reserve assumed a short query (13 of these 53 budgets
    overflowed on master). It passes because the header's query echo is capped.
    """
    # Arrange
    _seed(test_db, fake_embedder)

    # Act
    result = _recall(test_db, fake_embedder, query=_LONG_QUERY, budget_tokens=budget)

    # Assert
    assert result.fallback is None
    assert result.passages
    assert fake_embedder.count_tokens(result.context_block()) <= budget
