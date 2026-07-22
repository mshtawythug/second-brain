"""Postgres connection + migration helpers."""
import importlib.resources
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql

from .embedding_targets import (
    embedding_index_name,
    validate_embedding_target,
)
from .errors import AgeBootstrapError, BrainError
from .ingest import Embedder

# Canonical Apache AGE graph name created by the ``brain init`` bootstrap and
# traversed by the graph backend (wave G0). Defined here — not in
# :mod:`brain.config` — so this low-level layer stays dependency-free; the
# configurable ``BRAIN_GRAPH_NAME`` override (spec §10) is resolved by higher
# layers and passed in explicitly as ``graph_name`` when it lands (G0-4+).
# ``tests/conftest.py``'s ``_reset_age_graph`` drops exactly this graph between
# tests, so the bootstrap and the reset must agree on the name.
DEFAULT_GRAPH_NAME = "brain_graph"


def connect_raw(database_url: str) -> psycopg.Connection:
    """Open a connection with pgvector adapter registered.

    Same semantics as :func:`connect` but without the context-manager
    wrapper. Callers own the connection lifecycle: they MUST call
    ``conn.close()`` themselves. Used by the long-running watcher, which
    holds a connection across many sync calls in a worker thread; the
    `with` block of :func:`connect` would auto-close it after the first
    use.
    """
    conn = psycopg.connect(database_url, connect_timeout=10)
    # The vector extension may not exist yet during the initial `brain init`
    # bootstrap; check pg_type before registering so we don't rely on
    # exception-as-control-flow. The SELECT starts an implicit transaction
    # under psycopg3's default autocommit=False — roll it back so the
    # caller can still flip autocommit on if needed.
    row = conn.execute(
        "SELECT 1 FROM pg_type WHERE typname = 'vector'"
    ).fetchone()
    conn.rollback()
    if row is not None:
        register_vector(conn)
    return conn


@contextmanager
def connect(database_url: str) -> Iterator[psycopg.Connection]:
    """Open a connection with pgvector adapter registered.

    Tolerates the bootstrap case where the `vector` extension has not yet been
    installed (e.g. the first `brain init` on a fresh database). In that case
    the adapter registration is skipped; callers that need vector support
    should open a new connection after `run_migrations`.
    """
    conn = connect_raw(database_url)
    try:
        yield conn
    finally:
        conn.close()


def _age_extension_installed(conn: psycopg.Connection) -> bool:
    """True iff the ``age`` extension object exists in this database.

    ``LOAD 'age'`` only loads the shared library; the openCypher catalog
    functions (``cypher``, ``create_graph``, ...) come from the extension
    object created by ``CREATE EXTENSION age``. On a fresh database — before
    :func:`bootstrap_age` runs during ``brain init`` — that object is absent, so
    the session helpers must no-op rather than fail. Mirrors the
    ``vector``-extension tolerance in :func:`connect_raw`.
    """
    row = conn.execute(
        "SELECT 1 FROM pg_extension WHERE extname = 'age'"
    ).fetchone()
    return row is not None


def load_age(conn: psycopg.Connection) -> bool:
    """Make Apache AGE callable on ``conn`` for the rest of the session.

    Issues ``LOAD 'age'`` so the openCypher catalog functions become available.
    AGE requires this once per backend session even when the extension object
    already exists — verified against the live AGE image, where a fresh
    connection that skips ``LOAD`` raises
    ``unhandled cypher(cstring) function call``.

    Deliberately does **not** mutate ``search_path``. The graph backend
    fully-qualifies every call (``ag_catalog.cypher(...) AS (col
    ag_catalog.agtype)``), which the live AGE image accepts without an
    ``ag_catalog``-first search_path. Avoiding the global path keeps the
    contract identical to ``tests/conftest.py``'s ``_reset_age_graph`` (which
    explicitly refuses to leak ``ag_catalog`` onto the session) and guarantees
    zero impact on the unqualified ``public`` queries every other command runs.

    Tolerates the bootstrap window where the extension is not yet installed
    (returns ``False`` without loading) so :func:`connect_age` is safe on a
    fresh database. ``LOAD`` is a process-level effect that survives a
    transaction rollback, so the implicit transaction opened under psycopg's
    default ``autocommit=False`` is rolled back here — mirroring
    :func:`connect_raw` — leaving the caller free to flip ``autocommit``
    afterwards.

    Returns ``True`` when AGE was loaded, ``False`` when the extension is
    absent (nothing loaded). Raises :class:`AgeBootstrapError` (never a raw
    ``psycopg.Error``) if the catalog probe or ``LOAD`` fails.
    """
    try:
        installed = _age_extension_installed(conn)
        if installed:
            conn.execute("LOAD 'age'")
    except psycopg.Error as exc:
        # Clear the aborted transaction first so the connection stays usable,
        # then surface a typed bootstrap failure (no raw psycopg.Error escapes).
        if not conn.autocommit:
            conn.rollback()
        raise AgeBootstrapError(f"failed to LOAD Apache AGE: {exc}") from exc
    # Clear the implicit transaction opened by the SELECT (and LOAD) so the
    # caller can still flip autocommit; harmless no-op under autocommit=True.
    if not conn.autocommit:
        conn.rollback()
    return installed


