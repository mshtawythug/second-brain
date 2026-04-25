"""Tests for the Voyage embeddings client wrapper."""
from unittest.mock import MagicMock

from brain.embeddings import VoyageEmbedder


def _client_returning(vectors: list[list[float]]) -> MagicMock:
    client = MagicMock()
    client.embed.return_value = MagicMock(embeddings=vectors)
    return client


def test_embed_single_batch() -> None:
    client = _client_returning([[0.1] * 1024, [0.2] * 1024])
    emb = VoyageEmbedder(api_key="test", client=client)
    out = emb.embed(["hello", "world"], input_type="document")
    assert len(out) == 2
    assert len(out[0]) == 1024
    client.embed.assert_called_once_with(
        texts=["hello", "world"], model="voyage-4", input_type="document"
    )


def test_embed_batches_respect_limit() -> None:
    client = MagicMock()
    client.embed.side_effect = [
        MagicMock(embeddings=[[0.1] * 1024] * 128),
        MagicMock(embeddings=[[0.2] * 1024] * 2),
    ]
    emb = VoyageEmbedder(api_key="test", client=client, batch_size=128)
    out = emb.embed(["t"] * 130, input_type="document")
    assert len(out) == 130
    assert client.embed.call_count == 2


def test_count_tokens_uses_local_tokenizer() -> None:
    emb = VoyageEmbedder(api_key="test", client=MagicMock())
    n = emb.count_tokens("hello world this is a test")
    assert n > 0
