"""MCP server exposing the second brain's read tools over stdio."""
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

import psycopg
from mcp import McpError
from mcp.server.fastmcp import FastMCP
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData

from .config import Config
from .db import connect
from .embeddings import VoyageEmbedder
from .ingest import Embedder
from .search import hybrid_search

logger = logging.getLogger("brain.mcp")

# UUID prefixes consist solely of hex digits and hyphens; anything else is
# rejected before reaching SQL so user-supplied `_` / `%` cannot act as LIKE
# wildcards. Mirrors the regex used by `brain.cli._resolve_id`.
_UUID_PREFIX_RE = re.compile(r"[0-9a-f-]+")

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


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


def _resolve_id(conn: psycopg.Connection[Any], prefix: str) -> str:
    """Resolve a UUID prefix (min 6 chars) to a full document id.

    Mirrors the validation rules of ``brain.cli._resolve_id`` but raises
    :class:`McpError` (rather than ``typer.Exit``) so the MCP runtime can
    surface the failure to the caller.
    """
    if len(prefix) < 6:
        raise _mcp_error(INVALID_PARAMS, "id prefix must be at least 6 characters")
    if not _UUID_PREFIX_RE.fullmatch(prefix):
        raise _mcp_error(
            INVALID_PARAMS, "id prefix must contain only hex digits and hyphens"
        )
    rows = conn.execute(
        "SELECT id::text FROM documents WHERE id::text LIKE %s",
        (prefix + "%",),
    ).fetchall()
    if not rows:
        raise _mcp_error(INVALID_PARAMS, f"document not found: {prefix}")
    if len(rows) > 1:
        raise _mcp_error(INVALID_PARAMS, f"id prefix ambiguous: {prefix}")
    return str(rows[0][0])


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
        raise _mcp_error(INTERNAL_ERROR, f"database error: {e}") from e
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
            row = conn.execute(
                """
                SELECT d.id::text, d.title, d.content, d.content_type, d.tags,
                       d.source_path, d.ingested_at, s.kind
                FROM documents d
                LEFT JOIN sources s ON s.id = d.source_id
                WHERE d.id = %s
                """,
                (doc_id,),
            ).fetchone()
    except psycopg.Error as e:
        raise _mcp_error(INTERNAL_ERROR, f"database error: {e}") from e
    assert row is not None  # _resolve_id confirmed the doc exists
    return {
        "id": row[0],
        "title": row[1],
        "content": row[2],
        "content_type": row[3],
        "tags": list(row[4] or []),
        "source_path": row[5],
        "ingested_at": row[6].isoformat() if row[6] is not None else None,
        "source_kind": row[7],
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
    try:
        with connect(state.cfg.database_url) as conn:
            rows = conn.execute(sql, params).fetchall()
    except psycopg.Error as e:
        raise _mcp_error(INTERNAL_ERROR, f"database error: {e}") from e
    return [
        {
            "id": r[0],
            "title": r[1],
            "content_type": r[2],
            "tags": list(r[3] or []),
            "source_kind": r[4],
            "ingested_at": r[5].isoformat() if r[5] is not None else None,
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
            doc_row = conn.execute("SELECT count(*) FROM documents").fetchone()
            chunk_row = conn.execute("SELECT count(*) FROM chunks").fetchone()
            source_row = conn.execute("SELECT count(*) FROM sources").fetchone()
            last_row = conn.execute(
                "SELECT max(ingested_at) FROM documents"
            ).fetchone()
            by_kind = conn.execute(
                "SELECT coalesce(s.kind, 'manual') AS kind, count(*) "
                "FROM documents d LEFT JOIN sources s ON s.id = d.source_id "
                "GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()
    except psycopg.Error as e:
        raise _mcp_error(INTERNAL_ERROR, f"database error: {e}") from e
    last = last_row[0] if last_row else None
    return {
        "documents": doc_row[0] if doc_row else 0,
        "chunks": chunk_row[0] if chunk_row else 0,
        "sources": source_row[0] if source_row else 0,
        "last_ingest": last.isoformat() if last is not None else None,
        "by_kind": [{"kind": k, "count": c} for k, c in by_kind],
    }


def _configure_logging() -> None:
    """Configure stderr logging from the ``BRAIN_MCP_LOG_LEVEL`` env var.

    Defaults to ``INFO``. Unknown values fall back to ``INFO`` and emit a
    warning. Logging always goes to stderr — stdout belongs to the MCP
    JSON-RPC channel.
    """
    raw_level = os.environ.get("BRAIN_MCP_LOG_LEVEL", "INFO").upper()
    if raw_level in _VALID_LOG_LEVELS:
        logging.basicConfig(stream=sys.stderr, level=getattr(logging, raw_level))
    else:
        logging.basicConfig(stream=sys.stderr, level=logging.INFO)
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
