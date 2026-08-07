"""``brain usage`` — is this brain actually being used, and by whom (F7).

``interactions`` (migration 010) and ``search_queries`` (019) have been
populated on every read, rating and search for months, and consumed only by
gap-mining. This module is the read side: it answers "am I using this?",
"which surface and which agent?", and "how often does a search find nothing?"

Everything here is a rollup over data that already exists. No new writes, no
new columns beyond what migrations 024 (``duration_ms``) and 027
(``agent_id``) already added.

Two deliberate choices worth knowing before editing:

**Shared predicates, not re-derived ones.** The zero-result rate uses
:data:`brain.gaps.ZERO_RESULT_PREDICATE_SQL`, the same fragment the gap
detector keys off. A second hand-written copy of "this search failed" would
drift, and the two numbers would disagree with no way to tell which was right.

**``agent_id = NULL`` is never coalesced in SQL.** It surfaces as ``None`` and
is rendered ``(unattributed)`` at the display layer. Collapsing it into a
literal ``'cli'`` in the query would invent an agent that never existed and
make the honest answer unrepresentable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_cls
from typing import Any

import psycopg

from .gaps import ZERO_RESULT_PREDICATE_SQL, canonical_query_key

#: Actions that represent *reading* — the user (or agent) consumed a document.
_READ_ACTIONS = ("opened", "clicked")

#: Actions that represent *feedback* — an explicit judgement about a document.
_FEEDBACK_ACTIONS = ("rated_useful", "rated_irrelevant", "pinned")

#: Label for the ``agent_id IS NULL`` bucket. A display concern, applied here
#: rather than in SQL so the underlying value stays a real ``None``.
UNATTRIBUTED = "(unattributed)"


@dataclass(frozen=True)
class UsageTotals:
    """Headline counters for the window."""

    searches: int
    sessions: int
    opens: int
    feedback: int
    documents_ingested: int
    zero_result: int
    duration_p50_ms: float | None
    duration_p95_ms: float | None

    @property
    def zero_result_rate(self) -> float:
        """Share of searches that found nothing lexically, in ``[0, 1]``."""
        return self.zero_result / self.searches if self.searches else 0.0

    @property
    def read_events(self) -> int:
        return self.searches + self.opens

    @property
    def write_events(self) -> int:
        return self.documents_ingested + self.feedback


@dataclass(frozen=True)
class DailyUsage:
    """One day's activity."""

    day: date_cls
    searches: int
    sessions: int
    opens: int
    zero_result: int


@dataclass(frozen=True)
class SurfaceUsage:
    """Activity attributed to one surface (``cli`` / ``mcp`` / ``wiki``)."""

    surface: str
    searches: int
    opens: int
    feedback: int


@dataclass(frozen=True)
class AgentUsage:
    """Activity attributed to one agent.

    ``agent_id`` is ``None`` for unattributed rows — every pre-027 row, and
    every surface with no ``BRAIN_AGENT_ID`` configured. Use :attr:`label` for
    display.
    """

    agent_id: str | None
    searches: int
    opens: int
    feedback: int

    @property
    def label(self) -> str:
        return self.agent_id if self.agent_id is not None else UNATTRIBUTED


@dataclass(frozen=True)
class QueryCount:
    """One query and how often it ran.

    ``query`` is the raw string; ``canonical`` is the normalized label safe to
    emit in machine-readable output. See :meth:`UsageReport.to_dict`.
    """

    query: str
    canonical: str
    count: int


@dataclass(frozen=True)
class SourceCount:
    """Documents ingested in the window, by source kind."""

    source_kind: str | None
    count: int

    @property
    def label(self) -> str:
        return self.source_kind if self.source_kind is not None else "manual"


@dataclass(frozen=True)
class UsageReport:
    """The whole report. Rendering lives in :mod:`brain.cli_usage`."""

    days: int
    totals: UsageTotals
    daily: list[DailyUsage]
    by_surface: list[SurfaceUsage]
    by_agent: list[AgentUsage]
    top_queries: list[QueryCount]
    ingested_by_source: list[SourceCount]

    def to_dict(self, *, raw_queries: bool = False) -> dict[str, Any]:
        """JSON projection.

        ``raw_queries`` defaults to **False**, and that default is the point.
        Query strings are the most sensitive thing this module touches — they
        record what the user was looking for, which is often more revealing
        than any single document. Machine-readable output therefore carries
        only the normalized label (the same canonicalization ``brain gaps``
        uses for its MCP surface) unless the caller explicitly opts in.

        The counts are identical either way; only the label changes.
        """
        return {
            "days": self.days,
            "totals": {
                "searches": self.totals.searches,
                "sessions": self.totals.sessions,
                "opens": self.totals.opens,
                "feedback": self.totals.feedback,
                "documents_ingested": self.totals.documents_ingested,
                "zero_result": self.totals.zero_result,
                "zero_result_rate": round(self.totals.zero_result_rate, 4),
                "read_events": self.totals.read_events,
                "write_events": self.totals.write_events,
                "duration_p50_ms": self.totals.duration_p50_ms,
                "duration_p95_ms": self.totals.duration_p95_ms,
            },
            "daily": [
                {
                    "day": d.day.isoformat(),
                    "searches": d.searches,
                    "sessions": d.sessions,
                    "opens": d.opens,
                    "zero_result": d.zero_result,
                }
                for d in self.daily
            ],
            "by_surface": [
                {
                    "surface": s.surface,
                    "searches": s.searches,
                    "opens": s.opens,
                    "feedback": s.feedback,
                }
                for s in self.by_surface
            ],
            "by_agent": [
                {
                    "agent_id": a.agent_id,
                    "label": a.label,
                    "searches": a.searches,
                    "opens": a.opens,
                    "feedback": a.feedback,
                }
                for a in self.by_agent
            ],
            "top_queries": [
                {
                    "query": q.query if raw_queries else q.canonical,
                    "count": q.count,
                }
                for q in self.top_queries
            ],
            "ingested_by_source": [
                {"source_kind": s.source_kind, "label": s.label, "count": s.count}
                for s in self.ingested_by_source
            ],
        }


