"""Tests for the Voyage embeddings client wrapper.

Restored in Phase 3.5 (deleted in Phase 1's all-in commit). The Voyage
backend is now one of three pluggable embedders selected via
``BRAIN_EMBEDDER``; this file covers its specific batching + token-counting
behavior. The factory dispatch is covered separately in
``test_make_embedder.py``.
"""
from unittest.mock import MagicMock

import pytest

from brain.embeddings import VoyageEmbedder, VoyageEmbedError
from brain.errors import EmbedError


def _client_returning(vectors: list[list[float]]) -> MagicMock:
    client = MagicMock()
    client.embed.return_value = MagicMock(embeddings=vectors)
    return client


def test_voyage_embedder_dim_is_1024() -> None:
    """voyage-3.5 emits 1024-dim vectors — assert the class attribute."""
    assert VoyageEmbedder.dim == 1024
    emb = VoyageEmbedder(api_key="test", client=MagicMock())
    assert emb.dim == 1024


def test_embed_single_batch() -> None:
    client = _client_returning([[0.1] * 1024, [0.2] * 1024])
    emb = VoyageEmbedder(api_key="test", client=client)
    out = emb.embed(["hello", "world"], input_type="document")
    assert len(out) == 2
    assert len(out[0]) == 1024
    client.embed.assert_called_once_with(
        texts=["hello", "world"], model="voyage-3.5", input_type="document"
    )


def test_embed_batches_respect_limit() -> None:
    """batch_size=128: a 130-row request must split into 128 + 2."""
    client = MagicMock()
    client.embed.side_effect = [
        MagicMock(embeddings=[[0.1] * 1024] * 128),
        MagicMock(embeddings=[[0.2] * 1024] * 2),
    ]
    emb = VoyageEmbedder(api_key="test", client=client, batch_size=128)
    out = emb.embed(["t"] * 130, input_type="document")
    assert len(out) == 130
    assert client.embed.call_count == 2


def test_embed_passes_query_input_type_through() -> None:
    """Voyage's SDK natively understands ``input_type="query"`` — pass it untouched."""
    client = _client_returning([[0.0] * 1024])
    emb = VoyageEmbedder(api_key="test", client=client)
    emb.embed(["what did person-a say?"], input_type="query")
    client.embed.assert_called_once_with(
        texts=["what did person-a say?"], model="voyage-3.5", input_type="query"
    )


def test_count_tokens_uses_local_tokenizer() -> None:
    emb = VoyageEmbedder(api_key="test", client=MagicMock())
    n = emb.count_tokens("hello world this is a test")
    assert n > 0


def test_voyage_embed_wraps_sdk_error_in_typed_embed_error() -> None:
    """Regression (Task 2.12a): a Voyage SDK failure surfaces as the shared
    typed :class:`VoyageEmbedError` (an :class:`EmbedError`) — the same typed
    contract the Ollama backends honor — never a leaked ``voyageai`` exception.
    """
    from voyageai.error import RateLimitError

    client = MagicMock()
    client.embed.side_effect = RateLimitError("rate limited")
    emb = VoyageEmbedder(api_key="test", client=client)

    with pytest.raises(VoyageEmbedError) as excinfo:
        emb.embed(["hello"], input_type="document")
    # Shared base so callers can ``except EmbedError`` across backends.
    assert isinstance(excinfo.value, EmbedError)
    # Original SDK error preserved for diagnostics.
    assert isinstance(excinfo.value.__cause__, RateLimitError)


def test_voyage_embed_empty_input_makes_no_sdk_call() -> None:
    """Empty input returns ``[]`` with no SDK round-trip (matches the Ollama base)."""
    client = MagicMock()
    emb = VoyageEmbedder(api_key="test", client=client)
    assert emb.embed([]) == []
    client.embed.assert_not_called()


def test_keep_alive_payload_coerces_numeric_sentinels() -> None:
    """Regression (Wave 1 addendum): the ``"-1"`` / ``"0"`` keep_alive sentinels
    must be coerced to JSON NUMBERS so Ollama 0.24.0 accepts them (it rejects the
    unit-less strings with HTTP 400 ``missing unit in duration``); unit-bearing
    duration strings pass through unchanged. The full on-the-wire payload
    assertion (via the httpx transport) lives in ``tests/test_arctic_embedder.py``.
    """
    from brain.embeddings import _keep_alive_payload

    assert _keep_alive_payload("-1") == -1
    assert isinstance(_keep_alive_payload("-1"), int)
    assert _keep_alive_payload("0") == 0
    assert isinstance(_keep_alive_payload("0"), int)
    assert _keep_alive_payload("30m") == "30m"
    assert isinstance(_keep_alive_payload("30m"), str)
    assert _keep_alive_payload("1h") == "1h"
    assert isinstance(_keep_alive_payload("1h"), str)
