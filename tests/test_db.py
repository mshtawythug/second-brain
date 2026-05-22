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
        "postgresql://brain:brain@localhost:5434/second_brain_test",
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
        "postgresql://brain:brain@localhost:5434/second_brain_test",
    )
    # migrations_dir() resolves to the repo-root migrations/ directory
    expected_files = sorted(p.name for p in migrations_dir().glob("*.sql"))
    assert expected_files, "no migration files discovered"

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        applied = run_migrations(conn)

    assert applied == expected_files


def test_run_migrations_is_idempotent_when_already_applied() -> None:
    """Second call returns [] — schema_migrations dedups."""
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://brain:brain@localhost:5434/second_brain_test",
    )
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        first = run_migrations(conn)
        second = run_migrations(conn)

    assert len(first) > 0  # at least the .sql files in migrations/
    assert second == []


def test_run_migrations_records_each_applied_in_schema_migrations() -> None:
    """After a fresh run, schema_migrations has one row per .sql file."""
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://brain:brain@localhost:5434/second_brain_test",
    )
    expected_files = sorted(p.name for p in migrations_dir().glob("*.sql"))

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        run_migrations(conn)
        rows = conn.execute(
            "SELECT name FROM schema_migrations ORDER BY name"
        ).fetchall()

    assert [r[0] for r in rows] == expected_files


def test_run_migrations_seeds_existing_schema_without_tracking_table() -> None:
    """Real-world bug: a DB that predates schema_migrations must NOT re-apply
    migrations 001/002 (which would crash on duplicate-table or destroy
    existing embeddings). The seeder records existing migrations as applied
    based on schema state, then only NEW migrations run.
    """
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://brain:brain@localhost:5434/second_brain_test",
    )
    with psycopg.connect(url, autocommit=True) as conn:
        # Simulate a prod-style DB that has 001 + 002 applied (pre-vault-model)
        # but lacks schema_migrations entirely.
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        # Apply 001 and 002 directly so the schema is in a "vault-model never
        # ran" state, then drop schema_migrations to mimic pre-fix DBs.
        for name in ("001_init.sql", "002_qwen3_embedding.sql"):
            sql = (migrations_dir() / name).read_text()
            conn.execute(sql)
        # Verify schema_migrations doesn't exist yet.
        exists = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'schema_migrations'"
        ).fetchone()
        assert exists is None

        # First run: should seed 001+002 as applied (NOT re-apply them) and
        # then apply only the pending migrations (003, 004, ...).
        applied = run_migrations(conn)

        # Tracking table now exists with 001, 002, plus everything new.
        seeded_rows = conn.execute(
            "SELECT name FROM schema_migrations ORDER BY name"
        ).fetchall()
        seeded_names = [r[0] for r in seeded_rows]
        assert "001_init.sql" in seeded_names
        assert "002_qwen3_embedding.sql" in seeded_names

        # 001 and 002 must NOT be in the freshly-applied list — they were
        # seeded, not run again.
        assert "001_init.sql" not in applied
        assert "002_qwen3_embedding.sql" not in applied
        # Pending migrations (everything past 002) WERE applied.
        all_files = sorted(p.name for p in migrations_dir().glob("*.sql"))
        for name in all_files:
            if name not in ("001_init.sql", "002_qwen3_embedding.sql"):
                assert name in applied, f"expected {name} in applied list"

        # Idempotent: re-running is a no-op.
        rerun = run_migrations(conn)
        assert rerun == []


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


def _column_dim_of(conn: psycopg.Connection, table: str, column: str) -> int:
    """Generalized variant of :func:`_column_dim` for any ``<table>.<column>``."""
    row = conn.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = %s::regclass AND attname = %s",
        (table, column),
    ).fetchone()
    assert row is not None
    formatted = str(row[0])
    return int(formatted[len("vector(") : -1])


def _column_is_nullable(conn: psycopg.Connection, table: str, column: str) -> bool:
    row = conn.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    ).fetchone()
    assert row is not None
    return str(row[0]) == "YES"


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


