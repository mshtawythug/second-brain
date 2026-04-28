"""Tests for the Voyage embeddings client wrapper.

Restored in Phase 3.5 (deleted in Phase 1's all-in commit). The Voyage
backend is now one of three pluggable embedders selected via
``BRAIN_EMBEDDER``; this file covers its specific batching + token-counting
behavior. The factory dispatch is covered separately in
``test_make_embedder.py``.
"""
from unittest.mock import MagicMock

from brain.embeddings import VoyageEmbedder


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