@contextmanager
def connect_age(database_url: str) -> Iterator[psycopg.Connection]:
    """Open a connection with pgvector registered **and** Apache AGE loaded.

    Thin wrapper over :func:`connect` that additionally runs :func:`load_age`,
    so graph callers don't repeat the per-session ``LOAD 'age'`` bootstrap. On a
    fresh database where the ``age`` extension isn't installed yet,
    :func:`load_age` no-ops; the connection is still usable for the relational
    source-of-truth tables.
    """
    with connect(database_url) as conn:
        load_age(conn)
        yield conn


def migrations_dir() -> Path:
    """Path to the packaged migrations directory (``brain/migrations``).

    Resolved via :mod:`importlib.resources` so it works in BOTH editable
    (``pip install -e``) and wheel / pip installs: the SQL files ship inside the
    ``brain`` package itself (see pyproject ``[tool.setuptools.package-data]``),
    not at the repo root. Before 0.2.1 they lived at ``<repo>/migrations`` — a
    sibling of ``src/`` — which setuptools never bundled, so ``brain init``
    globbed an empty directory and applied zero migrations on wheel installs.

    ``brain`` is a regular, never-zip-imported package, so the ``Traversable``
    returned by ``files()`` is already a concrete filesystem path; the ``Path``
    cast is safe and keeps ``.glob`` working at the call site.
    """
    return Path(str(importlib.resources.files("brain").joinpath("migrations")))


_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _table_exists(conn: psycopg.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(conn: psycopg.Connection, table: str, column: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, column),
    ).fetchone()
    return row is not None


def _index_exists(conn: psycopg.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = %s",
        (name,),
    ).fetchone()
    return row is not None


def _seed_applied_migrations(conn: psycopg.Connection) -> None:
    """Detect and record migrations already applied to a pre-existing schema.

    Run on first encounter with a DB that predates ``schema_migrations``. We
    can't run prior migrations against a populated DB without crashing
    (``CREATE TABLE`` collides; ``ALTER TABLE DROP COLUMN`` on 002 would lose
    data), so we infer their applied state from schema artifacts and seed the
    tracking table. Subsequent runs then skip them.

    Detection is conservative: 002 is treated as applied whenever 001 is,
    since 002 is the documented qwen3 backend swap that's been the only
    supported path for any DB old enough to lack ``schema_migrations``.
    """
    if _table_exists(conn, "sources"):
        conn.execute(
            "INSERT INTO schema_migrations (name) VALUES (%s) "
            "ON CONFLICT (name) DO NOTHING",
            ("001_init.sql",),
        )
        conn.execute(
            "INSERT INTO schema_migrations (name) VALUES (%s) "
            "ON CONFLICT (name) DO NOTHING",
            ("002_qwen3_embedding.sql",),
        )
    if _column_exists(conn, "documents", "kind"):
        conn.execute(
            "INSERT INTO schema_migrations (name) VALUES (%s) "
            "ON CONFLICT (name) DO NOTHING",
            ("003_vault_model.sql",),
        )
    if _index_exists(conn, "documents_content_hash_ingested_idx"):
        conn.execute(
            "INSERT INTO schema_migrations (name) VALUES (%s) "
            "ON CONFLICT (name) DO NOTHING",
            ("004_relax_content_hash_uniqueness.sql",),
        )


