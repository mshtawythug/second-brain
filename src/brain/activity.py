"""Time-windowed activity-reader queries (shared: brain brief + brain review weekly).

Single home for the read-only, time-bounded queries that both proactivity
features need: which documents were interacted with in a window
(:func:`iter_activity_docs`), which were ingested in a window
(:func:`iter_ingested_docs`), and which were captured in the last N hours
(:func:`recent_captures`). Plus :func:`week_bounds`, the ISO-week parser.

Import direction is one-way: ``brain.brief`` → ``brain.activity`` and
``brain.review.weekly`` → ``brain.activity``, never the reverse. This module
has no dependency on the brief or weekly-report shapes — it only knows query
shapes — so it stays the neutral shared layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from .queries import DocumentRow
from .sensitivity import not_confidential_sql

# Strict ``YYYY-Www`` shape (e.g. ``2026-W23``). Anchored so a stray suffix or a
# bare year is rejected before reaching ``datetime.fromisocalendar`` (which
# would otherwise raise a less-actionable error).
_ISO_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


@dataclass(frozen=True)
class ActivityDoc:
    """One document interacted with inside an activity window.

    ``interaction_count`` is the number of ``interactions`` rows for this doc in
    the window; ``last_at`` is the most recent of them. ``tags`` is the doc's
    tag array (may be empty).
    """

    document_id: str
    title: str
    interaction_count: int
    last_at: datetime
    tags: list[str]


@dataclass(frozen=True)
class IngestedDoc:
    """One document ingested inside an activity window.

    ``source_kind`` is the joined ``sources.kind`` (``None`` for sourceless
    manual docs). ``tags`` is the doc's tag array (may be empty).
    """

    document_id: str
    title: str
    ingested_at: datetime
    source_kind: str | None
    tags: list[str]


def week_bounds(iso_week: str) -> tuple[datetime, datetime]:
    """Parse ``"YYYY-Www"`` → ``(monday 00:00 UTC, sunday 23:59:59 UTC)``.

    Uses :meth:`datetime.fromisocalendar`, so ISO year-boundaries are correct:
    ``week_bounds("2026-W01")`` → ``2025-12-29 00:00`` .. ``2026-01-04 23:59:59``.

    Raises:
        ValueError: ``iso_week`` is not the strict ``YYYY-Www`` shape, or the
            week number is out of range for the ISO year (e.g. ``2026-W53``).
    """
    match = _ISO_WEEK_RE.match(iso_week)
    if match is None:
        raise ValueError(
            f"week must be in 'YYYY-Www' form (e.g. '2026-W23'); got {iso_week!r}"
        )
    year, week = int(match.group(1)), int(match.group(2))
    # fromisocalendar raises ValueError on week 0 / out-of-range weeks.
    start = datetime.fromisocalendar(year, week, 1).replace(tzinfo=UTC)
    end = datetime.fromisocalendar(year, week, 7).replace(
        hour=23, minute=59, second=59, tzinfo=UTC
    )
    return start, end


def current_iso_week() -> str:
    """Return the current ISO week as ``"YYYY-Www"`` (UTC), e.g. ``"2026-W23"``.

    The default ``--week`` for ``brain review weekly``. Uses ``%G-W%V`` so the
    ISO year + zero-padded ISO week round-trip through :func:`week_bounds`.
    """
    return datetime.now(UTC).strftime("%G-W%V")


def iter_activity_docs(
    conn: psycopg.Connection[Any],
    *,
    after: datetime,
    before: datetime,
    limit: int = 20,
    exclude_confidential: bool = False,
) -> list[ActivityDoc]:
    """Return documents interacted with in ``[after, before]``, busiest first.

    Joins ``interactions`` to ``documents`` over the time window, counts
    interactions per doc, and orders by interaction count (then recency, then
    id) for a deterministic result. Graph-target interaction rows
    (``document_id IS NULL``) are excluded. Returns ``[]`` for an empty window.

    ``exclude_confidential`` (F6) drops confidential documents from the window.
    See :func:`recent_captures` for why this reader needed a gate and which way
    the default points.
    """
    where = ["i.at BETWEEN %s AND %s", "i.document_id IS NOT NULL"]
    if exclude_confidential:
        where.append(not_confidential_sql("d"))
    rows = conn.execute(
        f"""
        SELECT d.id::text, d.title, d.tags,
               COUNT(*) AS interaction_count,
               MAX(i.at) AS last_at
        FROM   interactions i
        JOIN   documents d ON d.id = i.document_id
        WHERE  {' AND '.join(where)}
        GROUP  BY d.id, d.title, d.tags
        ORDER  BY interaction_count DESC, last_at DESC, d.id
        LIMIT  %s
        """,
        (after, before, limit),
    ).fetchall()
    return [
        ActivityDoc(
            document_id=r[0],
            title=r[1],
            tags=list(r[2] or []),
            interaction_count=int(r[3]),
            last_at=r[4],
        )
        for r in rows
    ]


def iter_ingested_docs(
    conn: psycopg.Connection[Any],
    *,
    after: datetime,
    before: datetime,
    limit: int = 10,
    exclude_confidential: bool = False,
) -> list[IngestedDoc]:
    """Return documents ingested in ``[after, before]``, newest first.

    Left-joins ``sources`` so ``source_kind`` is populated (``None`` for manual
    docs without a source row). Ordered by ingest time then id for determinism.
    Returns ``[]`` for an empty window.

    ``exclude_confidential`` (F6) drops confidential documents from the window.
    See :func:`recent_captures` for the rationale and the default's direction.
    """
    where = ["d.ingested_at BETWEEN %s AND %s"]
    if exclude_confidential:
        where.append(not_confidential_sql("d"))
    rows = conn.execute(
        f"""
        SELECT d.id::text, d.title, d.ingested_at, d.tags, s.kind
        FROM   documents d
        LEFT JOIN sources s ON s.id = d.source_id
        WHERE  {' AND '.join(where)}
        ORDER  BY d.ingested_at DESC, d.id
        LIMIT  %s
        """,
        (after, before, limit),
    ).fetchall()
    return [
        IngestedDoc(
            document_id=r[0],
            title=r[1],
            ingested_at=r[2],
            tags=list(r[3] or []),
            source_kind=r[4],
        )
        for r in rows
    ]


def recent_captures(
    conn: psycopg.Connection[Any],
    *,
    since_hours: int,
    limit: int,
    exclude_confidential: bool = False,
) -> list[DocumentRow]:
    """Return docs ingested in the last ``since_hours`` hours, newest first.

    The ``brain brief`` "recent captures" section. Returns the light
    :class:`brain.queries.DocumentRow` projection (no body / summary), mirroring
    :func:`brain.queries.list_documents`. The window is computed in SQL relative
    to ``NOW()`` so the caller need not pass a timestamp.

    ``exclude_confidential`` (F6) drops confidential documents from the window.

    WHY THIS READER NEEDED A GATE. The projection carries no body, which is
    exactly why it was missed — but a time window is an ENUMERATION: the caller
    named no document, so every title returned is one it did not ask for. That is
    the ruling that closed ``brain_orphans`` and ``/api/notes/{id}/links``, and
    :func:`brain.mcp_server.brain_brief` reaches this function with no parameters
    at all.

    It DEFAULTS FALSE — include — because ``brain brief`` at a terminal is the
    owner reading their own corpus and offers no flag to turn a hidden row back
    on. The gate lives at the boundary that has a policy: the MCP layer passes
    ``exclude_confidential=not include_confidential``. Opposite name AND opposite
    default from that layer's ``include_confidential``, so inverting the bridge
    flips the gate while every one-directional test stays green — the permissive
    direction only ever ADDS rows. Both directions are pinned in
    ``tests/test_mcp_listing_confidential.py``.
    """
    where = ["d.ingested_at >= NOW() - (%s * INTERVAL '1 hour')"]
    if exclude_confidential:
        where.append(not_confidential_sql("d"))
    rows = conn.execute(
        f"""
        SELECT d.id::text, d.title, d.content_type, d.tags, s.kind, d.ingested_at
        FROM   documents d
        LEFT JOIN sources s ON s.id = d.source_id
        WHERE  {' AND '.join(where)}
        ORDER  BY d.ingested_at DESC, d.id
        LIMIT  %s
        """,
        (since_hours, limit),
    ).fetchall()
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