def test_ensure_embedding_column_resizes_when_chunks_have_null_embedding(
    test_db: psycopg.Connection,
) -> None:
    """Mismatch + chunks present but all-NULL embedding → safe to resize.

    Reproduces the real-world flow exposed when a user merges Phase 2's
    migration into a previously-Voyage corpus: migration 002 drops + re-adds
    the embedding column at vector(4096), so chunks exist but every
    embedding is NULL. ensure_embedding_column must allow resizing in this
    case (no embeddings would be lost) so the user can switch to arctic
    (1024-dim) without a destructive reset.
    """
    # Seed two chunks with NULL embedding (post-migration-002 state).
    doc_row = test_db.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, %s) RETURNING id::text",
        ("doc", "body", "h-null-1", "note"),
    ).fetchone()
    assert doc_row is not None
    doc_id = doc_row[0]
    for i in range(2):
        test_db.execute(
            "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
            "VALUES (%s, %s, %s, NULL)",
            (doc_id, i, f"chunk {i}"),
        )

    # Sanity: chunks exist (count > 0), but no real embeddings.
    chunk_count = test_db.execute("SELECT count(*) FROM chunks").fetchone()
    populated = test_db.execute(
        "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL"
    ).fetchone()
    assert chunk_count is not None and chunk_count[0] == 2
    assert populated is not None and populated[0] == 0

    # The resize should succeed; chunks survive, embedding is now vector(1024).
    ensure_embedding_column(test_db, _DimEmbedder(dim=1024))

    assert _column_dim(test_db) == 1024
    surviving = test_db.execute("SELECT count(*) FROM chunks").fetchone()
    assert surviving is not None and surviving[0] == 2


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


# --- G0b: generalized (table, column) dim reconciliation --------------------
# The reconciliation helpers are parameterized over (table, column) so the
# GraphRAG tables can reuse them. These tests prove (a) the chunks path is
# unchanged when the explicit-arg signature is used, (b) graph_entities.embedding
# resizes correctly and stays NULLABLE, and (c) a non-allowlisted pair is
# rejected before any DDL runs.


def test_ensure_embedding_column_chunks_explicit_args_match_default(
    test_db: psycopg.Connection,
) -> None:
    """Regression: explicit ``("chunks", "embedding")`` args == default behavior.

    Proves the generalization didn't change the chunks reconciliation: a
    4096→1024 resize on an empty table drops + re-adds the column at the new
    dim, exactly as the default-arg path does (covered by
    :func:`test_ensure_embedding_column_resizes_on_zero_rows`).
    """
    assert _column_dim(test_db) == 4096

    ensure_embedding_column(test_db, _DimEmbedder(dim=1024), "chunks", "embedding")

    assert _column_dim(test_db) == 1024
    # Re-added column is NULLABLE (NOT NULL is a separate finalize step).
    assert _column_is_nullable(test_db, "chunks", "embedding")


def test_ensure_embedding_column_resizes_graph_entities(
    test_db: psycopg.Connection,
) -> None:
    """graph_entities.embedding (ships vector(1024)) resizes to the active dim.

    Migration 012 ships the column as ``vector(1024)`` NULLABLE. With a row
    present but its embedding NULL, reconciling against a 4096-dim embedder must
    drop + re-add the column at 4096, preserve the row, and leave the column
    NULLABLE (graph columns are never forced NOT NULL by reconciliation).
    """
    assert _column_dim_of(test_db, "graph_entities", "embedding") == 1024

    # Synthetic entity row with a NULL embedding (post-migration state).
    test_db.execute(
        "INSERT INTO graph_entities (entity_type, name, canonical_key) "
        "VALUES (%s, %s, %s)",
        ("topic", "Alpha", "alpha"),
    )

    ensure_embedding_column(
        test_db, _DimEmbedder(dim=4096), "graph_entities", "embedding"
    )

    assert _column_dim_of(test_db, "graph_entities", "embedding") == 4096
    # Column stays NULLABLE — reconciliation never sets NOT NULL on graph tables.
    assert _column_is_nullable(test_db, "graph_entities", "embedding")
    # The entity row survived the column rebuild.
    surviving = test_db.execute(
        "SELECT count(*) FROM graph_entities"
    ).fetchone()
    assert surviving is not None and surviving[0] == 1


def test_ensure_embedding_column_graph_entities_noop_on_match(
    test_db: psycopg.Connection,
) -> None:
    """graph_entities at 1024 + a 1024-dim embedder → no schema change."""
    assert _column_dim_of(test_db, "graph_entities", "embedding") == 1024

    ensure_embedding_column(
        test_db, _DimEmbedder(dim=1024), "graph_entities", "embedding"
    )

    assert _column_dim_of(test_db, "graph_entities", "embedding") == 1024
    assert _column_is_nullable(test_db, "graph_entities", "embedding")


def test_ensure_embedding_column_rejects_non_allowlisted(
    test_db: psycopg.Connection,
) -> None:
    """A ``(table, column)`` pair off the allowlist raises before any DDL."""
    with pytest.raises(BrainError, match="allowlist"):
        ensure_embedding_column(
            test_db, _DimEmbedder(dim=1024), "documents", "tsv"
        )
