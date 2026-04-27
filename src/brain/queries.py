"""Shared read-side SQL helpers used by both the CLI and the MCP server.

This module exists to avoid duplicating identical SELECTs and prefix-resolution
logic across two callers. The helpers raise plain :mod:`brain.errors`
exceptions so each caller can map them to its own framework's error type.
"""
import re
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
