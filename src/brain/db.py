"""Postgres connection + migration helpers."""
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector


@contextmanager
def connect(database_url: str) -> Iterator[psycopg.Connection]:
    """Open a connection with pgvector adapter registered.

    Tolerates the bootstrap case where the `vector` extension has not yet been
    installed (e.g. the first `brain init` on a fresh database). In that case
    the adapter registration is skipped; callers that need vector support
    should open a new connection after `run_migrations`.
    """
    with psycopg.connect(database_url, connect_timeout=10) as conn:
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
        yield conn


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