def run_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply pending migrations in name order. Returns the list newly applied.

    Tracks applied migrations in the ``schema_migrations`` table so each .sql
    file runs at most once. On first run against a pre-existing schema (no
    ``schema_migrations`` table yet), seeds the table from schema state via
    :func:`_seed_applied_migrations` so the prior CREATE TABLE / ALTER COLUMN
    statements aren't re-attempted.
    """
    mdir = migrations_dir()
    migration_files = sorted(mdir.glob("*.sql"))
    if not migration_files:
        # Loud-fail guard: an empty migrations dir means the package was installed
        # without its SQL (the pre-0.2.1 wheel bug). Raise instead of silently
        # applying zero migrations and leaving an empty schema.
        raise BrainError(
            f"No migration files found under {mdir} — the package is installed "
            "incorrectly (migrations were not shipped in the wheel). Reinstall "
            "from a build that includes src/brain/migrations/*.sql."
        )

    conn.execute(_SCHEMA_MIGRATIONS_DDL)
    seeded_row = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
    assert seeded_row is not None  # count(*) always yields one row
    if int(seeded_row[0]) == 0:
        _seed_applied_migrations(conn)

    rows = conn.execute("SELECT name FROM schema_migrations").fetchall()
    applied_names = {str(r[0]) for r in rows}

    applied: list[str] = []
    for sql_file in migration_files:
        if sql_file.name in applied_names:
            continue
        conn.execute(sql_file.read_text())
        conn.execute(
            "INSERT INTO schema_migrations (name) VALUES (%s)",
            (sql_file.name,),
        )
        applied.append(sql_file.name)
    return applied


def age_extension_available(conn: psycopg.Connection) -> bool:
    """True iff the ``age`` extension is *installable* in this database.

    Probes ``pg_available_extensions`` — the catalog of extensions whose control
    files are present on the server (i.e. extensions that *can* be
    ``CREATE EXTENSION``-ed) — as opposed to ``pg_extension``, which lists the
    extensions already *installed* (the latter is what
    :func:`_age_extension_installed` checks). On a stock pgvector image (a prod
    DB before the Apache AGE cut-over) ``age`` is absent here, so ``brain init``
    can SKIP the AGE bootstrap instead of crashing on ``CREATE EXTENSION age``
    after the relational migrations have already committed.

    Rolls back the implicit read transaction opened by the SELECT under
    psycopg's default ``autocommit=False`` so the caller's connection stays
    clean (a harmless no-op when the caller is already autocommit, as ``init``
    is) — on BOTH the success and failure paths: if the probe statement itself
    raises on a non-autocommit connection it would otherwise leave the
    transaction aborted, poisoning the caller's connection. Mirrors the
    ``vector``-extension tolerance in :func:`connect_raw`.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'age'"
        ).fetchone()
    except psycopg.Error:
        # Clear the aborted transaction the failed SELECT may have opened on a
        # non-autocommit connection, then re-raise (the probe makes no claim it
        # can swallow DB failures — the caller decides how to surface them).
        if not conn.autocommit:
            conn.rollback()
        raise
    if not conn.autocommit:
        conn.rollback()
    return row is not None


