"""Unit tests for the in-process query-embedding LRU cache (perf F1).

These exercise the cache helpers directly with a spy embedder and need no DB.
The cache and registry are process-global (an ``lru_cache``), so the autouse
fixture clears both around every test via their public APIs (``cache_clear`` /
``dict.clear``) — standard test-double cleanup, not monkey-patching.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from brain.search import (
    _cached_query_embed,
    _embedder_identity,
    _embedder_registry,
    _query_embed,
)


class _SpyEmbedder:
    """Counts embed calls and returns a deterministic vector.

    Satisfies the surface the cache touches: ``dim``, ``embed`` and an optional
    ``_model`` attribute (mirrors the real Ollama/Voyage backends).
    """

    def __init__(self, *, dim: int = 8, model: str | None = None) -> None:
        self.dim = dim
        if model is not None:
            self._model = model
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        self.calls.append((tuple(texts), input_type))
        # Seed the vector by dim so distinct-dim spies produce distinct vectors.
        return [[float(self.dim) + i for i in range(self.dim)] for _ in texts]

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    _cached_query_embed.cache_clear()
    _embedder_registry.clear()
    yield
    _cached_query_embed.cache_clear()
    _embedder_registry.clear()


def test_repeat_query_hits_cache() -> None:
    spy = _SpyEmbedder(model="arctic", dim=8)
    first = _query_embed(spy, "hello world")
    second = _query_embed(spy, "hello world")
    assert first == second
    # Second call is served from cache — the embedder is consulted once.
    assert len(spy.calls) == 1


def test_returns_fresh_list_copy_not_shared() -> None:
    spy = _SpyEmbedder(model="arctic", dim=4)
    first = _query_embed(spy, "q")
    first.append(999.0)  # mutate the returned list
    second = _query_embed(spy, "q")
    assert 999.0 not in second  # cached entry was not corrupted
    assert first is not second


def test_returned_vector_matches_embedder_output() -> None:
    spy = _SpyEmbedder(model="arctic", dim=8)
    expected = spy.embed(["q"], input_type="query")[0]
    spy.calls.clear()
    assert _query_embed(spy, "q") == expected


def test_input_type_is_part_of_key() -> None:
    spy = _SpyEmbedder(model="arctic", dim=8)
    _query_embed(spy, "q", input_type="query")
    _query_embed(spy, "q", input_type="document")
    assert len(spy.calls) == 2  # different input_type ⇒ different cache entry
    assert {call[1] for call in spy.calls} == {"query", "document"}


def test_different_model_is_a_different_entry() -> None:
    a = _SpyEmbedder(model="arctic", dim=8)
    b = _SpyEmbedder(model="qwen3", dim=8)
    _query_embed(a, "q")
    _query_embed(b, "q")
    # b must NOT receive a's cached vector — both compute independently.
    assert len(a.calls) == 1
    assert len(b.calls) == 1
    assert _embedder_identity(a) != _embedder_identity(b)


def test_different_dim_is_a_different_entry() -> None:
    a = _SpyEmbedder(model="m", dim=8)
    b = _SpyEmbedder(model="m", dim=16)
    _query_embed(a, "q")
    _query_embed(b, "q")
    assert len(a.calls) == 1
    assert len(b.calls) == 1
    assert _embedder_identity(a) != _embedder_identity(b)


def test_same_identity_shares_entry() -> None:
    a = _SpyEmbedder(model="arctic", dim=8)
    b = _SpyEmbedder(model="arctic", dim=8)  # identical identity to ``a``
    _query_embed(a, "shared")
    _query_embed(b, "shared")
    assert len(a.calls) == 1
    assert len(b.calls) == 0  # cache hit; the second embedder is never consulted
    assert _embedder_identity(a) == _embedder_identity(b)


def test_identity_includes_class_model_and_dim() -> None:
    spy = _SpyEmbedder(model="arctic", dim=8)
    ident = _embedder_identity(spy)
    assert type(spy).__qualname__ in ident
    assert "arctic" in ident
    assert "8" in ident


def test_identity_without_model_attr_falls_back_to_class_and_dim() -> None:
    spy = _SpyEmbedder(dim=8)  # no ``_model`` attribute
    ident = _embedder_identity(spy)
    assert type(spy).__qualname__ in ident
    assert "8" in ident
