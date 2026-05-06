"""Shared read-side SQL helpers used by both the CLI and the MCP server.

This module exists to avoid duplicating identical SELECTs and prefix-resolution
logic across two callers. The helpers raise plain :mod:`brain.errors`
exceptions so each caller can map them to its own framework's error type.
"""
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml

from .errors import (
    BrainError,
    IdPrefixAmbiguous,
    IdPrefixNotFound,
    IdPrefixNotHex,
    IdPrefixTooShort,
)
from .ingest import Embedder
from .vault.frontmatter import parse_frontmatter

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
    include_embedded: bool = False,
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

    When ``include_embedded=True``, the ``embedding IS NULL`` filter is
    dropped — the iterator yields every chunk in the table. Used by
    ``brain reembed --all`` to re-embed an entire corpus after switching
    embedder backends.
    """
    null_clause = "" if include_embedded else "WHERE embedding IS NULL"
    null_clause_and = "" if include_embedded else "WHERE embedding IS NULL AND"
    last_id: str | None = None
    while True:
        if last_id is None:
            rows = conn.execute(
                f"SELECT id::text, content FROM chunks "
                f"{null_clause} "
                f"ORDER BY id LIMIT %s",
                (batch_size,),
            ).fetchall()
        else:
            cursor_clause = (
                "WHERE id > %s::uuid" if include_embedded else f"{null_clause_and} id > %s::uuid"
            )
            rows = conn.execute(
                f"SELECT id::text, content FROM chunks "
                f"{cursor_clause} "
                f"ORDER BY id LIMIT %s",
                (last_id, batch_size),
            ).fetchall()
        if not rows:
            return
        last_id = str(rows[-1][0])
        yield [NullEmbeddingChunk(id=str(r[0]), content=str(r[1])) for r in rows]


def count_chunks_missing_embedding(
    conn: psycopg.Connection[Any], *, include_embedded: bool = False
) -> int:
    """Return the number of chunks whose embedding is NULL.

    When ``include_embedded=True``, returns the total chunk count instead.
    """
    sql = (
        "SELECT count(*) FROM chunks"
        if include_embedded
        else "SELECT count(*) FROM chunks WHERE embedding IS NULL"
    )
    row = conn.execute(sql).fetchone()
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


def sync_chunk_search_metadata(
    conn: psycopg.Connection[Any], document_id: str
) -> int:
    """Refresh ``chunks.title_text`` and ``chunks.tags_text`` from documents.

    Migration 009 denormalizes ``documents.title`` and ``documents.tags`` onto
    every chunk so the weighted FTS tsvector (title at weight A, tags at
    weight B) stays a single GIN-indexable expression. Anything that mutates
    those two source columns without re-inserting chunks must call this so
    the chunk-side denormalization stays consistent — every title/tag UPDATE
    site in the codebase pairs with a call to this helper.

    The ``IS DISTINCT FROM`` guards on both columns make repeated calls a
    no-op (returns 0). Scoped to one document so the mutation is bounded
    even on a brain with hundreds of thousands of chunks. Returns the
    number of chunk rows actually updated — useful for tests and
    observability.
    """
    cur = conn.execute(
        "UPDATE chunks "
        "SET title_text = d.title, "
        "    tags_text  = array_to_string(d.tags, ' ') "
        "FROM documents d "
        "WHERE chunks.document_id = d.id "
        "  AND chunks.document_id = %s "
        "  AND (chunks.title_text IS DISTINCT FROM d.title "
        "       OR chunks.tags_text IS DISTINCT FROM array_to_string(d.tags, ' '))",
        (document_id,),
    )
    return cur.rowcount or 0


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


# ---------------------------------------------------------------------------
# Mirror drift helpers — used by ``brain doctor`` and
# ``brain vault prune-orphans`` to surface DB-vs-disk drift in the
# ``_ingested/`` mirror tier.
# ---------------------------------------------------------------------------

# Top-level vault subdirectory that holds mirror files for stdin/file-ingested
# documents. Mirrors :mod:`brain.vault.sync._INGESTED_DIR_NAME` — duplicating
# the constant here keeps ``queries`` from importing the heavier
# ``vault.sync`` module just to read a string.
_INGESTED_DIR_NAME = "_ingested"


@dataclass(frozen=True)
class MirrorDriftSummary:
    """Counts of DB↔disk drift in the ``_ingested/`` mirror tier.

    All four counters are independent; a healthy vault has zeros across the
    board. Used by ``brain doctor`` to surface drift the user can act on:

    - ``rows_with_null_vault_path`` → ``brain vault export --force`` to
      re-materialize mirror files for ingest rows missing one.
    - ``orphan_files`` → ``brain vault prune-orphans`` to delete mirror files
      whose document row was removed.
    - ``ghost_rows`` → manual ``brain rm <id>`` for each, or
      ``brain vault export --force`` to re-create the missing files.
    """

    total_ingested_rows: int
    rows_with_null_vault_path: int
    ghost_rows: int  # vault_path set, file missing on disk
    orphan_files: int  # file on disk under _ingested/, no DB row matches its id


def iter_orphan_mirror_files(
    conn: psycopg.Connection[Any], *, vault_path: Path
) -> Iterator[Path]:
    """Yield absolute paths under ``<vault>/_ingested/`` that have no DB row.

    A file is "orphan" iff:

    - It lives under ``<vault>/_ingested/`` (recursive walk).
    - It is a regular ``.md`` file.
    - Its YAML frontmatter parses cleanly and contains a string ``id`` key.
    - That ``id`` value matches no row in ``documents.id``.

    Files without parseable frontmatter (e.g. ``_ingested/README.md`` written
    by ``brain vault init``, or any markdown file the user dropped in
    manually) are skipped — "no frontmatter" is intentional, not orphan.
    Files whose frontmatter has no ``id`` are likewise skipped: pruning would
    silently delete user-authored content.

    Returns a generator so the caller (``brain doctor``) can show a count
    without holding the full path list. Iteration order follows
    :py:meth:`Path.rglob` sort for deterministic output. The corpus is
    bounded (≤10K rows in the personal-brain use case), so we materialize
    every ``documents.id`` into a single in-memory ``set`` to avoid issuing
    one SELECT per file.
    """
    ingested_dir = vault_path / _INGESTED_DIR_NAME
    if not ingested_dir.is_dir():
        return
    rows = conn.execute("SELECT id::text FROM documents").fetchall()
    known_ids = {str(r[0]) for r in rows}
    for path in sorted(ingested_dir.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            frontmatter, _body = parse_frontmatter(text)
        except (ValueError, yaml.YAMLError):
            continue
        doc_id = frontmatter.get("id")
        if not isinstance(doc_id, str) or not doc_id:
            continue
        if doc_id in known_ids:
            continue
        yield path


def iter_stale_mirror_files(
    conn: psycopg.Connection[Any], *, vault_path: Path
) -> Iterator[Path]:
    """Yield mirror files whose id resolves but whose path is not the row's ``vault_path``.

    A file is "stale" iff:

    - It lives under ``<vault>/_ingested/`` (recursive walk).
    - It is a regular ``.md`` file.
    - Its YAML frontmatter parses and contains a string ``id``.
    - That ``id`` matches a row in ``documents``.
    - That row's ``documents.vault_path`` is set to a path **other** than this file.

    These are leftovers from a slug-shape change (e.g. P1.5 reshaped Gmail
    mirrors from ``Mon,-27-Ap-19dd20fb-no-subject.md`` to
    ``2026-04-28-03015b-no-subject.md``). The DB row's ``vault_path`` was
    updated but the old file was not unlinked, so it lingers on disk while
    the new file is the canonical mirror. Stale files don't get deleted by
    :func:`iter_orphan_mirror_files` because their id still resolves; this
    helper catches them.

    Files whose frontmatter has no ``id``, or whose ``id`` doesn't resolve
    to any row, are NOT yielded — those are handled by
    :func:`iter_orphan_mirror_files`. Rows with ``vault_path IS NULL`` are
    treated as not-yet-mirrored and yield no stales (the file at this path
    might be the row's new canonical mirror once the next export runs).
    """
    ingested_dir = vault_path / _INGESTED_DIR_NAME
    if not ingested_dir.is_dir():
        return
    rows = conn.execute(
        "SELECT id::text, vault_path FROM documents WHERE vault_path IS NOT NULL"
    ).fetchall()
    canonical: dict[str, str] = {str(r[0]): str(r[1]) for r in rows}
    for path in sorted(ingested_dir.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            frontmatter, _body = parse_frontmatter(text)
        except (ValueError, yaml.YAMLError):
            continue
        doc_id = frontmatter.get("id")
        if not isinstance(doc_id, str) or not doc_id:
            continue
        canonical_path = canonical.get(doc_id)
        if canonical_path is None:
            continue
        relative = path.relative_to(vault_path).as_posix()
        if relative == canonical_path:
            continue
        yield path


def mirror_drift_summary(
    conn: psycopg.Connection[Any], *, vault_path: Path
) -> MirrorDriftSummary:
    """Compute the four-counter drift snapshot for ``<vault>/_ingested/``.

    Three SQL round-trips (total ingested rows, NULL-vault_path rows, and the
    set of vault_path strings claimed by ingested rows) plus one filesystem
    walk via :func:`iter_orphan_mirror_files`. Safe to call from
    ``brain doctor`` on every invocation — bounded by the corpus size.

    Ghost rows are counted purely against the filesystem: a row whose
    ``vault_path`` points at a file that doesn't exist on disk under
    ``vault_path`` contributes one ghost. We don't dedupe pairs (a single
    DB row with a missing file is one ghost regardless of whether an
    orphan file with the same id happens to also exist — these are
    independent symptoms with different fixes).
    """
    total_row = conn.execute(
        "SELECT count(*) FROM documents WHERE kind = 'ingested'"
    ).fetchone()
    null_row = conn.execute(
        "SELECT count(*) FROM documents "
        "WHERE kind = 'ingested' AND vault_path IS NULL"
    ).fetchone()
    ghost_paths = conn.execute(
        "SELECT vault_path FROM documents "
        "WHERE kind = 'ingested' AND vault_path IS NOT NULL"
    ).fetchall()
    if total_row is None or null_row is None:
        # count(*) always returns one row in practice; guard explicitly so
        # the failure mode survives `python -O` (which strips asserts).
        raise BrainError("count(*) returned no row from documents")

    ghost_count = 0
    for (vp,) in ghost_paths:
        if vp is None:
            continue
        if not (vault_path / str(vp)).is_file():
            ghost_count += 1

    orphan_count = sum(1 for _ in iter_orphan_mirror_files(conn, vault_path=vault_path))

    return MirrorDriftSummary(
        total_ingested_rows=int(total_row[0]),
        rows_with_null_vault_path=int(null_row[0]),
        ghost_rows=ghost_count,
        orphan_files=orphan_count,
    )