def _totals(
    conn: psycopg.Connection[Any], *, days: int, tenant_id: str
) -> UsageTotals:
    """Headline counters. Three small aggregates rather than one wide join.

    Joining ``search_queries``, ``interactions`` and ``documents`` in a single
    statement would multiply rows across unrelated dimensions and silently
    inflate every count — the classic fan-out. Three scans over indexed
    windows is both correct and cheaper.
    """
    row = conn.execute(
        f"""
        SELECT COUNT(*),
               COUNT(DISTINCT session_id)
                   FILTER (WHERE session_id IS NOT NULL),
               COUNT(*) FILTER (WHERE {ZERO_RESULT_PREDICATE_SQL}),
               percentile_disc(0.50) WITHIN GROUP (ORDER BY duration_ms)
                   FILTER (WHERE duration_ms IS NOT NULL),
               percentile_disc(0.95) WITHIN GROUP (ORDER BY duration_ms)
                   FILTER (WHERE duration_ms IS NOT NULL)
        FROM search_queries
        WHERE tenant_id = %s AND at >= NOW() - make_interval(days => %s)
        """,
        (tenant_id, days),
    ).fetchone()
    assert row is not None  # aggregate always yields one row
    searches, sessions, zero_result, p50, p95 = row

    interactions = conn.execute(
        """
        SELECT COUNT(*) FILTER (WHERE action = ANY(%s)),
               COUNT(*) FILTER (WHERE action = ANY(%s))
        FROM interactions
        WHERE at >= NOW() - make_interval(days => %s)
        """,
        (list(_READ_ACTIONS), list(_FEEDBACK_ACTIONS), days),
    ).fetchone()
    assert interactions is not None
    opens, feedback = interactions

    ingested = conn.execute(
        """
        SELECT COUNT(*) FROM documents
        WHERE ingested_at >= NOW() - make_interval(days => %s)
        """,
        (days,),
    ).fetchone()
    assert ingested is not None

    return UsageTotals(
        searches=int(searches),
        sessions=int(sessions),
        opens=int(opens),
        feedback=int(feedback),
        documents_ingested=int(ingested[0]),
        zero_result=int(zero_result),
        duration_p50_ms=None if p50 is None else float(p50),
        duration_p95_ms=None if p95 is None else float(p95),
    )


def _daily(
    conn: psycopg.Connection[Any], *, days: int, tenant_id: str
) -> list[DailyUsage]:
    """Per-day rollup, newest first.

    Searches and opens come from different tables, so they are gathered
    separately and merged in Python keyed by day. A FULL OUTER JOIN in SQL
    would work but reads far worse for the same result.
    """
    search_rows = conn.execute(
        f"""
        SELECT date_trunc('day', at)::date AS day,
               COUNT(*),
               COUNT(DISTINCT session_id)
                   FILTER (WHERE session_id IS NOT NULL),
               COUNT(*) FILTER (WHERE {ZERO_RESULT_PREDICATE_SQL})
        FROM search_queries
        WHERE tenant_id = %s AND at >= NOW() - make_interval(days => %s)
        GROUP BY 1
        """,
        (tenant_id, days),
    ).fetchall()
    open_rows = conn.execute(
        """
        SELECT date_trunc('day', at)::date AS day, COUNT(*)
        FROM interactions
        WHERE at >= NOW() - make_interval(days => %s) AND action = ANY(%s)
        GROUP BY 1
        """,
        (days, list(_READ_ACTIONS)),
    ).fetchall()

    opens_by_day = {r[0]: int(r[1]) for r in open_rows}
    merged: dict[date_cls, DailyUsage] = {
        r[0]: DailyUsage(
            day=r[0],
            searches=int(r[1]),
            sessions=int(r[2]),
            opens=opens_by_day.get(r[0], 0),
            zero_result=int(r[3]),
        )
        for r in search_rows
    }
    # Days with opens but no searches are real activity and must not vanish.
    for day, count in opens_by_day.items():
        if day not in merged:
            merged[day] = DailyUsage(
                day=day, searches=0, sessions=0, opens=count, zero_result=0
            )
    return sorted(merged.values(), key=lambda d: d.day, reverse=True)


