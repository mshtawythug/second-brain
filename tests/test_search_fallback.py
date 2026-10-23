"""``brain.search_fallback`` — text-only search when the query embed fails.

Plan: ``docs/plans/2026-10-23-search-fts-fallback.md``. Runs against the real
test Postgres; the failing embedder is :mod:`tests.embed_down`. All data is
synthetic.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest

from brain.embeddings import OllamaEmbedContextOverflow, OllamaEmbedError
from brain.ingest import ExtractedDoc, ingest_document
from brain.search import SearchDiagnostics, SearchResult, hybrid_search
from brain.search_fallback import (
    FALLBACK_MODE,
    FallbackSearch,
    SearchFallback,
    fallback_json,
    search_with_fts_fallback,
)
from tests.embed_down import SECRET_DETAIL, BrokenEmbedder, QueryEmbedDownEmbedder

_FALLBACK_KEYS = {"fallback", "fallback_reason", "fallback_transient", "fallback_note"}


def _seed(conn: psycopg.Connection[Any], embedder: Any, count: int = 3) -> None:
    """Ingest ``count`` synthetic notes that all match the word 'lighthouse'."""
    for i in range(count):
        ingest_document(
            conn,
            embedder=embedder,
            doc=ExtractedDoc(
                title=f"Lighthouse note {i}",
                content=f"The lighthouse keeper logged the tide table {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:lighthouse-{i}",
            tags=["coast"],
        )


# --- SearchFallback / fallback_json (pure) ----------------------------------


def test_reason_is_class_only_never_the_message() -> None:
    # Arrange
    exc = OllamaEmbedError(f"boom {SECRET_DETAIL}", transient=True)

    # Act
    fallback = SearchFallback.from_error(exc)

    # Assert
    assert fallback.reason.startswith("OllamaEmbedError")
    assert SECRET_DETAIL not in fallback.reason
    assert SECRET_DETAIL not in fallback.note
    assert fallback.transient is True


def test_transient_note_says_the_embedder_is_unavailable() -> None:
    # Arrange / Act
    note = SearchFallback(reason="OllamaEmbedError", transient=True).note

    # Assert
    assert "text-only" in note
    assert "unavailable" in note
    assert "brain doctor" not in note
    assert "\n" not in note


def test_permanent_note_names_a_configuration_problem() -> None:
    # Arrange / Act
    note = SearchFallback(reason="OllamaEmbedError", transient=False).note

    # Assert
    assert "text-only" in note
    assert "configuration" in note
    assert "retry will not fix" in note
    assert "brain doctor" in note
    assert "\n" not in note


def test_context_overflow_gets_its_own_note_not_brain_doctor() -> None:
    """The query is too long for the embed window: not a config fault, not retryable."""
    # Arrange
    exc = OllamaEmbedContextOverflow(f"input too long {SECRET_DETAIL}")

    # Act
    fallback = SearchFallback.from_error(exc)

    # Assert
    assert fallback.transient is False
    assert fallback.context_overflow is True
    assert fallback.reason.startswith("OllamaEmbedContextOverflow")
    assert "BRAIN_OLLAMA_EMBED_NUM_CTX" in fallback.note
    assert "shorten the query" in fallback.note
    assert "text-only" in fallback.note
    assert "brain doctor" not in fallback.note
    assert "configuration problem" not in fallback.note
    assert SECRET_DETAIL not in fallback.note
    assert fallback_json(fallback)["fallback_transient"] is False


def test_a_plain_permanent_failure_is_not_a_context_overflow() -> None:
    # Act
    fallback = SearchFallback.from_error(OllamaEmbedError("x", transient=False))

    # Assert
    assert fallback.context_overflow is False
    assert "brain doctor" in fallback.note


def test_fallback_json_is_all_null_without_a_fallback() -> None:
    # Act
    payload = fallback_json(None)

    # Assert
    assert payload == dict.fromkeys(_FALLBACK_KEYS)


def test_fallback_json_names_the_mode_reason_and_note() -> None:
    # Arrange
    fallback = SearchFallback(reason="OllamaEmbedError", transient=False)

    # Act
    payload = fallback_json(fallback)

    # Assert
    assert set(payload) == _FALLBACK_KEYS
    assert payload["fallback"] == FALLBACK_MODE == "fts_only"
    assert payload["fallback_reason"] == "OllamaEmbedError"
    assert payload["fallback_transient"] is False
    assert payload["fallback_note"] == fallback.note


# --- search_with_fts_fallback: the retry contract (pure, recording fake) ----


class _RecordingSearch:
    """A ``search_fn`` that raises on its first call and records every call."""

    def __init__(self, first_error: BaseException | None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._first_error = first_error

    def __call__(self, conn: Any, **kwargs: Any) -> list[SearchResult]:
        self.calls.append(kwargs)
        if len(self.calls) == 1 and self._first_error is not None:
            raise self._first_error
        return []


def test_retry_passes_fts_only_and_keeps_every_other_kwarg() -> None:
    # Arrange
    search_fn = _RecordingSearch(OllamaEmbedError("x", transient=True))
    sentinel_embedder = object()

    # Act
    outcome = search_with_fts_fallback(
        search_fn,
        None,
        embedder=sentinel_embedder,
        query="q",
        limit=7,
        fts_only=False,
        tag="coast",
    )

    # Assert
    assert len(search_fn.calls) == 2
    first, second = search_fn.calls
    assert first["fts_only"] is False
    assert second == {**first, "fts_only": True}
    assert second["embedder"] is sentinel_embedder
    assert outcome.fallback is not None


def test_no_retry_when_the_first_call_succeeds() -> None:
    # Arrange
    search_fn = _RecordingSearch(None)

    # Act
    outcome = search_with_fts_fallback(search_fn, None, embedder=object(), query="q")

    # Assert
    assert len(search_fn.calls) == 1
    assert outcome == FallbackSearch(results=[], fallback=None)


@dataclass
class _GrownDiagnostics(SearchDiagnostics):
    """A diagnostics holder with a ``default_factory`` field, as a future one may have."""

    extra: list[int] = field(default_factory=list)


def test_reset_uses_real_defaults_even_for_a_default_factory_field() -> None:
    """``Field.default`` is ``MISSING`` for a factory field; the reset must not write it."""
    # Arrange
    diagnostics = _GrownDiagnostics(fts_count=4, extra=[1, 2])
    search_fn = _RecordingSearch(OllamaEmbedError("x", transient=True))

    # Act
    search_with_fts_fallback(
        search_fn, None, embedder=object(), query="q", diagnostics=diagnostics
    )

    # Assert
    assert diagnostics.extra == []
    assert diagnostics.fts_count is None


def test_a_non_embed_error_is_not_swallowed() -> None:
    # Arrange
    search_fn = _RecordingSearch(psycopg.OperationalError("synthetic db down"))

    # Act / Assert
    with pytest.raises(psycopg.OperationalError):
        search_with_fts_fallback(search_fn, None, embedder=object(), query="q")
    assert len(search_fn.calls) == 1


# --- against the real test database ------------------------------------------


@pytest.mark.parametrize("transient", [True, False])
def test_embedder_down_returns_fts_hits_not_a_traceback(
    test_db: psycopg.Connection[Any], fake_embedder: Any, transient: bool
) -> None:
    """REGRESSION: an unreachable embedder used to raise out of the search."""
    # Arrange
    _seed(test_db, fake_embedder)
    down = QueryEmbedDownEmbedder(fake_embedder, transient=transient)

    # Act
    outcome = search_with_fts_fallback(
        hybrid_search, test_db, embedder=down, query="lighthouse", limit=5
    )

    # Assert
    assert down.query_calls == 1
    assert {r.title for r in outcome.results} == {
        "Lighthouse note 0", "Lighthouse note 1", "Lighthouse note 2",
    }
    assert outcome.fallback == SearchFallback(
        reason="OllamaEmbedError", transient=transient
    )


def test_fallback_results_equal_an_explicit_fts_only_search(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """DRY: the fallback IS the existing fts_only path, not a second FTS."""
    # Arrange
    _seed(test_db, fake_embedder)
    down = QueryEmbedDownEmbedder(fake_embedder, transient=True)

    # Act
    outcome = search_with_fts_fallback(
        hybrid_search, test_db, embedder=down, query="lighthouse", explain=True
    )
    explicit = hybrid_search(
        test_db, embedder=fake_embedder, query="lighthouse", fts_only=True,
        explain=True,
    )

    # Assert
    assert [r.document_id for r in outcome.results] == [
        r.document_id for r in explicit
    ]
    for r in outcome.results:
        assert r.explain is not None
        assert r.explain.matched_filters["fts_only"] is True
        assert r.explain.vector_rank is None


def test_healthy_embedder_is_untouched(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    # Arrange
    _seed(test_db, fake_embedder)

    # Act
    outcome = search_with_fts_fallback(
        hybrid_search, test_db, embedder=fake_embedder, query="lighthouse",
        explain=True,
    )

    # Assert
    assert outcome.fallback is None
    assert outcome.results
    assert all(
        r.explain is not None and r.explain.matched_filters["fts_only"] is False
        for r in outcome.results
    )


class _SlowDownEmbedder(QueryEmbedDownEmbedder):
    """Fails a query embed only after ``delay`` seconds, like a timeout."""

    delay = 0.2

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        if input_type == "query":
            time.sleep(self.delay)
        return super().embed(texts, input_type=input_type)


def test_diagnostics_are_reset_and_total_covers_both_attempts(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """A slow failed embed must not vanish from ``total_ms``."""
    # Arrange
    _seed(test_db, fake_embedder)
    down = _SlowDownEmbedder(fake_embedder, transient=True)
    diagnostics = SearchDiagnostics(facets_ms=123.0, results_tokens=9)

    # Act
    search_with_fts_fallback(
        hybrid_search, test_db, embedder=down, query="lighthouse",
        diagnostics=diagnostics, total_count=True,
    )

    # Assert
    assert diagnostics.total_ms is not None
    assert diagnostics.total_ms >= _SlowDownEmbedder.delay * 1000
    assert diagnostics.embed_ms is None  # so the meta's fts_only reads true
    assert diagnostics.embed_cached is None
    assert diagnostics.vector_ms is None
    assert diagnostics.fts_count == 3
    assert diagnostics.total_documents == 3
    # Fields from before the call are a caller's to write afterwards; the
    # reset leaves nothing stale from the failed attempt.
    assert diagnostics.facets_ms is None
    assert diagnostics.results_tokens is None


def test_non_embed_failure_from_the_embedder_propagates(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    # Arrange
    _seed(test_db, fake_embedder)
    broken = BrokenEmbedder(fake_embedder, transient=True)

    # Act / Assert
    with pytest.raises(RuntimeError):
        search_with_fts_fallback(
            hybrid_search, test_db, embedder=broken, query="lighthouse"
        )