def bootstrap_age(
    conn: psycopg.Connection,
    graph_name: str = DEFAULT_GRAPH_NAME,
) -> bool:
    """Idempotently provision the Apache AGE extension + the canonical graph.

    Invoked by ``brain init`` after :func:`run_migrations`. All steps are
    idempotent / guarded:

    1. ``CREATE EXTENSION IF NOT EXISTS age CASCADE`` — installs the catalog
       functions if absent; no-op otherwise.
    2. ``LOAD 'age'`` — required before ``create_graph`` is callable.
    3. Check ``ag_catalog.ag_graph`` for ``graph_name`` and call
       ``ag_catalog.create_graph`` **only when absent**. ``create_graph`` raises
       ``InvalidSchemaName`` ("graph already exists") on a second call, so the
       existence guard is mandatory (verified against the live AGE image); it is
       what makes re-running ``brain init`` a safe no-op.

    AGE catalog DDL (``CREATE EXTENSION`` / ``create_graph``) does not behave
    well inside an open transaction under psycopg v3, so this **requires
    ``conn.autocommit`` to be True** and raises :class:`BrainError` otherwise.
    ``brain init`` already sets autocommit before running migrations.

    Every AGE catalog reference is fully-qualified (``ag_catalog.*``) and no
    ``search_path`` is mutated, so the surrounding ``init`` work (migrations,
    :func:`ensure_embedding_column`, search backfill) keeps targeting
    ``public``.

    Returns ``True`` when the graph was created by this call, ``False`` when it
    already existed (re-run no-op). Raises :class:`AgeBootstrapError` (never a
    raw ``psycopg.Error``) on any AGE catalog DDL failure; the autocommit
    precondition is a separate, plain :class:`BrainError` (caller bug).

    **Vertex/edge labels and property indexes are intentionally NOT created
    here.** Per the phase split (plan §G0-4 / spec §12), the G0-4
    ``GraphBackend`` owns entity/edge upserts and will create its labels (via
    ``MERGE`` / ``create_vlabel``/``create_elabel``) and the matching per-label
    property indexes (spec §5b: ``tenant_id``, ``entity_uuid``,
    ``canonical_key``, ``CO_OCCURS.weight``) at that point — when G0-3's
    tenantized relational schema and the ``tenant_id`` property contract are in
    place. AGE property indexes target a specific label's backing table, which
    cannot exist before its label is created, so pre-creating empty labels now
    would duplicate G0-4's ownership and risk drift. Deferred deliberately.
    """
    if not conn.autocommit:
        raise BrainError(
            "bootstrap_age requires an autocommit connection — AGE catalog DDL "
            "does not run reliably inside an open transaction under psycopg v3"
        )
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS age CASCADE")
        conn.execute("LOAD 'age'")
        existing = conn.execute(
            "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s",
            (graph_name,),
        ).fetchone()
        if existing is not None:
            return False
        conn.execute("SELECT ag_catalog.create_graph(%s)", (graph_name,))
    except psycopg.Error as exc:
        raise AgeBootstrapError(
            f"failed to bootstrap Apache AGE graph {graph_name!r}: {exc}"
        ) from exc
    return True


def _current_embedding_dim(
    conn: psycopg.Connection, table: str, column: str
) -> int:
    """Return the dim declared in ``<table>.<column>``'s ``vector(N)`` type.

    The table/column are bound as *values* through the ``::regclass`` cast and
    an ``attname`` equality (not interpolated into SQL text), so this query is
    safe for any name. Callers still validate against the allowlist before
    issuing DDL.

    Raises :class:`BrainError` if the column doesn't exist or the type isn't
    a ``vector(N)``. Both are bugs (migrations should always shape the
    column), not user-facing conditions.
    """
    row = conn.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = %s::regclass AND attname = %s",
        (table, column),
    ).fetchone()
    if row is None:
        raise BrainError(
            f"{table}.{column} column not found — run brain init first"
        )
    formatted = str(row[0])
    # format_type returns e.g. ``vector(1024)``; strip + parse the int.
    if not (formatted.startswith("vector(") and formatted.endswith(")")):
        raise BrainError(
            f"unexpected {table}.{column} column type: {formatted!r}"
        )
    return int(formatted[len("vector(") : -1])


