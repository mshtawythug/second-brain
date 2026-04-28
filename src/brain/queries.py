"""Shared read-side SQL helpers used by both the CLI and the MCP server.

This module exists to avoid duplicating identical SELECTs and prefix-resolution
logic across two callers. The helpers raise plain :mod:`brain.errors`
exceptions so each caller can map them to its own framework's error type.
"""
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from .errors import (
    IdPrefixAmbiguous,
    IdPrefixNotFound,
    IdPrefixNotHex,
    IdPrefixTooShort,
)
from .ingest import Embedder

# pgvector 0.8.x caps HNSW (and IVFFlat) at 2000 dims for ``vector`` and 4000
# for ``halfvec``. Backends with native dims at or below this limit get an
# HNSW cosine index at finalize time; higher-dim backends (Qwen3 at 4096)
# use sequential scan, acceptable at personal-corpus scale.
_PGVECTOR_HNSW_DIM_CAP = 2000

# UUID prefixes consist solely of hex digits and hyphens; anything else is
# rejected before reaching SQL so user-supplied `_` / `%` cannot act as LIKE
# wildcards.
_UUID_PREFIX_RE = re.compile(r"[0-9a-f-]+")
_MIN_PREFIX_LEN = 6


@dataclass
class DocumentRow:
    """A document row joined with its source kind, used by show + list views."""

    id: str
    title: str
    content_type: str
    tags: list[str]
    source_kind: str | None
    ingested_at: datetime | None
    # Show-only fields. Populated by :func:`fetch_document`; ``None`` after
    # :func:`list_documents` (which returns the lighter projection).
    content: str | None = None
    source_path: str | None = None


def resolve_document_prefix(conn: psycopg.Connection[Any], prefix: str) -> str:
    """Resolve a UUID prefix (min 6 chars) to a full document id.

    Raises :class:`IdPrefixTooShort`, :class:`IdPrefixNotHex`,
    :class:`IdPrefixNotFound`, or :class:`IdPrefixAmbiguous` so the caller can
    map to its framework's user-facing error type.
    """
    if len(prefix) < _MIN_PREFIX_LEN:
        raise IdPrefixTooShort("id prefix must be at least 6 characters")
    if not _UUID_PREFIX_RE.fullmatch(prefix):
        raise IdPrefixNotHex(
            "id prefix must contain only hex digits and hyphens"
        )
    rows = conn.execute(
        "SELECT id::text FROM documents WHERE id::text LIKE %s",
        (prefix + "%",),
    ).fetchall()
    if not rows:
        raise IdPrefixNotFound(prefix)
    if len(rows) > 1:
        raise IdPrefixAmbiguous(prefix)
    return str(rows[0][0])


def fetch_document(conn: psycopg.Connection[Any], document_id: str) -> DocumentRow | None:
    """Return the full document row for ``document_id`` (or ``None`` if missing).

    Includes the document body and ``source_path``; pair with
    :func:`resolve_document_prefix` when the caller has a prefix instead.
    """
    row = conn.execute(
        """
        SELECT d.id::text, d.title, d.content, d.content_type, d.tags,
               d.source_path, d.ingested_at, s.kind
        FROM documents d
        LEFT JOIN sources s ON s.id = d.source_id
        WHERE d.id = %s
        """,
        (document_id,),
    ).fetchone()
    if row is None:
        return None
    return DocumentRow(
        id=row[0],
        title=row[1],
        content=row[2],
        content_type=row[3],
        tags=list(row[4] or []),
        source_path=row[5],
        ingested_at=row[6],
        source_kind=row[7],
    )


def list_documents(
    conn: psycopg.Connection[Any],
    *,
    source: str | None = None,
    tag: str | None = None,
    limit: int = 20,
) -> list[DocumentRow]:
    """Return up to ``limit`` documents (most-recently-ingested first).

    Optional ``source`` and ``tag`` filters mirror ``brain list``. The returned
    rows omit the document body (``content``) and ``source_path`` to keep the
    projection cheap.
    """
    where = ["TRUE"]
    params: list[Any] = []
    if source:
        where.append("s.kind = %s")
        params.append(source)
    if tag:
        where.append("%s = ANY(d.tags)")
        params.append(tag)
    sql = f"""
        SELECT d.id::text, d.title, d.content_type, d.tags, s.kind, d.ingested_at
        FROM documents d
        LEFT JOIN sources s ON s.id = d.source_id
        WHERE {" AND ".join(where)}
        ORDER BY d.ingested_at DESC
        LIMIT %s
    """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        DocumentRow(
            id=r[0],
            title=r[1],
            content_type=r[2],
            tags=list(r[3] or []),
            source_kind=r[4],
            ingested_at=r[5],
        )
        for r in rows
    ]


@dataclass
class StatusCounts:
    """Aggregate counts for ``brain status`` / ``brain_status``.

    Each caller (CLI vs MCP) formats this into its own response shape.
    """

    documents: int
    chunks: int
    sources: int
    last_ingest: datetime | None
    by_kind: list[tuple[str, int]]


