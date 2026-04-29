"""Test harness — uses a real Postgres test DB and a fake embedder.

The test DB is reset (schema dropped + recreated) before each test that uses it.
"""

import hashlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from dotenv import load_dotenv

from brain.db import connect, run_migrations
from brain.ingest import ExtractedDoc, ingest_document

load_dotenv()

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


@pytest.fixture(autouse=True, scope="session")
def _ensure_test_db_initialized() -> Iterator[None]:
    """Reset the test DB to a known migrated state at session start.

    Tests that don't take the per-test ``test_db`` fixture (e.g. doctor checks
    that the pgvector extension is installed) still need the schema to be in a
    migrated state. Doing this once per session — before any test runs — makes
    that starting state deterministic regardless of any prior aborted runs.
    """
    with connect(TEST_DATABASE_URL) as conn:
        conn.autocommit = True
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        run_migrations(conn)
    yield


@pytest.fixture
def test_db() -> Iterator[psycopg.Connection]:
    """Fresh schema in the test DB for each test."""
    with connect(TEST_DATABASE_URL) as conn:
        conn.autocommit = True
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        run_migrations(conn)
        yield conn


class FakeEmbedder:
    """Deterministic embedder for tests — hashes text into a stable vector.

    Default ``dim`` matches the qwen3 production schema (4096); pass a
    different ``dim`` to construct vectors at other sizes if a test needs
    it (e.g. 1024 for the arctic / voyage backend paths). ``dim`` is
    exposed as an instance attribute to satisfy the
    :class:`brain.ingest.Embedder` Protocol.
    """

    def __init__(self, dim: int = 4096) -> None:
        self.dim = dim

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        return [self._vec(t, input_type) for t in texts]

    def count_tokens(self, text: str) -> int:
        # rough approximation: 1 token per ~4 chars
        return max(1, len(text) // 4)

    def _vec(self, text: str, input_type: str) -> list[float]:
        h = hashlib.sha256((input_type + ":" + text).encode()).digest()
        # 32 bytes → 32 floats, tiled to ``self.dim``.
        floats = [b / 255.0 - 0.5 for b in h]
        # Tile enough copies to cover ``dim``, then truncate.
        repeats = (self.dim + len(floats) - 1) // len(floats)
        return (floats * repeats)[: self.dim]


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


class CountingEmbedder:
    """Wrap an embedder to count embed calls — used by `brain edit` tests
    that need to assert the title-only path didn't re-embed.

    Mirrors the wrapped embedder's ``dim`` so it satisfies the same Protocol.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.dim = inner.dim
        self.embed_calls = 0
        self.token_calls = 0

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        self.embed_calls += 1
        return self._inner.embed(texts, input_type=input_type)  # type: ignore[no-any-return]

    def count_tokens(self, text: str) -> int:
        self.token_calls += 1
        return self._inner.count_tokens(text)  # type: ignore[no-any-return]


@pytest.fixture
def counting_embedder(fake_embedder: FakeEmbedder) -> CountingEmbedder:
    return CountingEmbedder(fake_embedder)


class FakeRunner:
    """Test double for :class:`brain.vault.derived_links.directory.GwsRunner`.

    Records every ``args`` list it's invoked with so tests can assert the
    Calendar/Contacts subcommand shape. ``response`` defaults to an empty
    JSON list (a valid empty success that still advances the high-water
    mark in ``directory_refresh_state``). ``raises`` simulates a
    subprocess failure — ``refresh_calendar`` / ``refresh_contacts``
    catch ``(OSError, DirectoryRefreshError, RuntimeError)`` from the
    runner and downgrade them to soft warnings, so most call sites pass
    a ``RuntimeError`` here to exercise that branch without dragging in
    real subprocess machinery.

    Importable via ``from tests.conftest import FakeRunner`` — same
    pattern as :class:`FakeEmbedder` above.
    """

    def __init__(
        self,
        *,
        response: str = "[]",
        raises: BaseException | None = None,
    ) -> None:
        self.response = response
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if self.raises is not None:
            raise self.raises
        return self.response


@pytest.fixture
def patch_embedder(monkeypatch: pytest.MonkeyPatch) -> Callable[[object], None]:
    """Wire ``DATABASE_URL`` env var and swap ``brain.cli._build_embedder`` to
    return ``embedder``. Returns a callable so tests pick which embedder
    (fake vs counting) to install."""

    def _install(embedder: object) -> None:
        monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
        monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: embedder)

    return _install


@pytest.fixture
def seed_doc(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> Callable[..., str]:
    """Factory fixture: ingest a manual document and return its UUID.

    Defaults match the simplest test seed; override any kwarg as needed."""

    def _seed(
        *,
        title: str = "Initial Title",
        content: str = "Initial body content.",
        content_type: str = "note",
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> str:
        result = ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=ExtractedDoc(
                title=title,
                content=content,
                content_type=content_type,
                source_path=None,
                metadata=metadata or {},
            ),
            source_kind="manual",
            tags=tags or [],
        )
        assert result.document_id is not None
        return result.document_id

    return _seed


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
