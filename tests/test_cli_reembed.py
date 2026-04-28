"""Tests for the ``brain reembed`` CLI command.

Phase 2's migration left ``chunks.embedding`` nullable so that ``brain
reembed`` could backfill it. These tests cover backfill, dry-run, limit,
finalize semantics, idempotence, and crash-resume — using the
:class:`FakeEmbedder` so no real Ollama call is made.
"""
import contextlib
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import patch as upatch

import httpx
import psycopg
from typer.testing import CliRunner

from brain.cli import app
from tests.conftest import FakeEmbedder


@contextlib.contextmanager
def _stub_ollama_for_doctor() -> Iterator[None]:
    """Stub the Ollama daemon for ``brain doctor`` to a 200-OK with the model loaded."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "qwen3-embedding:8b"}]})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    with upatch("brain.cli.httpx.Client", side_effect=factory):
        yield


def _seed_doc(conn: psycopg.Connection, *, content_hash: str = "h-r1") -> str:
    row = conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, %s) RETURNING id::text",
        ("doc", "body", content_hash, "note"),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _seed_null_chunks(
    conn: psycopg.Connection, *, document_id: str, n: int
) -> list[str]:
    """Insert ``n`` chunks with NULL embedding; return their ids."""
    ids: list[str] = []
    for i in range(n):
        row = conn.execute(
            "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
            "VALUES (%s, %s, %s, NULL) RETURNING id::text",
            (document_id, i, f"chunk content {i}"),
        ).fetchone()
        assert row is not None
        ids.append(str(row[0]))
    return ids


def _embedding_column_is_not_null(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT attnotnull FROM pg_attribute "
        "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
    ).fetchone()
    assert row is not None
    return bool(row[0])


def _null_count(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT count(*) FROM chunks WHERE embedding IS NULL"
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_reembed_backfills_null_chunks(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    """All NULL chunks become non-NULL with a 4096-dim vector after reembed."""
    doc_id = _seed_doc(test_db)
    _seed_null_chunks(test_db, document_id=doc_id, n=3)
    patch_embedder(fake_embedder)

    result = CliRunner().invoke(app, ["reembed"])

    assert result.exit_code == 0, result.output
    assert _null_count(test_db) == 0
    # Spot-check that the rows really got 4096-dim vectors written.
    dim_row = test_db.execute(
        "SELECT vector_dims(embedding) FROM chunks LIMIT 1"
    ).fetchone()
    assert dim_row is not None
    assert dim_row[0] == 4096


def test_reembed_dry_run_reports_counts_without_writing(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    """--dry-run prints counts and leaves NULL embeddings untouched."""
    doc_id = _seed_doc(test_db)
    _seed_null_chunks(test_db, document_id=doc_id, n=4)
    patch_embedder(fake_embedder)

    result = CliRunner().invoke(app, ["reembed", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "would embed 4" in result.output
    assert "4 chunk(s) have NULL embedding" in result.output
    assert _null_count(test_db) == 4  # nothing actually written


def test_reembed_limit_respects_cap(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    """--limit 2 of 5 NULL chunks → 2 backfilled, 3 still NULL."""
    doc_id = _seed_doc(test_db)
    _seed_null_chunks(test_db, document_id=doc_id, n=5)
    patch_embedder(fake_embedder)

    result = CliRunner().invoke(app, ["reembed", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert _null_count(test_db) == 3


def test_reembed_idempotent(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    """Re-running after a clean run reports nothing-to-embed and is a no-op."""
    doc_id = _seed_doc(test_db)
    _seed_null_chunks(test_db, document_id=doc_id, n=2)
    patch_embedder(fake_embedder)

    runner = CliRunner()
    first = runner.invoke(app, ["reembed"])
    assert first.exit_code == 0, first.output
    second = runner.invoke(app, ["reembed"])

    assert second.exit_code == 0, second.output
    assert "nothing to embed" in second.output


def test_reembed_no_finalize_when_nulls_remain(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    """With --limit 1 of 5 NULLs, finalize must not fire — 4 still NULL."""
    doc_id = _seed_doc(test_db)
    _seed_null_chunks(test_db, document_id=doc_id, n=5)
    patch_embedder(fake_embedder)

    result = CliRunner().invoke(app, ["reembed", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "finalize skipped" in result.output
    assert "4 chunk(s) still have NULL embedding" in result.output
    assert not _embedding_column_is_not_null(test_db)


def test_reembed_finalize_applies_not_null(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    """Default --finalize applies NOT NULL once backfill is complete."""
    doc_id = _seed_doc(test_db)
    _seed_null_chunks(test_db, document_id=doc_id, n=3)
    patch_embedder(fake_embedder)

    result = CliRunner().invoke(app, ["reembed"])

    assert result.exit_code == 0, result.output
    assert "finalized" in result.output
    assert _embedding_column_is_not_null(test_db)
    # Phase 3 deliberately ships without an HNSW/IVFFlat index — assert
    # finalize did NOT secretly create one.
    idx = test_db.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'chunks_embedding_idx'"
    ).fetchone()
    assert idx is None


def test_reembed_no_finalize_flag_skips_constraint(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    """--no-finalize leaves the column nullable even after backfill completes."""
    doc_id = _seed_doc(test_db)
    _seed_null_chunks(test_db, document_id=doc_id, n=2)
    patch_embedder(fake_embedder)

    result = CliRunner().invoke(app, ["reembed", "--no-finalize"])

    assert result.exit_code == 0, result.output
    assert "finalized" not in result.output
    assert not _embedding_column_is_not_null(test_db)
    # All chunks ARE backfilled, even though the constraint wasn't applied.
    assert _null_count(test_db) == 0


def test_reembed_resumes_after_crash(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    """Regression: a crash mid-backfill (some rows NULL, some not) is recoverable.

    Simulate by ingesting 3 chunks (all populated by ingest), then NULL-ing 2
    of them — exactly the state ``brain reembed`` is designed to recover from.
    """
    doc_id = _seed_doc(test_db)
    # Three chunks, all already embedded with a placeholder vector.
    placeholder = [0.1] * 4096
    for i in range(3):
        test_db.execute(
            "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
            "VALUES (%s, %s, %s, %s)",
            (doc_id, i, f"chunk {i}", placeholder),
        )
    # Now NULL two of them out — the simulated crash state.
    test_db.execute(
        "UPDATE chunks SET embedding = NULL "
        "WHERE id IN (SELECT id FROM chunks LIMIT 2)"
    )
    assert _null_count(test_db) == 2

    patch_embedder(fake_embedder)
    result = CliRunner().invoke(app, ["reembed"])

    assert result.exit_code == 0, result.output
    assert _null_count(test_db) == 0
    assert _embedding_column_is_not_null(test_db)


def test_reembed_on_empty_db_finalizes_immediately(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    """No chunks at all → reembed reports nothing-to-do and still finalizes.

    Edge case: the user runs `brain reembed` on a freshly-migrated, empty DB.
    There are 0 NULL chunks so finalize is allowed to run; the column flips
    to NOT NULL even though there's no data to embed yet. This means future
    inserts must include an embedding (which the ingest pipeline already does).
    """
    patch_embedder(fake_embedder)
    result = CliRunner().invoke(app, ["reembed"])
    assert result.exit_code == 0, result.output
    assert "nothing to embed" in result.output
    assert "finalized" in result.output
    assert _embedding_column_is_not_null(test_db)


def test_reembed_limit_breaks_across_multiple_batches(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    """--limit 2 with --batch-size 1 across 5 NULL chunks: stop after 2nd batch.

    Exercises the ``break`` branch in the reembed loop that fires when
    ``embedded >= limit`` AFTER one or more full batches have already been
    consumed (vs. the slice-truncation branch which fires WITHIN a batch).
    """
    doc_id = _seed_doc(test_db)
    _seed_null_chunks(test_db, document_id=doc_id, n=5)
    patch_embedder(fake_embedder)

    result = CliRunner().invoke(
        app, ["reembed", "--limit", "2", "--batch-size", "1"]
    )

    assert result.exit_code == 0, result.output
    assert _null_count(test_db) == 3


def test_doctor_reports_embedding_not_null_post_finalize(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    """After finalize, ``brain doctor`` reports the embedding column as NOT NULL.

    Covers the post-finalize branch of ``_report_embedding_column``.
    """
    doc_id = _seed_doc(test_db)
    _seed_null_chunks(test_db, document_id=doc_id, n=1)
    patch_embedder(fake_embedder)
    runner = CliRunner()
    runner.invoke(app, ["reembed"])
    assert _embedding_column_is_not_null(test_db)

    with _stub_ollama_for_doctor():
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "embedding" in result.output
    assert "NOT NULL" in result.output


def test_reembed_reports_finalize_failure_and_exits_nonzero(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    """If ``finalize_embedding_index`` raises ValueError, the CLI prints it and exits 1.

    Covers the defensive ValueError catch around the finalize call — a guard
    against a race where some other writer reintroduces NULL embeddings
    between the CLI's pre-check and the actual ALTER.
    """
    doc_id = _seed_doc(test_db)
    _seed_null_chunks(test_db, document_id=doc_id, n=1)
    patch_embedder(fake_embedder)

    def _explode(_conn: psycopg.Connection) -> None:
        raise ValueError("simulated race")

    runner = CliRunner()
    with upatch("brain.cli.finalize_embedding_index", side_effect=_explode):
        result = runner.invoke(app, ["reembed"])

    assert result.exit_code == 1, result.output
    combined = result.output + (result.stderr if result.stderr else "")
    assert "finalize failed" in combined
    assert "simulated race" in combined


def test_doctor_reports_embedding_nullable_pre_finalize(
    test_db: psycopg.Connection,
    patch_embedder: Callable[[object], None],
    fake_embedder: FakeEmbedder,
) -> None:
    """Pre-finalize: doctor's embedding line says ``nullable`` and points at reembed.

    Covers the nullable branch of ``_report_embedding_column``. The
    ``test_db`` fixture re-runs migrations, so the column starts nullable
    (Phase 2's state). ``patch_embedder`` is taken solely to install the
    test-DB URL into the environment for ``Config.load()``.
    """
    patch_embedder(fake_embedder)
    runner = CliRunner()
    with _stub_ollama_for_doctor():
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "embedding" in result.output
    assert "nullable" in result.output
    assert "brain reembed" in result.output
