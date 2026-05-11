"""Phase D regression — vector cosine floor (plan revisions #3 + #6).

Three behaviors covered here:

1. The floor is honored — chunks below ``vector_sim_floor`` are
   excluded from the vector leg.
2. Default applies when ``BRAIN_VECTOR_SIM_FLOOR`` is unset; values
   are validated as floats in ``[0.0, 1.0]``.
3. Invalid env values raise :class:`brain.config.ConfigError` (not
   ``BrainError`` — per plan revision #6).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import config as config_module
from brain.config import DEFAULT_VECTOR_SIM_FLOOR, Config, ConfigError
from brain.ingest import ExtractedDoc, ingest_document
from brain.search import hybrid_search


@pytest.fixture()
def isolated_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Block .env file sources so delenv tests aren't undone by T1.0 setdefault."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: tmp_path / "project.env")
    monkeypatch.setattr(
        config_module, "_brain_home_dotenv", lambda: tmp_path / "brain_home.env"
    )
    monkeypatch.delenv("BRAIN_HOME", raising=False)


def _ingest(
    conn: psycopg.Connection[Any],
    embedder: Any,
    *,
    title: str,
    content: str,
) -> str:
    res = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=f"manual:{title}",
        tags=[],
    )
    assert res.document_id is not None
    return res.document_id


# ---------------------------------------------------------------------------
# Search SQL behavior
# ---------------------------------------------------------------------------
def test_floor_zero_returns_all_vector_candidates(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Floor=0.0 keeps the legacy behavior (no vector filtering)."""
    _ingest(test_db, fake_embedder, title="A", content="alpha")
    _ingest(test_db, fake_embedder, title="B", content="beta")
    _ingest(test_db, fake_embedder, title="C", content="gamma")

    # Use a query that doesn't FTS-match any doc — only the vector leg
    # contributes candidates. Floor=0 means every chunk passes.
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="zzznomatchzzz",
        limit=10,
        vector_sim_floor=0.0,
    )
    # FakeEmbedder produces deterministic vectors; they may or may not
    # cluster, but with floor=0 we should get *some* vector candidates
    # back. The exact identity isn't important — just that we get more
    # than 0.
    assert len(results) >= 1


def test_floor_one_excludes_all_vector_candidates(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Floor=1.0 excludes everything except an exact-cosine-1.0 match.

    With FakeEmbedder's hash-based vectors, no chunk will hit cosine 1.0
    against the query embedding, so the vector leg returns 0 rows and
    only FTS contributes. With a query that doesn't FTS-match anything
    either, results is empty.
    """
    _ingest(test_db, fake_embedder, title="A", content="alpha")
    _ingest(test_db, fake_embedder, title="B", content="beta")

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="zzznomatchzzz",
        limit=10,
        vector_sim_floor=1.0,
    )
    assert results == []


def test_floor_does_not_affect_fts_only_path(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """``fts_only=True`` skips the vector leg entirely — floor is irrelevant.

    Even with floor=1.0 (which would block all vector hits), the FTS
    leg still returns its matches. Confirms the floor predicate is
    scoped to the vector SQL only.
    """
    _ingest(test_db, fake_embedder, title="Hit", content="rare-term-foobar")

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="rare-term-foobar",
        limit=10,
        fts_only=True,
        vector_sim_floor=1.0,  # would block every vector hit
    )
    assert [r.title for r in results] == ["Hit"]


# ---------------------------------------------------------------------------
# Config loader behavior
# ---------------------------------------------------------------------------
def test_default_floor_applied_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dotenv: None,
) -> None:
    """Unset ``BRAIN_VECTOR_SIM_FLOOR`` ⇒ ``cfg.vector_sim_floor`` is the default."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@localhost:5/z")
    monkeypatch.delenv("BRAIN_VECTOR_SIM_FLOOR", raising=False)
    cfg = Config.load()
    assert cfg.vector_sim_floor == DEFAULT_VECTOR_SIM_FLOOR


def test_empty_env_value_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace-only env value is treated as unset (matches user_email pattern)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@localhost:5/z")
    monkeypatch.setenv("BRAIN_VECTOR_SIM_FLOOR", "   ")
    cfg = Config.load()
    assert cfg.vector_sim_floor == DEFAULT_VECTOR_SIM_FLOOR


def test_explicit_env_value_overrides_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid float in [0.0, 1.0] is parsed and stored."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@localhost:5/z")
    monkeypatch.setenv("BRAIN_VECTOR_SIM_FLOOR", "0.42")
    cfg = Config.load()
    assert cfg.vector_sim_floor == pytest.approx(0.42)


def test_zero_floor_is_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.0 is a valid floor — disables filtering."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@localhost:5/z")
    monkeypatch.setenv("BRAIN_VECTOR_SIM_FLOOR", "0.0")
    cfg = Config.load()
    assert cfg.vector_sim_floor == 0.0


def test_one_floor_is_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1.0 is a valid floor — excludes everything below exact match."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@localhost:5/z")
    monkeypatch.setenv("BRAIN_VECTOR_SIM_FLOOR", "1.0")
    cfg = Config.load()
    assert cfg.vector_sim_floor == 1.0


def test_negative_floor_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative values would silently re-admit the noise tail."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@localhost:5/z")
    monkeypatch.setenv("BRAIN_VECTOR_SIM_FLOOR", "-0.1")
    with pytest.raises(ConfigError, match="BRAIN_VECTOR_SIM_FLOOR"):
        Config.load()


def test_above_one_floor_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Values >1 would exclude every chunk."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@localhost:5/z")
    monkeypatch.setenv("BRAIN_VECTOR_SIM_FLOOR", "1.5")
    with pytest.raises(ConfigError, match="BRAIN_VECTOR_SIM_FLOOR"):
        Config.load()


def test_non_numeric_floor_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage env value surfaces as ConfigError (not ValueError)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@localhost:5/z")
    monkeypatch.setenv("BRAIN_VECTOR_SIM_FLOOR", "not-a-number")
    with pytest.raises(ConfigError, match="BRAIN_VECTOR_SIM_FLOOR"):
        Config.load()
