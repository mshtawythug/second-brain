"""Postgres connection + migration helpers."""
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

from .errors import BrainError
from .ingest import Embedder


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


def migrations_dir() -> Path:
    """Path to the migrations directory at the repo root."""
    return Path(__file__).parent.parent.parent / "migrations"


def run_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply every .sql file in migrations/ in name order. Returns the list applied."""
    applied: list[str] = []
    for sql_file in sorted(migrations_dir().glob("*.sql")):
        conn.execute(sql_file.read_text())
        applied.append(sql_file.name)
    return applied


def _current_embedding_dim(conn: psycopg.Connection) -> int:
    """Return the dim declared in ``chunks.embedding``'s ``vector(N)`` type.

    Raises :class:`BrainError` if the column doesn't exist or the type isn't
    a ``vector(N)``. Both are bugs (migrations should always shape the
    column), not user-facing conditions.
    """
    row = conn.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
    ).fetchone()
    if row is None:
        raise BrainError("chunks.embedding column not found — run brain init first")
    formatted = str(row[0])
    # format_type returns e.g. ``vector(1024)``; strip + parse the int.
    if not (formatted.startswith("vector(") and formatted.endswith(")")):
        raise BrainError(
            f"unexpected chunks.embedding column type: {formatted!r}"
        )
    return int(formatted[len("vector(") : -1])


def ensure_embedding_column(conn: psycopg.Connection, embedder: Embedder) -> None:
    """Reconcile ``chunks.embedding`` column dim with the active embedder.

    Idempotent. The contract:

    - Column dim already matches ``embedder.dim`` → no-op.
    - Mismatch with zero non-NULL embeddings in ``chunks`` → drop + re-add
      the column at ``embedder.dim`` (and drop any leftover HNSW index,
      which would point at a column that's about to disappear). Safe —
      there are no embeddings to lose. Document/chunk rows are preserved;
      only the (NULL) embedding column is rebuilt at the new dim.
    - Mismatch with one or more non-NULL embeddings → raise
      :class:`BrainError` instructing the user to do a destructive reset.
      Switching backends with populated embeddings is intentionally not
      silent; those embeddings would all be invalidated and re-embedding
      from ``chunks.content`` (via ``brain reembed --all``) is the only
      correct recovery.

    Called by ``brain init`` after :func:`run_migrations` so the column
    always matches the configured backend before any embeddings are
    written.
    """
    current_dim = _current_embedding_dim(conn)
    if current_dim == embedder.dim:
        return

    # Count rows that ACTUALLY hold a vector. Rows whose embedding is NULL
    # (e.g. immediately after migration 002 drops + re-adds the column, or
    # after `brain reembed` ingest of new docs that haven't been embedded)
    # contribute no data we'd lose by resizing.
    row = conn.execute(
        "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL"
    ).fetchone()
    assert row is not None  # count(*) always yields one row
    populated = int(row[0])
    if populated > 0:
        raise BrainError(
            f"Embedding column is vector({current_dim}) but BRAIN_EMBEDDER "
            f"expects vector({embedder.dim}). Switching backends with "
            f"existing embeddings requires a destructive reset. Run: "
            f"docker compose down && rm -rf data/postgres && "
            f"docker compose up -d && brain init && brain reembed"
        )

    with conn.transaction():
        conn.execute("DROP INDEX IF EXISTS chunks_embedding_idx")
        conn.execute("ALTER TABLE chunks DROP COLUMN embedding")
        conn.execute(
            f"ALTER TABLE chunks ADD COLUMN embedding vector({embedder.dim})"
        )