def _by_dimension(
    conn: psycopg.Connection[Any],
    *,
    column: str,
    days: int,
    tenant_id: str,
) -> list[tuple[Any, int, int, int]]:
    """Searches / opens / feedback grouped by ``column`` on both tables.

    ``column`` is a module-controlled identifier (``"source"`` or
    ``"agent_id"``), never user input — every runtime value below is a bound
    ``%s`` parameter. Shared because the surface and agent rollups are the
    same query over a different grouping key, and two copies would drift.
    """
    search_rows = conn.execute(
        f"""
        SELECT {column}, COUNT(*)
        FROM search_queries
        WHERE tenant_id = %s AND at >= NOW() - make_interval(days => %s)
        GROUP BY 1
        """,
        (tenant_id, days),
    ).fetchall()
    interaction_rows = conn.execute(
        f"""
        SELECT {column},
               COUNT(*) FILTER (WHERE action = ANY(%s)),
               COUNT(*) FILTER (WHERE action = ANY(%s))
        FROM interactions
        WHERE at >= NOW() - make_interval(days => %s)
        GROUP BY 1
        """,
        (list(_READ_ACTIONS), list(_FEEDBACK_ACTIONS), days),
    ).fetchall()

    searches = {r[0]: int(r[1]) for r in search_rows}
    interactions = {r[0]: (int(r[1]), int(r[2])) for r in interaction_rows}
    keys = set(searches) | set(interactions)
    out = [
        (key, searches.get(key, 0), *interactions.get(key, (0, 0)))
        for key in keys
    ]
    # Busiest first; the None key sorts last among ties via the label fallback
    # so "(unattributed)" never leads the table.
    out.sort(key=lambda t: (-(t[1] + t[2]), str(t[0] or "~")))
    return out


def _top_queries(
    conn: psycopg.Connection[Any], *, days: int, tenant_id: str, limit: int
) -> list[QueryCount]:
    rows = conn.execute(
        """
        SELECT query, COUNT(*) AS n
        FROM search_queries
        WHERE tenant_id = %s AND at >= NOW() - make_interval(days => %s)
        GROUP BY query
        ORDER BY n DESC, query
        LIMIT %s
        """,
        (tenant_id, days, limit),
    ).fetchall()
    return [
        QueryCount(
            query=str(r[0]), canonical=canonical_query_key(str(r[0])), count=int(r[1])
        )
        for r in rows
    ]


def _ingested_by_source(
    conn: psycopg.Connection[Any], *, days: int
) -> list[SourceCount]:
    rows = conn.execute(
        """
        SELECT s.kind, COUNT(*)
        FROM documents d
        LEFT JOIN sources s ON s.id = d.source_id
        WHERE d.ingested_at >= NOW() - make_interval(days => %s)
        GROUP BY s.kind
        ORDER BY 2 DESC, 1
        """,
        (days,),
    ).fetchall()
    return [
        SourceCount(source_kind=None if r[0] is None else str(r[0]), count=int(r[1]))
        for r in rows
    ]


def build_usage_report(
    conn: psycopg.Connection[Any],
    *,
    days: int,
    tenant_id: str = "default",
    limit: int = 10,
) -> UsageReport:
    """Assemble the usage report for the trailing ``days`` window.

    Raises :class:`psycopg.errors.UndefinedTable` /
    :class:`~psycopg.errors.UndefinedColumn` on a DB that has not run the
    relevant migrations. That is deliberate: unlike the *write* path — where
    telemetry must never break the daily-driver search — a report that
    silently omitted a whole table would present a confident wrong number.
    The CLI maps those to the ``brain init`` hint via
    :func:`brain.gaps.search_queries_schema_hint`.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1 (got {days})")
    if limit < 1:
        raise ValueError(f"limit must be >= 1 (got {limit})")

    return UsageReport(
        days=days,
        totals=_totals(conn, days=days, tenant_id=tenant_id),
        daily=_daily(conn, days=days, tenant_id=tenant_id),
        by_surface=[
            SurfaceUsage(surface=str(k or "unknown"), searches=s, opens=o, feedback=f)
            for k, s, o, f in _by_dimension(
                conn, column="source", days=days, tenant_id=tenant_id
            )
        ],
        by_agent=[
            AgentUsage(
                agent_id=None if k is None else str(k),
                searches=s,
                opens=o,
                feedback=f,
            )
            for k, s, o, f in _by_dimension(
                conn, column="agent_id", days=days, tenant_id=tenant_id
            )
        ],
        top_queries=_top_queries(
            conn, days=days, tenant_id=tenant_id, limit=limit
        ),
        ingested_by_source=_ingested_by_source(conn, days=days),
    )
