"""MCP server exposing the second brain's tools over stdio."""
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

import psycopg
import voyageai.error
from mcp import McpError
from mcp.server.fastmcp import FastMCP
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData

from .config import Config
from .db import connect
from .embeddings import VoyageEmbedder
from .errors import (
    IdPrefixAmbiguous,
    IdPrefixNotFound,
    IdPrefixNotHex,
    IdPrefixTooShort,
)
from .ingest import (
    Embedder,
    apply_tags,
    ingest_document,
    update_document,
)
from .ingest.stdin import make_doc as _stdin_make_doc
from .queries import (
    fetch_document,
    list_documents,
    resolve_document_prefix,
    summary_counts,
)
from .search import hybrid_search

logger = logging.getLogger("brain.mcp")

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# Tag automatically added to every document ingested via ``brain_ingest_stdin``
# so MCP-saved snippets are distinguishable from CLI-ingested ones.
_MCP_AUTO_TAG = "source-mcp"


@dataclass
class _State:
    """Shared server state initialized once in :func:`main`.

    Tests build this directly and assign to ``_state`` (via
    ``monkeypatch.setattr``) instead of calling :func:`main`.
    """

    cfg: Config
    embedder: Embedder


_state: _State | None = None


def _get_state() -> _State:
    """Return the initialized server state.

    Raises ``AssertionError`` if called before :func:`main` has run (or before
    a test has explicitly populated ``_state``)."""
    assert _state is not None, "mcp_server not initialized — call main() first"
    return _state


def _mcp_error(code: int, message: str) -> McpError:
    """Construct an :class:`McpError` with ``code`` and ``message``."""
    return McpError(ErrorData(code=code, message=message))


def _wrap_db_error(e: psycopg.Error) -> McpError:
    """Wrap a Postgres failure as an MCP error.

    The user-facing message intentionally omits ``str(e)`` (which can include
    SQL fragments + connection details) and exposes only the exception class
    name. The full exception is logged to stderr so we can still debug.
    """
    logger.error("database error", exc_info=e)
    return _mcp_error(INTERNAL_ERROR, f"database error: {type(e).__name__}")


def _wrap_voyage_error(e: voyageai.error.VoyageError) -> McpError:
    """Wrap a Voyage embedding failure as an MCP error.

    Mirrors :func:`_wrap_db_error`: the user-facing message exposes only the
    exception class name; the full exception is logged to stderr.
    """
    logger.error("embedding failed", exc_info=e)
    return _mcp_error(INTERNAL_ERROR, f"embedding failed: {type(e).__name__}")


def _resolve_id(conn: psycopg.Connection[Any], prefix: str) -> str:
    """Resolve a UUID prefix (min 6 chars) to a full document id.

    Thin wrapper around :func:`brain.queries.resolve_document_prefix` that
    maps its plain exceptions to ``McpError`` so the MCP runtime can surface
    the failure to the caller.
    """
    try:
        return resolve_document_prefix(conn, prefix)
    except (
        IdPrefixTooShort,
        IdPrefixNotHex,
        IdPrefixNotFound,
        IdPrefixAmbiguous,
    ) as e:
        raise _mcp_error(INVALID_PARAMS, str(e)) from e


mcp_app: FastMCP = FastMCP(name="brain")


@mcp_app.tool()
def brain_search(
    query: str,
    limit: int = 5,
    source: str | None = None,
    tag: str | None = None,
    since_days: int | None = None,
    fts_only: bool = False,
) -> list[dict[str, Any]]:
    """Hybrid search across the second brain.

    Returns up to ``limit`` matching documents ranked by RRF over FTS + vector
    cosine similarity. Filter by ``source`` kind, ``tag``, or ``since_days``
    recency. Set ``fts_only=True`` to skip the Voyage embed call.
    """
    state = _get_state()
    logger.debug("brain_search: query=%r limit=%d", query, limit)
    try:
        with connect(state.cfg.database_url) as conn:
            results = hybrid_search(
                conn,
                embedder=state.embedder,
                query=query,
                limit=limit,
                source_kind=source,
                tag=tag,
                since_days=since_days,
                fts_only=fts_only,
            )
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    except voyageai.error.VoyageError as e:
        raise _wrap_voyage_error(e) from e
    return [
        {
            "id": r.document_id,
            "title": r.title,
            "source_kind": r.source_kind,
            "snippet": r.snippet,
            "score": r.score,
            "content_type": r.content_type,
            "tags": r.tags,
        }
        for r in results
    ]