def summary_counts(conn: psycopg.Connection[Any]) -> StatusCounts:
    """Aggregate counts for ``status`` / ``brain_status``. One round-trip per table."""
    doc_row = conn.execute("SELECT count(*) FROM documents").fetchone()
    chunk_row = conn.execute("SELECT count(*) FROM chunks").fetchone()
    source_row = conn.execute("SELECT count(*) FROM sources").fetchone()
    last_row = conn.execute("SELECT max(ingested_at) FROM documents").fetchone()
    by_kind_rows = conn.execute(
        "SELECT coalesce(s.kind, 'manual') AS kind, count(*) "
        "FROM documents d LEFT JOIN sources s ON s.id = d.source_id "
        "GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    assert doc_row is not None  # count(*) always yields one row
    assert chunk_row is not None
    assert source_row is not None
    assert last_row is not None
    return StatusCounts(
        documents=int(doc_row[0]),
        chunks=int(chunk_row[0]),
        sources=int(source_row[0]),
        last_ingest=last_row[0],
        by_kind=[(str(k), int(c)) for k, c in by_kind_rows],
    )


@dataclass
class NullEmbeddingChunk:
    """A chunk whose embedding is NULL and needs backfill."""

    id: str
    content: str


def iter_chunks_missing_embedding(
    conn: psycopg.Connection[Any],
    *,
    batch_size: int = 32,
) -> Iterator[list[NullEmbeddingChunk]]:
    """Yield batches of chunks whose embedding is NULL.

    Uses keyset pagination over ``chunks.id`` (UUID, ordered) so the
    in-memory footprint stays bounded even on a brain with hundreds of
    thousands of chunks — only one batch's content is materialized at a
    time. Keyset (rather than a server-side ``DECLARE CURSOR``) sidesteps
    the autocommit/transaction subtleties that would arise from issuing
    UPDATEs on the same connection while a named cursor is open.

    The iterator advances on ``id`` rather than re-running ``LIMIT N``
    against the NULL-set so that callers which inspect rows *without*
    backfilling them (e.g. tests) still see every NULL row exactly once.
    """
    last_id: str | None = None
    while True:
        if last_id is None:
            rows = conn.execute(
                "SELECT id::text, content FROM chunks "
                "WHERE embedding IS NULL "
                "ORDER BY id LIMIT %s",
                (batch_size,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id::text, content FROM chunks "
                "WHERE embedding IS NULL AND id > %s::uuid "
                "ORDER BY id LIMIT %s",
                (last_id, batch_size),
            ).fetchall()
        if not rows:
            return
        last_id = str(rows[-1][0])
        yield [NullEmbeddingChunk(id=str(r[0]), content=str(r[1])) for r in rows]


def count_chunks_missing_embedding(conn: psycopg.Connection[Any]) -> int:
    """Return the number of chunks whose embedding is NULL."""
    row = conn.execute(
        "SELECT count(*) FROM chunks WHERE embedding IS NULL"
    ).fetchone()
    assert row is not None  # count(*) always yields one row
    return int(row[0])


def finalize_embedding_index(
    conn: psycopg.Connection[Any], embedder: Embedder
) -> None:
    """Apply NOT NULL on ``chunks.embedding`` once backfill is complete.

    For embedders with ``dim <= 2000`` (arctic, voyage), additionally creates
    an HNSW cosine index. pgvector 0.8.x caps HNSW/IVFFlat at 2000 dims for
    ``vector`` (4000 for ``halfvec``), so higher-dim embedders (Qwen3 at
    4096) skip the index — sequential scan over the cosine operator is
    acceptable at personal-corpus scale (~150 ms at 10K chunks, ~1 s at
    100K).

    Idempotent — ``ALTER COLUMN ... SET NOT NULL`` is a no-op if the column
    is already non-nullable, and ``CREATE INDEX IF NOT EXISTS`` is a no-op
    if the index already exists.

    Raises :class:`ValueError` if any chunk still has NULL embedding —
    that's a caller bug (the CLI should only call this after asserting
    ``count_chunks_missing_embedding == 0``).
    """
    remaining = count_chunks_missing_embedding(conn)
    if remaining > 0:
        raise ValueError(
            f"cannot finalize: {remaining} chunk(s) still have NULL embedding"
        )
    with conn.transaction():
        conn.execute("ALTER TABLE chunks ALTER COLUMN embedding SET NOT NULL")
        if embedder.dim <= _PGVECTOR_HNSW_DIM_CAP:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks "
                "USING hnsw (embedding vector_cosine_ops)"
            )


@dataclass
class EmbeddingColumnState:
    """Snapshot of the ``chunks.embedding`` column for ``brain doctor``.

    ``has_index`` reports whether the HNSW cosine index exists. For low-dim
    backends (arctic, voyage) finalize creates it; for Qwen3 (4096 dims) it
    stays absent because pgvector's HNSW cap is 2000.
    """

    column_type: str
    not_null: bool
    has_index: bool


def embedding_column_state(conn: psycopg.Connection[Any]) -> EmbeddingColumnState:
    """Return the current state of the ``chunks.embedding`` column.

    Used by ``brain doctor`` to surface the post-migration / pre-finalize
    state to the user without gating exit code on it.
    """
    col_row = conn.execute(
        "SELECT format_type(atttypid, atttypmod), attnotnull "
        "FROM pg_attribute "
        "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
    ).fetchone()
    assert col_row is not None  # chunks.embedding always exists post-migration
    idx_row = conn.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'chunks_embedding_idx'"
    ).fetchone()
    return EmbeddingColumnState(
        column_type=str(col_row[0]),
        not_null=bool(col_row[1]),
        has_index=idx_row is not None,
    )
