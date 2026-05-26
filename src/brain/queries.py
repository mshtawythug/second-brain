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
from psycopg import sql

from .embedding_targets import (
    embedding_index_name,
    validate_embedding_target,
)
from .errors import (
    BrainError,
    IdPrefixAmbiguous,
    IdPrefixNotFound,
    IdPrefixNotHex,
    IdPrefixTooShort,
    PersonAmbiguous,
    PersonNotFound,
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
    # Wave Q1-D — per-document auto-summary. Populated by
    # :func:`fetch_document` (so ``brain show`` + MCP ``brain_show`` can
    # surface it); :func:`list_documents` leaves them at ``None`` to keep
    # the listing projection cheap.
    summary: str | None = None
    summary_model: str | None = None
    summary_at: datetime | None = None


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


@dataclass(frozen=True)
class PersonMatch:
    """Resolved person → participant-key list for SQL array-overlap filtering.

    ``keys`` is the lower-cased list of every identifier (display name +
    emails + their ``"Display <email>"`` combination forms) that should
    overlap with a doc's ``documents.participants`` array to count as a
    match. Q1-C uses this to power ``--person`` on ``brain search`` /
    ``brain explain`` / MCP ``brain_search``.
    """

    display_name: str
    keys: list[str]


def _canonicalize_display_name(name: str) -> str:
    """Reduce a display name to a comparison-only canonical form.

    Treats common identity-equivalent variants as the same person:
    ``"person-x person-j"`` and ``"person-x.person-j"`` and ``"person-j"`` and
    ``"person-x  person-j"`` all canonicalize to ``"person-j person-j"``. Used
    inside :func:`resolve_person_to_keys` to dedupe step-3 substring hits
    that point at one logical person stored under several formattings —
    common when directory entries come from both Gmail header names and
    Krisp speaker labels for the same individual.

    The canonical form is NOT used for storage or display — only for
    grouping aggregate_people records inside the resolver. Mirror writes
    keep the original casing.
    """
    folded = name.strip().casefold()
    # Treat dots / underscores / hyphens as word separators so
    # "person-x.person-j" canonicalizes to "person-j person-j".
    for sep in (".", "_", "-"):
        folded = folded.replace(sep, " ")
    # Collapse runs of whitespace (incl. NBSP) to a single space.
    return " ".join(folded.split())


def _expand_person_keys(display_name: str, emails: list[str]) -> list[str]:
    """Build the SQL-overlap key list for one person.

    ``documents.participants`` is a free-form ``TEXT[]`` written by ingest
    extractors. Gmail emits a mix of bare emails (``alice@x.com``) and
    ``"Display <email>"`` combination strings; Krisp emits display names
    only. To match all three forms with a single GIN-friendly ``&&``
    overlap predicate, we expand each person's identity into every form
    they might have been recorded under and let the array operator do
    the union.

    Returns the deduplicated list, sorted for determinism so tests +
    explain payloads stay byte-stable across runs.
    """
    keys: set[str] = set()
    name = (display_name or "").strip().lower()
    if name:
        keys.add(name)
    lowered_emails: list[str] = []
    for raw in emails:
        if not raw:
            continue
        normalized = raw.strip().lower()
        if not normalized:
            continue
        keys.add(normalized)
        lowered_emails.append(normalized)
    # ``Display <email>`` combination form — Gmail emits this for any
    # header where the sender / recipient had a display-name component.
    if name:
        for email in lowered_emails:
            keys.add(f"{name} <{email}>")
    return sorted(keys)


def resolve_person_to_keys(
    conn: psycopg.Connection[Any], name_or_email: str
) -> PersonMatch:
    """Resolve a ``--person`` argument to a participant-key set.

    Resolution order (mirrors ``brain people <name>``):

    1. Exact email match (case-insensitive) against any
       ``directory_entries.email``.
    2. Exact display-name match against ``directory_entries.display_name``
       (case-folded).
    3. Case-insensitive substring on ``display_name``; alpha-first
       tiebreak when multiple records match.

    Per plan §3.b D16, the resolver calls
    :func:`brain.wiki.build_people.aggregate_people` with
    ``min_docs=0`` and ``owner_keys=frozenset()`` so query-time
    ``--person`` filters see every known person — including the corpus
    owner and curated-but-low-doc-count entries that the People Hub UI
    threshold would filter out. The UI's display rules should not gate
    a query filter.

    Raises:
        PersonAmbiguous: Multiple persons matched at step (2) or (3).
            The ``candidates`` attribute carries the top-5 display names
            for caller-side disambiguation messages.
        PersonNotFound: No match at any step.
    """
    # Late import: avoid a top-level dependency on the wiki package so
    # ``brain.queries`` stays import-cheap for non-search code paths.
    from .wiki.build_people import aggregate_people, humanize_display_name

    needle = name_or_email.strip().casefold()
    if not needle:
        raise PersonNotFound(name_or_email)

    records = aggregate_people(
        conn, owner_keys=frozenset(), min_docs=0
    )

    # Step 1 — exact email match.
    for rec in records:
        for email in rec.all_emails:
            if email.casefold() == needle:
                return PersonMatch(
                    display_name=humanize_display_name(rec.display_name),
                    keys=_expand_person_keys(rec.display_name, rec.all_emails),
                )

    def _merge_or_ambiguous(hits: list[Any]) -> PersonMatch | None:
        """Group hits by canonical display name. Single canonical group →
        merge all hit records' keys into one PersonMatch. Multiple canonical
        groups → raise PersonAmbiguous with the deduplicated candidate set.
        Returns None when ``hits`` is empty so callers fall through.

        This is what distinguishes "person-x person-j" + "person-x.person-j" (same
        canonical, one logical person, MERGE) from "John Smith" + "John
        Smith Jr" (different canonicals, two people, AMBIGUOUS).
        """
        if not hits:
            return None
        grouped: dict[str, list[Any]] = {}
        for rec in hits:
            grouped.setdefault(_canonicalize_display_name(rec.display_name), []).append(rec)
        if len(grouped) == 1:
            members = next(iter(grouped.values()))
            merged_emails: list[str] = []
            seen: set[str] = set()
            for rec in members:
                for email in rec.all_emails:
                    if email and email.lower() not in seen:
                        seen.add(email.lower())
                        merged_emails.append(email)
            # Prefer the humanized display name from the alphabetically-first
            # record so output stays deterministic across runs.
            canonical_rec = sorted(members, key=lambda r: r.display_name)[0]
            return PersonMatch(
                display_name=humanize_display_name(canonical_rec.display_name),
                keys=_expand_person_keys(canonical_rec.display_name, merged_emails),
            )
        raise PersonAmbiguous(
            name_or_email,
            sorted(humanize_display_name(members[0].display_name) for members in grouped.values()),
        )

    # Step 2 — canonical-name exact match. Compares the canonical form of
    # each record against the canonical form of the needle so equivalents
    # like ``"person-x person-j"`` / ``"person-x.person-j"`` / ``"person-j"``
    # all match the query ``"person-j person-j"`` at this strict-identity tier.
    canonical_needle = _canonicalize_display_name(name_or_email)
    exact_name_hits = [
        rec
        for rec in records
        if _canonicalize_display_name(rec.display_name) == canonical_needle
    ]
    if (match := _merge_or_ambiguous(exact_name_hits)) is not None:
        return match

    # Step 3 — canonical-name substring match. Same canonicalization rules
    # so a query like ``"person-j"`` catches both ``"person-x person-j"`` and
    # ``"person-x.person-j"`` (both canonicalize to ``"person-j person-j"``, both
    # contain ``"person-j"``). The merge pass then collapses them into one
    # PersonMatch.
    substring_hits = [
        rec
        for rec in records
        if canonical_needle in _canonicalize_display_name(rec.display_name)
    ]
    if (match := _merge_or_ambiguous(substring_hits)) is not None:
        return match

    raise PersonNotFound(name_or_email)


def fetch_document(conn: psycopg.Connection[Any], document_id: str) -> DocumentRow | None:
    """Return the full document row for ``document_id`` (or ``None`` if missing).

    Includes the document body, ``source_path``, and the Q1-D summary
    triple; pair with :func:`resolve_document_prefix` when the caller has
    a prefix instead.
    """
    row = conn.execute(
        """
        SELECT d.id::text, d.title, d.content, d.content_type, d.tags,
               d.source_path, d.ingested_at, s.kind,
               d.summary, d.summary_model, d.summary_at
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
        summary=row[8],
        summary_model=row[9],
        summary_at=row[10],
    )


@dataclass(frozen=True)
class UnenrichedDocument:
    """One row from :func:`iter_unenriched_documents` — the backfill driver.

    Carries just enough state for :class:`brain.enrichment.OllamaEnricher`
    to summarize the doc and for the caller to write the result back. The
    backfill loop reads this in batches via keyset pagination so memory
    stays bounded on a large corpus.
    """

    id: str
    title: str
    content: str


def iter_unenriched_documents(
    conn: psycopg.Connection[Any],
    *,
    batch_size: int = 32,
    current_model: str | None = None,
) -> Iterator[list[UnenrichedDocument]]:
    """Yield batches of documents that need enrichment.

    Uses keyset pagination over ``documents.id`` (same shape as
    :func:`iter_chunks_missing_embedding`) so the in-memory footprint stays
    bounded regardless of corpus size.

    Eligibility:

    - ``current_model is None`` (default) → rows with ``summary IS NULL``
      only. Backed by the partial index ``idx_documents_summary_null``.
    - ``current_model is not None`` → rows with ``summary IS NULL`` OR
      ``summary_model IS DISTINCT FROM current_model``. Lets the backfill
      loop re-enrich after a ``BRAIN_ENRICH_MODEL`` upgrade: an existing
      summary generated by the OLD model is considered stale relative to
      the NEW model, so the new model's improvements propagate to the
      whole corpus. The ``IS DISTINCT FROM`` (vs ``<>``) is critical —
      it matches NULL-summary rows whose ``summary_model`` is also NULL
      without an explicit NULL check.

    The iterator does NOT do any LLM work itself — the backfill loop in
    :func:`brain.cli.enrich` is what calls the enricher and writes
    ``summary`` / ``summary_model`` / ``summary_at`` back. That keeps this
    helper pure / test-friendly.
    """
    if current_model is None:
        base_where = "summary IS NULL"
        params_base: tuple[Any, ...] = ()
    else:
        base_where = (
            "(summary IS NULL OR summary_model IS DISTINCT FROM %s)"
        )
        params_base = (current_model,)

    last_id: str | None = None
    while True:
        if last_id is None:
            rows = conn.execute(
                f"SELECT id::text, title, content FROM documents "
                f"WHERE {base_where} "
                f"ORDER BY id LIMIT %s",
                (*params_base, batch_size),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT id::text, title, content FROM documents "
                f"WHERE {base_where} AND id > %s::uuid "
                f"ORDER BY id LIMIT %s",
                (*params_base, last_id, batch_size),
            ).fetchall()
        if not rows:
            return
        last_id = str(rows[-1][0])
        yield [
            UnenrichedDocument(id=str(r[0]), title=str(r[1]), content=str(r[2]))
            for r in rows
        ]


def count_unenriched_documents(
    conn: psycopg.Connection[Any], *, current_model: str | None = None
) -> int:
    """Return the number of documents that need enrichment.

    Mirrors the eligibility rules of :func:`iter_unenriched_documents`:
    ``current_model is None`` counts ``summary IS NULL`` rows only;
    ``current_model is not None`` also counts rows whose ``summary_model``
    differs from the supplied model (model-upgrade staleness).
    """
    if current_model is None:
        row = conn.execute(
            "SELECT count(*) FROM documents WHERE summary IS NULL"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT count(*) FROM documents "
            "WHERE summary IS NULL "
            "OR summary_model IS DISTINCT FROM %s",
            (current_model,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def list_existing_tags(
    conn: psycopg.Connection[Any], *, min_doc_count: int = 1
) -> list[str]:
    """Return every tag currently on documents, normalized + alpha-sorted.

    Used by ``brain tag --auto`` to feed the LLM the existing tag vocabulary
    so it preferentially proposes from it. Drafts are NOT excluded — they
    can still contribute to vocabulary. Stable alpha sort makes test
    fixtures byte-stable.

    ``min_doc_count`` is the inclusive lower bound on how many docs must
    already carry a tag for it to count. Q1-D ships with the default
    ``min_doc_count=1`` (every tag counts); a future caller can require an
    established tag (``min_doc_count > 1``) without a schema change.
    """
    rows = conn.execute(
        """
        SELECT t, COUNT(*) AS n
        FROM documents, unnest(tags) AS t
        GROUP BY t
        HAVING COUNT(*) >= %s
        ORDER BY t
        """,
        (min_doc_count,),
    ).fetchall()
    return [str(r[0]) for r in rows]


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
    """Aggregate counts for ``status`` / ``brain_status``. Single round-trip via CTE.

    Replaces five sequential SELECTs with one CTE query so ``brain status``
    and the MCP ``brain_status`` tool each pay one network round-trip instead
    of five. The return type (:class:`StatusCounts`) is unchanged.
    """
    row = conn.execute(
        """
        WITH
          doc_stats AS (
            SELECT count(*)            AS doc_count,
                   max(ingested_at)    AS last_ingest
            FROM documents
          ),
          chunk_count AS (
            SELECT count(*) AS n FROM chunks
          ),
          source_count AS (
            SELECT count(*) AS n FROM sources
          ),
          by_kind AS (
            SELECT coalesce(s.kind, 'manual') AS kind,
                   count(*)                    AS n
            FROM documents d
            LEFT JOIN sources s ON s.id = d.source_id
            GROUP BY 1
          )
        SELECT
          (SELECT doc_count   FROM doc_stats),
          (SELECT last_ingest FROM doc_stats),
          (SELECT n           FROM chunk_count),
          (SELECT n           FROM source_count),
          (SELECT json_agg(json_build_array(kind, n) ORDER BY n DESC)
           FROM by_kind)
        """
    ).fetchone()
    assert row is not None  # constant-expression SELECT always yields one row
    doc_count, last_ingest, chunk_count, source_count, by_kind_json = row
    by_kind: list[tuple[str, int]] = (
        [(str(item[0]), int(item[1])) for item in by_kind_json]
        if by_kind_json is not None
        else []
    )
    return StatusCounts(
        documents=int(doc_count),
        chunks=int(chunk_count),
        sources=int(source_count),
        last_ingest=last_ingest,
        by_kind=by_kind,
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


def iter_all_document_ids(
    conn: psycopg.Connection[Any], *, batch_size: int = 256
) -> Iterator[list[str]]:
    """Yield batches of every document id in ascending ``id`` order.

    Drives ``brain graphrag build --backfill`` — the batch equivalent of the
    per-document graph reconcile hook. Uses keyset pagination over
    ``documents.id`` (UUID, ordered) so the in-memory footprint stays bounded on
    a large corpus, mirroring :func:`iter_chunks_missing_embedding` /
    :func:`iter_unenriched_documents`.

    The deterministic ascending-id order is what makes a build resumable: a
    re-run after an interruption revisits the ids in the same order, and the
    reconcile watermark skips the already-indexed prefix cheaply.
    """
    last_id: str | None = None
    while True:
        if last_id is None:
            rows = conn.execute(
                "SELECT id::text FROM documents ORDER BY id LIMIT %s",
                (batch_size,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id::text FROM documents WHERE id > %s::uuid "
                "ORDER BY id LIMIT %s",
                (last_id, batch_size),
            ).fetchall()
        if not rows:
            return
        last_id = str(rows[-1][0])
        yield [str(r[0]) for r in rows]


def count_documents(conn: psycopg.Connection[Any]) -> int:
    """Return the total number of documents (drives build progress reporting)."""
    row = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert row is not None  # count(*) always yields one row
    return int(row[0])


def _count_null_embedding(
    conn: psycopg.Connection[Any], table: str, column: str
) -> int:
    """Count rows in ``<table>`` whose vector ``<column>`` is NULL.

    Identifiers are quoted via :class:`psycopg.sql.Identifier`; callers
    validate ``(table, column)`` against the allowlist before reaching here.
    """
    count_sql = sql.SQL(
        "SELECT count(*) FROM {table} WHERE {column} IS NULL"
    ).format(table=sql.Identifier(table), column=sql.Identifier(column))
    row = conn.execute(count_sql).fetchone()
    assert row is not None  # count(*) always yields one row
    return int(row[0])


def finalize_embedding_index(
    conn: psycopg.Connection[Any],
    embedder: Embedder,
    table: str = "chunks",
    column: str = "embedding",
    *,
    create_hnsw: bool = True,
) -> None:
    """Finalize a pgvector embedding column once its backfill is complete.

    Generalized over ``(table, column)`` (default ``chunks.embedding``) so the
    GraphRAG tables can reuse it; ``(table, column)`` is checked against the
    hard-coded allowlist in :mod:`brain.embedding_targets` and every identifier
    is quoted via :class:`psycopg.sql.Identifier` — never string-formatted.

    Two regimes, selected by ``create_hnsw``:

    - ``create_hnsw=True`` (default — the ``chunks`` semantics, **unchanged**):
      apply ``NOT NULL`` on the column, and for embedders with
      ``dim <= 2000`` (arctic, voyage) additionally create an HNSW cosine
      index. pgvector 0.8.x caps HNSW/IVFFlat at 2000 dims for ``vector``, so
      higher-dim embedders (Qwen3 at 4096) get ``NOT NULL`` but skip the index
      — sequential cosine scan is acceptable at personal-corpus scale.
    - ``create_hnsw=False`` (the GraphRAG semantics, e.g.
      ``graph_entities.embedding``): the column stays **NULLABLE** with **no**
      HNSW index. Small row counts make sequential scan fine (spec §5) and
      global ranking guards on ``IS NOT NULL`` rather than a column
      constraint, so there is nothing to finalize yet — the call is a
      validated no-op.

    Idempotent — ``ALTER COLUMN ... SET NOT NULL`` is a no-op if the column
    is already non-nullable, and ``CREATE INDEX IF NOT EXISTS`` is a no-op
    if the index already exists.

    Raises :class:`ValueError` (only on the ``create_hnsw=True`` path) if any
    row still has a NULL embedding — that's a caller bug (the CLI should only
    call this after asserting the NULL count is zero).
    """
    validate_embedding_target(table, column)
    if not create_hnsw:
        # GraphRAG deferred mode: column stays NULLABLE, no HNSW index, no
        # NULL-completeness requirement. Nothing to finalize.
        return

    remaining = _count_null_embedding(conn, table, column)
    if remaining > 0:
        raise ValueError(
            f"cannot finalize: {remaining} {table}.{column} value(s) "
            f"still NULL"
        )
    index_name = embedding_index_name(table, column)
    with conn.transaction():
        conn.execute(
            sql.SQL("ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL").format(
                table=sql.Identifier(table), column=sql.Identifier(column)
            )
        )
        if embedder.dim <= _PGVECTOR_HNSW_DIM_CAP:
            conn.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {index} ON {table} "
                    "USING hnsw ({column} vector_cosine_ops)"
                ).format(
                    index=sql.Identifier(index_name),
                    table=sql.Identifier(table),
                    column=sql.Identifier(column),
                )
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
