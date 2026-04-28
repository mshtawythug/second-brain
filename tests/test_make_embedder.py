"""Tests for the ``make_embedder`` factory and the CLI ``_build_embedder`` shim."""
from brain import cli as cli_module
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


def test_build_embedder_returns_embedder_from_config() -> None:
    """The CLI shim must satisfy the Embedder Protocol (and use the config)."""
    cfg = Config(
        database_url="postgresql://x:y@h:5432/d",
        ollama_host="http://203.0.113.99:11434",
        qwen3_model="qwen3-embedding:8b",
    )
    emb = cli_module._build_embedder(cfg)
    # Protocol-shaped: has embed + count_tokens.
    assert callable(emb.embed)
    assert callable(emb.count_tokens)
    # And it's actually a Qwen3Embedder under the hood.
    assert isinstance(emb, Qwen3Embedder)
    assert emb._host == "http://203.0.113.99:11434"
