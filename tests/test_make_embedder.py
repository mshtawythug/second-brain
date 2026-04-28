"""Tests for the ``make_embedder`` factory and the CLI ``_build_embedder`` shim.

Phase 3.5: factory dispatches on ``cfg.embedder`` ∈ ``{"arctic", "voyage", "qwen3"}``.
"""
import pytest

from brain import cli as cli_module
from brain.config import Config, ConfigError
from brain.embeddings import (
    ArcticEmbedder,
    Qwen3Embedder,
    VoyageEmbedder,
    make_embedder,
)


def _cfg(**overrides: object) -> Config:
    """Build a Config with sane defaults; override individual fields per test."""
    base: dict[str, object] = {
        "database_url": "postgresql://x:y@h:5432/d",
        "ollama_host": "http://localhost:11434",
        "qwen3_model": "qwen3-embedding:8b",
        "embedder": "arctic",
        "voyage_api_key": None,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def test_make_embedder_default_returns_arctic() -> None:
    """Default ``embedder == "arctic"`` → ``ArcticEmbedder`` with dim 1024."""
    emb = make_embedder(_cfg())
    assert isinstance(emb, ArcticEmbedder)
    assert emb.dim == 1024


def test_make_embedder_qwen3_returns_qwen3_with_dim_4096() -> None:
    emb = make_embedder(_cfg(embedder="qwen3"))
    assert isinstance(emb, Qwen3Embedder)
    assert emb.dim == 4096


def test_make_embedder_voyage_with_key_returns_voyage() -> None:
    emb = make_embedder(_cfg(embedder="voyage", voyage_api_key="vk-test"))
    assert isinstance(emb, VoyageEmbedder)
    assert emb.dim == 1024


def test_make_embedder_voyage_without_key_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="VOYAGE_API_KEY"):
        make_embedder(_cfg(embedder="voyage", voyage_api_key=None))


def test_make_embedder_unknown_backend_raises_config_error() -> None:
    """Anything outside the 3-backend whitelist must raise."""
    # bypass Config.load() validation by constructing the dataclass directly
    cfg = _cfg(embedder="bogus")
    with pytest.raises(ConfigError, match="must be one of"):
        make_embedder(cfg)


def test_make_embedder_arctic_uses_configured_host() -> None:
    emb = make_embedder(_cfg(ollama_host="http://203.0.113.5:11434"))
    assert isinstance(emb, ArcticEmbedder)
    assert emb._host == "http://203.0.113.5:11434"


def test_make_embedder_qwen3_honors_host_and_model() -> None:
    emb = make_embedder(
        _cfg(
            embedder="qwen3",
            ollama_host="http://203.0.113.5:11434",
            qwen3_model="qwen3-embedding:4b",
        )
    )
    assert isinstance(emb, Qwen3Embedder)
    assert emb._host == "http://203.0.113.5:11434"
    assert emb._model == "qwen3-embedding:4b"


def test_build_embedder_returns_embedder_from_config() -> None:
    """The CLI shim must satisfy the Embedder Protocol (and use the config)."""
    cfg = _cfg(ollama_host="http://203.0.113.99:11434")
    emb = cli_module._build_embedder(cfg)
    # Protocol-shaped: dim + embed + count_tokens.
    assert isinstance(emb.dim, int)
    assert callable(emb.embed)
    assert callable(emb.count_tokens)
    # And it's the active concrete class.
    assert isinstance(emb, ArcticEmbedder)
    assert emb._host == "http://203.0.113.99:11434"
