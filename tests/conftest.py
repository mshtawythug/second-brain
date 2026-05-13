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
def _force_test_vault_path(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Prevent the test suite from writing fixtures into ~/brain-vault.

    Without this, any test that invokes a CLI ingest command without an
    explicit ``monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))``
    override will resolve ``Config.load().vault_path`` to the user's real
    ``~/brain-vault`` and write vault-mirror files there.  On 2026-05-08/09
    ~50 test fixtures leaked into the prod vault, causing
    ``unique constraint violation (mirror drift)`` errors that saturated the
    file watcher.

    Sets ``os.environ["BRAIN_VAULT_PATH"]`` to a session-scoped tmp dir for
    the entire pytest session — mirroring the pattern used by
    :func:`_force_test_database_url` for ``DATABASE_URL``.  Per-test
    ``monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))`` calls still
    work — ``monkeypatch`` undoes itself per-test, after which this
    session-scope assignment continues to hold.

    Hard-fails immediately if ``BRAIN_VAULT_PATH`` is already set to the real
    vault at session start (e.g., exported in the shell environment).
    """
    real_vault = (Path.home() / "brain-vault").resolve()

    # Pre-flight: fail now rather than silently leak into the live corpus.
    current_env = os.environ.get("BRAIN_VAULT_PATH")
    if current_env is not None:
        current_resolved = Path(current_env).expanduser().resolve()
        assert current_resolved != real_vault, (
            f"BRAIN_VAULT_PATH is already set to the real vault ({real_vault})! "
            "Running the test suite would write test fixtures into your live "
            "knowledge base. Unset BRAIN_VAULT_PATH or point it at a test directory."
        )

    session_vault = tmp_path_factory.mktemp("vault")
    original = os.environ.get("BRAIN_VAULT_PATH")
    os.environ["BRAIN_VAULT_PATH"] = str(session_vault)
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("BRAIN_VAULT_PATH", None)
        else:
            os.environ["BRAIN_VAULT_PATH"] = original


@pytest.fixture(autouse=True, scope="session")
def _force_test_database_url() -> Iterator[None]:
    """Bulletproof prod-DB isolation for the entire test session.

    Without this, any test that uses ``CliRunner().invoke(app, ...)`` without
    first calling ``_patch_embedder`` (or its local copies) reads
    ``DATABASE_URL`` from ``.env`` (= PROD) inside
    ``brain.config.Config.load()``. On 2026-05-04 the post-merge full-suite
    run leaked 15 test fixtures (titles like "Renamed Document",
    "Sample Heading", "person-x sync") into the real ``second_brain`` DB before
    being noticed. This fixture forces
    ``os.environ["DATABASE_URL"] = TEST_DATABASE_URL`` for the whole pytest
    session and restores the original at teardown.

    Per-test ``monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)`` calls
    in individual fixtures (``patch_embedder``, ``_patch_embedder`` siblings)
    still work — ``monkeypatch`` undoes itself per-test, after which this
    session-scope assignment continues to hold. There is no ordering
    requirement; tests that already use those helpers don't need to change.
    """
    original = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original


def _reset_schema_and_migrate(conn: psycopg.Connection) -> None:
    """Drop and recreate the public schema then run all migrations.

    ``DROP SCHEMA public CASCADE`` orphans the vector and pgcrypto extensions
    in pg_extension (the namespace OID dies, but the pg_extension row stays).
    A subsequent ``CREATE EXTENSION IF NOT EXISTS vector`` then silently no-ops
    because pg_extension still has the row — leaving the new public schema
    without the vector type.

    Workaround for vector: move it to pg_catalog (which survives the DROP)
    before dropping the schema, then move it back to public afterwards.
    pgcrypto's gen_random_uuid() conflicts with pg_catalog, so we handle it
    by dropping and reinstalling it explicitly.
    """
    # Move vector to pg_catalog before the schema drop so it survives.
    # pgcrypto cannot go to pg_catalog (DuplicateFunction on gen_random_uuid).
    if conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
        conn.execute("ALTER EXTENSION vector SET SCHEMA pg_catalog")

    # Drop pgcrypto explicitly: chunks has no pgcrypto dependency, so no cascade needed.
    conn.execute("DROP EXTENSION IF EXISTS pgcrypto")

    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    # Move vector back to public — run_migrations will see it there and skip re-install.
    if conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
        conn.execute("ALTER EXTENSION vector SET SCHEMA public")

    # pgcrypto is gone, so run_migrations will reinstall it from migration 001.
    run_migrations(conn)


@pytest.fixture(autouse=True, scope="session")
def _ensure_test_db_initialized(_force_test_database_url: None) -> Iterator[None]:
    """Reset the test DB to a known migrated state at session start.

    Tests that don't take the per-test ``test_db`` fixture (e.g. doctor checks
    that the pgvector extension is installed) still need the schema to be in a
    migrated state. Doing this once per session — before any test runs — makes
    that starting state deterministic regardless of any prior aborted runs.

    Depends on :func:`_force_test_database_url` so the schema reset happens
    against ``second_brain_test``, never against prod.
    """
    with connect(TEST_DATABASE_URL) as conn:
        conn.autocommit = True
        _reset_schema_and_migrate(conn)
    yield


@pytest.fixture
def test_db() -> Iterator[psycopg.Connection]:
    """Fresh schema in the test DB for each test."""
    with connect(TEST_DATABASE_URL) as conn:
        conn.autocommit = True
        _reset_schema_and_migrate(conn)
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