@mcp_app.tool()
def brain_show(id_prefix: str) -> dict[str, Any]:
    """Return the full body and metadata of a single document by id prefix.

    The prefix must be at least 6 hex characters and must uniquely identify a
    document. Raises an MCP error if the prefix is unknown or ambiguous.
    """
    state = _get_state()
    logger.debug("brain_show: id_prefix=%s", id_prefix)
    try:
        with connect(state.cfg.database_url) as conn:
            doc_id = _resolve_id(conn, id_prefix)
            doc = fetch_document(conn, doc_id)
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    assert doc is not None  # _resolve_id confirmed the doc exists
    return {
        "id": doc.id,
        "title": doc.title,
        "content": doc.content,
        "content_type": doc.content_type,
        "tags": doc.tags,
        "source_path": doc.source_path,
        "ingested_at": doc.ingested_at.isoformat() if doc.ingested_at else None,
        "source_kind": doc.source_kind,
    }


@mcp_app.tool()
def brain_list(
    source: str | None = None,
    tag: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List documents in the brain, optionally filtered by source kind and/or tag.

    Returns up to ``limit`` rows ordered most-recently-ingested first. Mirrors
    the JSON output of ``brain list --json``.
    """
    state = _get_state()
    logger.debug("brain_list: source=%s tag=%s limit=%d", source, tag, limit)
    try:
        with connect(state.cfg.database_url) as conn:
            rows = list_documents(conn, source=source, tag=tag, limit=limit)
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    return [
        {
            "id": r.id,
            "title": r.title,
            "content_type": r.content_type,
            "tags": r.tags,
            "source_kind": r.source_kind,
            "ingested_at": r.ingested_at.isoformat() if r.ingested_at else None,
        }
        for r in rows
    ]


@mcp_app.tool()
def brain_status() -> dict[str, Any]:
    """Return summary counts for the brain.

    ``documents`` / ``chunks`` / ``sources`` are total row counts;
    ``last_ingest`` is the most recent ``documents.ingested_at`` (ISO 8601, or
    ``None`` if the brain is empty); ``by_kind`` is a list of
    ``{kind, count}`` pairs.
    """
    state = _get_state()
    logger.debug("brain_status: called")
    try:
        with connect(state.cfg.database_url) as conn:
            counts = summary_counts(conn)
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    return {
        "documents": counts.documents,
        "chunks": counts.chunks,
        "sources": counts.sources,
        "last_ingest": (
            counts.last_ingest.isoformat()
            if counts.last_ingest is not None
            else None
        ),
        "by_kind": [{"kind": k, "count": c} for k, c in counts.by_kind],
    }


@mcp_app.tool()
def brain_ingest_stdin(
    content: str,
    source: str,
    external_id: str,
    title: str,
    content_type: str = "note",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """Save a snippet from the conversation into the brain.

    Same code path as ``brain ingest-stdin`` on the CLI: chunk → embed → store
    with ``content_hash`` dedup and ``(source, external_id)`` source dedup.
    The ``source-mcp`` tag is auto-added to every document ingested through
    this tool so MCP-saved snippets are distinguishable from CLI ingests.
    """
    if not content.strip():
        raise _mcp_error(INVALID_PARAMS, "content is empty")
    state = _get_state()
    user_tags = list(tags or [])
    # Set-union with the auto tag, then back to a list for storage. Sorted so
    # the resulting tag list is deterministic for tests / users.
    merged_tags = sorted({*user_tags, _MCP_AUTO_TAG})
    meta: dict[str, Any] = dict(metadata or {})
    if date:
        meta.setdefault("date", date)
    doc = _stdin_make_doc(
        content=content,
        title=title,
        content_type=content_type,
        metadata=meta,
    )
    logger.debug(
        "brain_ingest_stdin: source=%s external_id=%s title=%s",
        source,
        external_id,
        title,
    )
    try:
        with connect(state.cfg.database_url) as conn:
            conn.autocommit = True
            result = ingest_document(
                conn,
                embedder=state.embedder,
                doc=doc,
                source_kind=source,
                source_external_id=external_id,
                source_metadata=meta,
                tags=merged_tags,
            )
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    except voyageai.error.VoyageError as e:
        raise _wrap_voyage_error(e) from e
    return {
        "document_id": result.document_id,
        "created": result.created,
    }


@mcp_app.tool()
def brain_tag(
    id_prefix: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    """Add and/or remove tags on an existing document.

    At least one of ``add`` / ``remove`` must be non-empty. Returns the
    document's full tag list after the mutation. Re-adding an existing tag is
    a no-op.
    """
    add = add or []
    remove = remove or []
    if not add and not remove:
        raise _mcp_error(INVALID_PARAMS, "expected add or remove tags")
    state = _get_state()
    logger.debug(
        "brain_tag: id_prefix=%s add=%s remove=%s", id_prefix, add, remove
    )
    try:
        with connect(state.cfg.database_url) as conn:
            conn.autocommit = True
            doc_id = _resolve_id(conn, id_prefix)
            tags = apply_tags(conn, doc_id, add=add, remove=remove)
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    return {"document_id": doc_id, "tags": tags}


@mcp_app.tool()
def brain_edit(
    id_prefix: str,
    title: str | None = None,
    content_type: str | None = None,
    content: str | None = None,
    metadata: dict[str, Any] | None = None,
    replace_metadata: bool = False,
) -> dict[str, Any]:
    """Update one of: title, content_type, content, or metadata of a document.

    Body changes (``content``) re-chunk + re-embed; field-only changes are a
    single SQL UPDATE. Metadata defaults to a shallow merge — set
    ``replace_metadata=True`` (with a non-None ``metadata``) to swap the JSONB
    blob entirely. Tag mutations live in :func:`brain_tag` for clean
    separation.
    """
    has_field = any(
        v is not None for v in (title, content_type, content, metadata)
    ) or replace_metadata
    if not has_field:
        raise _mcp_error(INVALID_PARAMS, "no edit fields specified")
    if replace_metadata and metadata is None:
        raise _mcp_error(
            INVALID_PARAMS, "replace_metadata requires metadata"
        )
    state = _get_state()
    logger.debug(
        "brain_edit: id_prefix=%s title?=%s ct?=%s content?=%s meta?=%s replace=%s",
        id_prefix,
        title is not None,
        content_type is not None,
        content is not None,
        metadata is not None,
        replace_metadata,
    )
    embedder = state.embedder if content is not None else None
    try:
        with connect(state.cfg.database_url) as conn:
            conn.autocommit = True
            doc_id = _resolve_id(conn, id_prefix)
            try:
                result = update_document(
                    conn,
                    document_id=doc_id,
                    embedder=embedder,
                    new_title=title,
                    new_content_type=content_type,
                    new_content=content,
                    metadata_patch=metadata,
                    replace_metadata=replace_metadata,
                )
            except ValueError as e:
                raise _mcp_error(INVALID_PARAMS, str(e)) from e
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    except voyageai.error.VoyageError as e:
        raise _wrap_voyage_error(e) from e
    return {
        "document_id": result.document_id,
        "fields_changed": result.fields_changed,
        "rechunked": result.rechunked,
    }


def _configure_logging() -> None:
    """Configure stderr logging from the ``BRAIN_MCP_LOG_LEVEL`` env var.

    Defaults to ``INFO``. Unknown values fall back to ``INFO`` and emit a
    warning. Logging always goes to stderr — stdout belongs to the MCP
    JSON-RPC channel.
    """
    raw_level = os.environ.get("BRAIN_MCP_LOG_LEVEL", "INFO").upper()
    if raw_level in _VALID_LOG_LEVELS:
        level = getattr(logging, raw_level)
        logging.basicConfig(stream=sys.stderr, level=level)
        logger.setLevel(level)
    else:
        logging.basicConfig(stream=sys.stderr, level=logging.INFO)
        logger.setLevel(logging.INFO)
        logger.warning(
            "BRAIN_MCP_LOG_LEVEL=%r is not a valid level; falling back to INFO",
            raw_level,
        )


def main() -> None:
    """Initialize shared state and run the brain-mcp server over stdio."""
    global _state
    _configure_logging()
    cfg = Config.load()
    embedder = VoyageEmbedder(api_key=cfg.voyage_api_key)
    _state = _State(cfg=cfg, embedder=embedder)
    logger.info("brain-mcp starting (stdio transport)")
    mcp_app.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - exercised by integration test
    main()
