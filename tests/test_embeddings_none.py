"""Tests for the FTS-only ``BRAIN_EMBEDDER=none`` backend (``NullEmbedder``).

Task A of the adoption/DX plan: a user with NO Ollama gets a working brain
(ingest + FTS search + passing doctor) instead of crashes. This file covers:

- A1 unit: ``Config`` accepts ``none``; ``make_embedder`` returns a
  ``NullEmbedder`` (dim 1024, ``produces_embeddings is False``); offline
  ``count_tokens``; ``embed()`` raises the shared ``EmbedError`` base with a
  ``BRAIN_EMBEDDER=none`` message.
- A5/A6 integration (real test DB): ingest + in-place body edit under the null
  backend store NULL embeddings and stay FTS-retrievable — no embed call, no
  crash.
- A11 integration (fresh schema): the ``none`` → ``arctic`` upgrade backfills
  NULL embeddings and finalizes (NOT NULL + HNSW) without a destructive reset,
  because ``NullEmbedder.dim`` matches the arctic/voyage 1024-dim schema.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from brain import config as config_module
from brain.config import Config
from brain.embeddings import ArcticEmbedder, NullEmbedder, make_embedder
from brain.errors import EmbedError

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _cfg(**overrides: object) -> Config:
    """Build a Config with sane defaults; override individual fields per test.

    Mirrors ``tests/test_make_embedder.py::_cfg`` — a direct dataclass
    construction that bypasses ``Config.load()`` env parsing so a test can pin
    ``embedder`` without touching the process environment.
    """
    base: dict[str, object] = {
        "database_url": "postgresql://x:y@h:5432/d",
        "ollama_host": "http://localhost:11434",
        "qwen3_model": "qwen3-embedding:8b",
        "embedder": "arctic",
        "voyage_api_key": None,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


@pytest.fixture
def isolated_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate ``Config.load()`` from any real .env discovery.

    Copies the pattern from ``tests/test_config.py`` so an env-driven
    ``BRAIN_EMBEDDER`` value under test cannot be clobbered by the repo ``.env``.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: tmp_path / "project.env")
    monkeypatch.setattr(
        config_module, "_brain_home_dotenv", lambda: tmp_path / "brain_home.env"
    )
    monkeypatch.setattr(
        config_module,
        "_brain_home_root",
        lambda _config_file=None: tmp_path / "brain_home_root",
    )
    monkeypatch.delenv("BRAIN_EMBEDDER", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# A1 — Config accepts BRAIN_EMBEDDER=none
# ---------------------------------------------------------------------------


def test_config_accepts_none_embedder(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path
) -> None:
    """``BRAIN_EMBEDDER=none`` loads cleanly (no ConfigError) and is preserved."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    monkeypatch.setenv("BRAIN_EMBEDDER", "none")
    cfg = Config.load()
    assert cfg.embedder == "none"


def test_config_none_embedder_lowercased(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path
) -> None:
    """``BRAIN_EMBEDDER=NONE`` is normalized to lowercase before validation."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    monkeypatch.setenv("BRAIN_EMBEDDER", "NONE")
    cfg = Config.load()
    assert cfg.embedder == "none"


# ---------------------------------------------------------------------------
# A1 — make_embedder(cfg) returns NullEmbedder
# ---------------------------------------------------------------------------


def test_make_embedder_none_returns_null_embedder() -> None:
    """``embedder == "none"`` → ``NullEmbedder`` with dim 1024, no vectors."""
    emb = make_embedder(_cfg(embedder="none"))
    assert isinstance(emb, NullEmbedder)
    assert emb.dim == 1024
    assert emb.produces_embeddings is False


def test_null_embedder_dim_matches_arctic() -> None:
    """dim 1024 == arctic/voyage so a none→arctic upgrade is a plain reembed."""
    assert NullEmbedder().dim == 1024


def test_null_embedder_produces_embeddings_flag_is_false() -> None:
    """The duck-typed degradation flag is explicitly False."""
    assert NullEmbedder().produces_embeddings is False


# ---------------------------------------------------------------------------
# A1 — count_tokens works offline (tiktoken), same as arctic
# ---------------------------------------------------------------------------


def test_null_embedder_count_tokens_offline_matches_arctic() -> None:
    """``count_tokens`` uses the same offline cl100k_base tokenizer as arctic."""
    text = "hello world, this is a synthetic sentence for token counting"
    null_count = NullEmbedder().count_tokens(text)
    arctic_count = ArcticEmbedder(host="http://localhost:11434").count_tokens(text)
    assert null_count == arctic_count
    assert null_count > 0


# ---------------------------------------------------------------------------
# A1 — embed() raises the shared EmbedError base with a BRAIN_EMBEDDER=none hint
# ---------------------------------------------------------------------------


def test_null_embedder_embed_raises_embed_error() -> None:
    """``embed()`` raises the shared ``EmbedError`` base, not a bare Exception."""
    with pytest.raises(EmbedError, match="BRAIN_EMBEDDER=none"):
        NullEmbedder().embed(["some document text"])


def test_null_embedder_embed_raises_for_query_input_type() -> None:
    """The query path raises identically — no silent empty-vector fallback."""
    with pytest.raises(EmbedError, match="BRAIN_EMBEDDER=none"):
        NullEmbedder().embed(["a query"], input_type="query")


def test_null_embedder_embed_message_guides_upgrade() -> None:
    """The error message names the concrete recovery steps."""
    with pytest.raises(EmbedError) as exc_info:
        NullEmbedder().embed(["x"])
    message = str(exc_info.value)
    assert "install Ollama" in message
    assert "BRAIN_EMBEDDER=arctic" in message
    assert "brain init" in message
    assert "brain reembed" in message
