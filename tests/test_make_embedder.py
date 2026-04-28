"""Tests for the ``make_embedder`` factory."""
from brain.config import Config
from brain.embeddings import Qwen3Embedder, make_embedder


def test_make_embedder_returns_qwen3_instance() -> None:
    cfg = Config(database_url="postgresql://x:y@h:5432/d")
    emb = make_embedder(cfg)
    assert isinstance(emb, Qwen3Embedder)


def test_make_embedder_honors_host_and_model() -> None:
    cfg = Config(
        database_url="postgresql://x:y@h:5432/d",
        ollama_host="http://203.0.113.5:11434",
        qwen3_model="qwen3-embedding:4b",
    )
    emb = make_embedder(cfg)
    assert emb._host == "http://203.0.113.5:11434"
    assert emb._model == "qwen3-embedding:4b"
