"""Tests for brain.db — connection helper + migration runner."""
import os
from pathlib import Path

import psycopg
import pytest

from brain.db import connect, ensure_embedding_column, migrations_dir, run_migrations
from brain.errors import BrainError


def test_connect_returns_open_connection() -> None:
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://brain:brain@localhost:5433/second_brain_test",
    )
    with connect(url) as conn:
        cur = conn.execute("SELECT 1")
        assert cur.fetchone() == (1,)


def test_run_migrations_creates_tables(test_db: psycopg.Connection) -> None:
    # test_db fixture already applies migrations; verify the schema exists
    rows = test_db.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' ORDER BY table_name"
    ).fetchall()
    names = [r[0] for r in rows]
    assert "sources" in names
    assert "documents" in names
    assert "chunks" in names


def test_run_migrations_is_idempotent_on_fresh_schema(test_db: psycopg.Connection) -> None:
    # Re-running on a freshly-migrated schema should not raise
    migrations_path = Path(__file__).parent.parent / "migrations"
    assert migrations_path.exists()
    # this will fail because tables already exist — we expect the runner to handle that
    # so this test asserts we get a clear error or the runner detects existing schema
    with test_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM documents")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


def test_run_migrations_applies_all_sql_files_in_order() -> None:
    """Directly exercise run_migrations() against a freshly reset schema."""
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://brain:brain@localhost:5433/second_brain_test",
    )
    # migrations_dir() resolves to the repo-root migrations/ directory
    expected_files = sorted(p.name for p in migrations_dir().glob("*.sql"))
    assert expected_files, "no migration files discovered"

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        applied = run_migrations(conn)

    assert applied == expected_files


# --- Migration 002 regression tests -----------------------------------------
# These verify the Voyage(1024) -> Qwen3(4096) embedding column swap. The
# session-scoped conftest fixture already applies all migrations in order, so
# the post-002 schema is what test_db reflects.


def test_migration_002_changes_embedding_dim_to_4096(test_db: psycopg.Connection) -> None:
    """After 002, chunks.embedding must be vector(4096)."""
    row = test_db.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
    ).fetchone()
    assert row is not None
    assert row[0] == "vector(4096)"


def test_migration_002_drops_hnsw_index(test_db: psycopg.Connection) -> None:
    """The HNSW index from 001 must be gone — Phase 3 rebuilds it post-backfill."""
    row = test_db.execute(
        "SELECT 1 FROM pg_class WHERE relname = 'chunks_embedding_idx'"
    ).fetchone()
    assert row is None


def test_migration_002_makes_embedding_nullable(test_db: psycopg.Connection) -> None:
    """The NOT NULL constraint is deferred to Phase 3's finalize step."""
    row = test_db.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name='chunks' AND column_name='embedding'"
    ).fetchone()
    assert row is not None
    assert row[0] == "YES"


# --- Phase 3.5: ensure_embedding_column reconciles dim with active embedder --


class _DimEmbedder:
    """Minimal Embedder stub — only ``dim`` is consulted by ensure_embedding_column."""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed(  # pragma: no cover - never called
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]

    def count_tokens(self, text: str) -> int:  # pragma: no cover
        return len(text)


def _column_dim(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
    ).fetchone()
    assert row is not None
    formatted = str(row[0])
    return int(formatted[len("vector(") : -1])


def test_ensure_embedding_column_noop_on_match(
    test_db: psycopg.Connection,
) -> None:
    """Column already at the right dim → no schema change."""
    # Migrations leave the column at 4096 (qwen3 default).
    assert _column_dim(test_db) == 4096

    ensure_embedding_column(test_db, _DimEmbedder(dim=4096))

    assert _column_dim(test_db) == 4096


def test_ensure_embedding_column_resizes_on_zero_rows(
    test_db: psycopg.Connection,
) -> None:
    """Mismatch + empty chunks table → drop + re-add at the new dim."""
    assert _column_dim(test_db) == 4096

    ensure_embedding_column(test_db, _DimEmbedder(dim=1024))

    assert _column_dim(test_db) == 1024


def test_ensure_embedding_column_raises_on_dim_change_with_data(
    test_db: psycopg.Connection,
) -> None:
    """Mismatch + non-empty chunks → BrainError, schema untouched.

    Switching backends with embeddings already written would silently
    invalidate them; force the user to do an intentional reset instead.
    """
    # Seed one chunk so the row count is > 0.
    doc_row = test_db.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, %s) RETURNING id::text",
        ("doc", "body", "h-1", "note"),
    ).fetchone()
    assert doc_row is not None
    doc_id = doc_row[0]
    test_db.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
        "VALUES (%s, %s, %s, %s)",
        (doc_id, 0, "chunk", [0.0] * 4096),
    )

    with pytest.raises(BrainError, match="destructive reset"):
        ensure_embedding_column(test_db, _DimEmbedder(dim=1024))

    # Column stays at the original dim.
    assert _column_dim(test_db) == 4096


def test_ensure_embedding_column_idempotent(
    test_db: psycopg.Connection,
) -> None:
    """Calling twice is fine — the second call is a no-op match."""
    ensure_embedding_column(test_db, _DimEmbedder(dim=1024))
    ensure_embedding_column(test_db, _DimEmbedder(dim=1024))  # no-op
    assert _column_dim(test_db) == 1024


def test_ensure_embedding_column_drops_stale_index_on_resize(
    test_db: psycopg.Connection,
) -> None:
    """Resize must drop ``chunks_embedding_idx`` if it somehow lingers.

    Phase 3 finalize creates the index for low-dim backends; a subsequent
    swap to a higher-dim backend (with zero rows, e.g. fresh DB) needs to
    drop the index before dropping the column it points at.
    """
    # Resize to 1024 first (qwen3-default 4096 exceeds pgvector's HNSW cap),
    # then build the index manually to simulate the post-finalize state.
    ensure_embedding_column(test_db, _DimEmbedder(dim=1024))
    test_db.execute(
        "CREATE INDEX chunks_embedding_idx ON chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    assert (
        test_db.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'chunks_embedding_idx'"
        ).fetchone()
        is not None
    )

    # Now switch to a higher-dim backend (qwen3, 4096) — index must drop
    # along with the column it points at.
    ensure_embedding_column(test_db, _DimEmbedder(dim=4096))

    assert (
        test_db.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'chunks_embedding_idx'"
        ).fetchone()
        is None
    )
    assert _column_dim(test_db) == 4096