def ensure_embedding_column(
    conn: psycopg.Connection,
    embedder: Embedder,
    table: str = "chunks",
    column: str = "embedding",
) -> None:
    """Reconcile a pgvector embedding column's dim with the active embedder.

    Generalized over ``(table, column)`` (default ``chunks.embedding``) so the
    GraphRAG tables can reuse it; ``(table, column)`` is checked against the
    hard-coded allowlist in :mod:`brain.embedding_targets` and every identifier
    is quoted via :class:`psycopg.sql.Identifier` — never string-formatted into
    SQL.

    Idempotent. The contract (applies to whichever ``(table, column)`` is
    passed; the re-added column is **nullable**, matching the migration shape —
    NOT NULL is a separate finalize concern handled by
    :func:`brain.queries.finalize_embedding_index`):

    - Column dim already matches ``embedder.dim`` → no-op.
    - Mismatch with zero non-NULL embeddings → drop + re-add the column at
      ``embedder.dim`` (and drop any leftover HNSW index, which would point at
      a column that's about to disappear). Safe — there are no embeddings to
      lose. Existing rows are preserved; only the (NULL) embedding column is
      rebuilt at the new dim.
    - Mismatch with one or more non-NULL embeddings → raise
      :class:`BrainError` instructing the user to do a destructive reset.
      Switching backends with populated embeddings is intentionally not
      silent; those embeddings would all be invalidated and re-embedding is
      the only correct recovery.

    Called by ``brain init`` after :func:`run_migrations` so ``chunks.embedding``
    always matches the configured backend before any embeddings are written;
    the GraphRAG reconcile path uses it for ``graph_entities.embedding``.
    """
    validate_embedding_target(table, column)
    current_dim = _current_embedding_dim(conn, table, column)
    if current_dim == embedder.dim:
        return

    # Count rows that ACTUALLY hold a vector. Rows whose embedding is NULL
    # (e.g. immediately after migration 002 drops + re-adds the column, or
    # after `brain reembed` ingest of new docs that haven't been embedded)
    # contribute no data we'd lose by resizing.
    populated_sql = sql.SQL(
        "SELECT count(*) FROM {table} WHERE {column} IS NOT NULL"
    ).format(table=sql.Identifier(table), column=sql.Identifier(column))
    row = conn.execute(populated_sql).fetchone()
    assert row is not None  # count(*) always yields one row
    populated = int(row[0])
    if populated > 0:
        raise BrainError(
            f"Embedding column {table}.{column} is vector({current_dim}) but "
            f"BRAIN_EMBEDDER expects vector({embedder.dim}). Switching backends "
            f"with existing embeddings requires a destructive reset. Run: "
            f"docker compose down && rm -rf data/postgres && "
            f"docker compose up -d && brain init && brain reembed"
        )

    index_name = embedding_index_name(table, column)
    with conn.transaction():
        conn.execute(
            sql.SQL("DROP INDEX IF EXISTS {index}").format(
                index=sql.Identifier(index_name)
            )
        )
        conn.execute(
            sql.SQL("ALTER TABLE {table} DROP COLUMN {column}").format(
                table=sql.Identifier(table), column=sql.Identifier(column)
            )
        )
        conn.execute(
            sql.SQL(
                "ALTER TABLE {table} ADD COLUMN {column} vector({dim})"
            ).format(
                table=sql.Identifier(table),
                column=sql.Identifier(column),
                dim=sql.Literal(embedder.dim),
            )
        )


class PersistentConnection:
    """A single long-lived psycopg connection for the MCP server.

    Opened lazily on first :meth:`get` call and reused across all subsequent
    MCP tool calls, eliminating the ~10–30 ms per-call TCP handshake overhead.
    Kept in ``autocommit=True`` mode so write helpers can manage transactions
    explicitly via ``conn.transaction()`` without a wrapping session-level
    transaction blocking them.

    On :class:`psycopg.OperationalError` during a tool call, callers invoke
    :meth:`reconnect` once before retrying; if the reconnect itself fails the
    ``OperationalError`` propagates as ``INTERNAL_ERROR``.

    Intended exclusively for the MCP server — CLI invocations use the
    per-call :func:`connect` context-manager so their connection lifetime
    remains bounded.
    """

    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._conn: psycopg.Connection | None = None

    def get(self) -> psycopg.Connection:
        """Return the live connection, opening it lazily if absent or closed."""
        if self._conn is None or self._conn.closed:
            self._conn = connect_raw(self._url)
            self._conn.autocommit = True
        return self._conn

    def reconnect(self) -> None:
        """Close the current connection (if any) and open a fresh one.

        Raises :class:`psycopg.OperationalError` when the new connection
        cannot be established — callers surface this as ``INTERNAL_ERROR``.
        """
        if self._conn is not None and not self._conn.closed:
            with suppress(psycopg.Error):  # best-effort close; open fresh one below
                self._conn.close()
        self._conn = connect_raw(self._url)
        self._conn.autocommit = True

    def close(self) -> None:
        """Shut down the connection gracefully (called at server teardown)."""
        if self._conn is not None and not self._conn.closed:
            with suppress(psycopg.Error):
                self._conn.close()
        self._conn = None
