"""Postgres connection + migration helpers."""
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector


@contextmanager
def connect(database_url: str) -> Iterator[psycopg.Connection]:
    """Open a connection with pgvector adapter registered."""
    with psycopg.connect(database_url, connect_timeout=10) as conn:
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
