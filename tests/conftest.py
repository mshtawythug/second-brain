"""Test harness — uses a real Postgres test DB and a fake embedder.

The test DB is reset (schema dropped + recreated) before each test that uses it.
"""

import hashlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
import pytest
from dotenv import load_dotenv

from brain.db import connect, run_migrations
from brain.ingest import ExtractedDoc, ingest_document

load_dotenv()

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    # Default to the Apache-AGE test instance (docker-compose.age-test.yml,
    # port 5434, named volume) — a separate container from prod on 5433.
    # Start it: docker compose -f docker-compose.age-test.yml up -d --build
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

# --- DB-SAFETY HARD GUARD ---------------------------------------------------
# The schema reset below runs ``DROP SCHEMA public CASCADE`` — irreversibly
# destructive. It must NEVER target the prod container. Prod is the
# ``second_brain`` database on localhost:5433 (docker-compose.yml); the test
# instance is ``*_test`` on port 5434 (docker-compose.age-test.yml). If
# ``TEST_DATABASE_URL`` / ``DATABASE_URL`` is ever pointed at prod (e.g. an
# accidental export), we ABORT loudly rather than wipe production data.
_PROD_PORT = 5433
_PROD_DB_NAME = "second_brain"
_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})


def _looks_like_prod_db(host: str | None, port: int | None, dbname: str | None) -> bool:
    """True if (host, port, dbname) resolves to the prod container.

    Refuses the prod port on a local host, OR the exact prod database name on
    any host (belt-and-suspenders: catches a non-default prod port mapping).
    """
    is_local = (host or "").lower() in _LOCAL_HOSTS
    return (is_local and port == _PROD_PORT) or (dbname == _PROD_DB_NAME)


def _assert_not_prod_db(host: str | None, port: int | None, dbname: str | None) -> None:
    """Abort the session if a destructive reset would hit the prod database."""
    if _looks_like_prod_db(host, port, dbname):
        raise RuntimeError(
            "REFUSING to run the destructive test schema reset against what "
            f"looks like the PROD database (host={host!r} port={port!r} "
            f"db={dbname!r}). The DROP SCHEMA reset must only ever target the "
            "AGE test instance (port 5434, db '*_test'). Fix TEST_DATABASE_URL "
            "/ DATABASE_URL so it points at the test container."
        )


# Fail fast at import/collection time on the resolved TEST_DATABASE_URL.
_test_url = urlparse(TEST_DATABASE_URL)
_assert_not_prod_db(
    _test_url.hostname, _test_url.port, (_test_url.path or "").lstrip("/")
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


@pytest.fixture(autouse=True, scope="session")
def _force_graph_flags_default() -> Iterator[None]:
    """Isolate the whole suite from the local ``.env``'s GraphRAG feature flags.

    ``Config.load()`` reads the ``.env`` FILE via ``dotenv_values`` (not just
    ``os.environ``) and applies it with ``os.environ.setdefault`` — so a flag
    present in the repo ``.env`` flows into every ``Config.load()`` unless that
    key is ALREADY in ``os.environ``. Once the operational ``.env`` enables the
    graph flags (``BRAIN_GRAPH_ENABLED`` for the prod cutover,
    ``BRAIN_GRAPH_CONCEPTS`` for the concept backfill), any test asserting the
    *disabled* default silently flips — and a bare ``monkeypatch.delenv`` does
    NOT help, because the file value is re-injected after the delete.

    Mirroring :func:`_force_test_database_url`, force both flags to an EMPTY
    string for the session (empty -> the parser uses the code default, i.e.
    disabled), so ``os.environ.setdefault`` skips the ``.env`` value. Per-test
    ``monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "true")`` in the enabled-path
    tests still overrides this and undoes itself per-test.
    """
    keys = ("BRAIN_GRAPH_ENABLED", "BRAIN_GRAPH_CONCEPTS")
    originals = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ[key] = ""
    try:
        yield
    finally:
        for key, original in originals.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


def _reset_age_graph(conn: psycopg.Connection) -> None:
    """Reset Apache AGE graph state between DB tests.

    The AGE test instance (``docker-compose.age-test.yml``, port 5434) ships
    the ``age`` shared library; the extension *object* must still be created
    once per database (``CREATE EXTENSION age CASCADE``), after which
    ``LOAD 'age'`` makes its catalog functions callable in the session.

    AGE keeps graphs in the ``ag_catalog`` schema plus a per-graph schema —
    neither lives in ``public``, so the ``DROP SCHEMA public CASCADE`` performed
    by :func:`_reset_schema_and_migrate` does NOT clear them. We explicitly drop
    ``brain_graph`` (the canonical graph the G0-2 bootstrap creates) so each
    test starts from a clean graph, mirroring that bootstrap.

    Runs under autocommit (the caller sets ``conn.autocommit = True``) because
    AGE catalog DDL wants explicit commits under psycopg v3. ``search_path`` is
    set to ``ag_catalog`` ONLY for the AGE statements and reset immediately
    afterward, so the later ``run_migrations`` DDL still lands in ``public`` and
    never leaks the ``ag_catalog`` namespace onto the session.
    """
    # Idempotent: the .so is in the AGE image; the extension object may not
    # exist yet on a freshly-created database.
    conn.execute("CREATE EXTENSION IF NOT EXISTS age CASCADE")
    conn.execute("LOAD 'age'")
    conn.execute('SET search_path = ag_catalog, "$user", public')
    try:
        existing = conn.execute(
            "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s",
            ("brain_graph",),
        ).fetchone()
        if existing is not None:
            conn.execute("SELECT drop_graph('brain_graph', true)")
    finally:
        # Restore the default search_path so migration DDL targets public —
        # never leak ag_catalog onto the session.
        conn.execute("RESET search_path")


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

    AGE graph state lives in ``ag_catalog`` (outside ``public``) and so
    survives the schema drop; :func:`_reset_age_graph` clears the canonical
    ``brain_graph`` first so each test gets a clean graph.
    """
    # DB-SAFETY: guard the ACTUAL connection target (not just the env var) — if
    # this connection somehow points at the prod container, abort before the
    # destructive DROP SCHEMA below ever runs.
    _assert_not_prod_db(conn.info.host, conn.info.port, conn.info.dbname)

    # Reset AGE graph state first — it lives outside public and would otherwise
    # leak across tests. Scopes ag_catalog to its own statements (no leakage).
    _reset_age_graph(conn)

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
