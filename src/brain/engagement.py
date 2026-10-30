"""Engagement readout over ``interactions``: is there enough data to learn from (RM-22a).

``brain usage --engagement`` prints these numbers. RM-22b (an engagement
ranking stage, default off) is built only once :func:`engagement_gate` says the
gate is met, and RM-58 (pruning ``interactions``) reads the table-size figures.
Nothing here changes ranking.

Five rulings, each reached by reading the writers rather than the column names
(``brain_show``, ``brain_ask``, ``brain_rate`` / ``brain rate``, and
``brain.ui.telemetry.record_ui_open``):

**The gate counts ``reads``: ``opened`` + ``clicked``.** That total is what
``brain usage``'s own report calls "opens" (``usage._READ_ACTIONS``). The
readout's ``opens`` and ``clicks`` split it: ``opens`` is ``opened`` only, and
``clicks`` is ``clicked`` only. Nothing writes ``clicked`` since the wiki was
deleted, but the action is allowed, so it is counted.

**Reads include ``brain ask``'s citations.** ``brain ask`` (CLI and MCP) records
one ``opened`` row for each citation in an answer, so those rows count toward
the gate's reads and documents. They are kept: the owner did read the answer
built from them. The search-session share is how to tell the two kinds apart,
because an ask citation is logged under the ask's own session, never a search's.

**Every count is over document rows**, except ``interactions_rows`` and
``interactions_bytes``, which describe the whole table for RM-58. RM-22b ranks
documents, so a rating of a graph target (entity, community, theme) is not data
it can learn from.

**"From a search session" means the row's ``session_id`` names a logged
search.** ``session_id IS NOT NULL`` alone is not enough: ``brain_ask`` logs
its citations under the ask's own session id, and no ``search_queries`` row
carries it.

**Counts only.** No title, query, document id or agent leaves this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

#: The gate's window. It is fixed, whatever ``--days`` says, so the gate
#: cannot be met by widening a window: 90 days of activity is recent enough to
#: describe how the brain is used now, and long enough to smooth out a quiet
#: week (RM-22a.D1).
GATE_WINDOW_DAYS = 90

#: Read events needed. RM-22b's boost is ``log1p`` of decayed opens, so a few
#: dozen opens move almost nothing; 500 over 90 days (about five a day) is the
#: point where the signal is larger than one busy afternoon (RM-22a.D1).
GATE_MIN_OPENS = 500

#: Distinct documents those reads must span. Without it, 500 opens of the
#: same handful of notes would meet the gate while teaching the stage only
#: to boost those few — the rich-get-richer risk RM-22b names (RM-22a.D1).
GATE_MIN_DOCUMENTS = 100

#: Explicit ratings (useful + irrelevant) needed. RM-22b's β term is
#: ``useful − irrelevant``; below about 30 ratings over 90 days, one rating is
#: a large share of the term and the demotion half is mostly noise
#: (RM-22a.D1).
GATE_MIN_RATINGS = 30

#: Actions that are a read of a document: the same pair ``brain usage``
#: counts as opens (``tests/test_engagement.py`` pins the two together).
READ_ACTIONS: tuple[str, ...] = ("opened", "clicked")

#: Explicit judgements: the ONE definition of a rating. ``ratings`` (and so
#: the gate) counts exactly these actions in SQL. ``pinned`` is a bookmark, not
#: a judgement about a document's relevance, so it is not a rating.
RATING_ACTIONS: tuple[str, ...] = ("rated_useful", "rated_irrelevant")


@dataclass(frozen=True)
class SurfaceEngagement:
    """Read events on one surface (``interactions.source``)."""

    surface: str
    opens: int
    clicks: int

    def to_dict(self) -> dict[str, Any]:
        return {"surface": self.surface, "opens": self.opens, "clicks": self.clicks}


@dataclass(frozen=True)
class EngagementStats:
    """Document engagement over a trailing window, plus the table's size."""

    days: int
    opens: int
    clicks: int
    #: Distinct documents with at least one read event in the window.
    documents_opened: int
    rated_useful: int
    rated_irrelevant: int
    #: Explicit ratings: rows whose action is in :data:`RATING_ACTIONS`.
    ratings: int
    #: Read events whose ``session_id`` names a logged search.
    search_session_reads: int
    #: Busiest surface first, then by name.
    by_surface: tuple[SurfaceEngagement, ...]
    #: Every row of ``interactions``, of every shape and age.
    interactions_rows: int
    #: ``pg_total_relation_size('interactions')``: heap, indexes and TOAST.
    interactions_bytes: int

    @property
    def reads(self) -> int:
        """Read events: opens plus clicks. The gate's "opens"."""
        return self.opens + self.clicks

    @property
    def search_session_share(self) -> float | None:
        """The share of reads that came from a search session; ``None`` if none."""
        if self.reads == 0:
            return None
        return self.search_session_reads / self.reads

    def to_dict(self) -> dict[str, Any]:
        share = self.search_session_share
        return {
            "days": self.days,
            "opens": self.opens,
            "clicks": self.clicks,
            "reads": self.reads,
            "documents_opened": self.documents_opened,
            "rated_useful": self.rated_useful,
            "rated_irrelevant": self.rated_irrelevant,
            "ratings": self.ratings,
            "search_session_reads": self.search_session_reads,
            "search_session_share": None if share is None else round(share, 4),
            "by_surface": [row.to_dict() for row in self.by_surface],
            "interactions_rows": self.interactions_rows,
            "interactions_bytes": self.interactions_bytes,
        }


@dataclass(frozen=True)
class EngagementGate:
    """Whether RM-22b's data gate is met, with each count beside its threshold."""

    reads: int
    documents: int
    ratings: int

    @property
    def met(self) -> bool:
        return (
            self.reads >= GATE_MIN_OPENS
            and self.documents >= GATE_MIN_DOCUMENTS
            and self.ratings >= GATE_MIN_RATINGS
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": GATE_WINDOW_DAYS,
            "met": self.met,
            "reads": self.reads,
            "min_reads": GATE_MIN_OPENS,
            "documents": self.documents,
            "min_documents": GATE_MIN_DOCUMENTS,
            "ratings": self.ratings,
            "min_ratings": GATE_MIN_RATINGS,
        }


_TOTALS_SQL = """
    SELECT
        COUNT(*) FILTER (WHERE i.action = 'opened'),
        COUNT(*) FILTER (WHERE i.action = 'clicked'),
        COUNT(DISTINCT i.document_id) FILTER (WHERE i.action = ANY(%(reads)s)),
        COUNT(*) FILTER (WHERE i.action = 'rated_useful'),
        COUNT(*) FILTER (WHERE i.action = 'rated_irrelevant'),
        COUNT(*) FILTER (WHERE i.action = ANY(%(ratings)s)),
        COUNT(*) FILTER (
            WHERE i.action = ANY(%(reads)s) AND ss.session_id IS NOT NULL
        )
    FROM interactions i
    -- The search-session leg is a JOIN, not a sub-select in the FILTER. A
    -- sub-select there cannot become a join, so Postgres runs it as a
    -- SubPlan: correlated (EXISTS), it re-scans search_queries once per read
    -- row (27.6 s on 30k x 30k synthetic rows); uncorrelated (IN), it is
    -- hashed only while the hash is estimated to fit in work_mem, and on
    -- PG 16 defaults it stopped fitting at ~300k searches (> 120 s).
    -- search_queries is never pruned, so every install gets there. DISTINCT
    -- makes ss hold each session id at most once, so the join cannot
    -- duplicate an interaction row: every other count is unchanged, and a
    -- read is counted once however many searches share its session.
    LEFT JOIN (
        SELECT DISTINCT session_id FROM search_queries
        WHERE session_id IS NOT NULL
    ) ss ON ss.session_id = i.session_id
    WHERE i.document_id IS NOT NULL
      AND i.at >= NOW() - make_interval(days => %(days)s)
"""

_BY_SURFACE_SQL = """
    SELECT i.source,
           COUNT(*) FILTER (WHERE i.action = 'opened'),
           COUNT(*) FILTER (WHERE i.action = 'clicked')
    FROM interactions i
    WHERE i.document_id IS NOT NULL
      AND i.action = ANY(%(reads)s)
      AND i.at >= NOW() - make_interval(days => %(days)s)
    GROUP BY i.source
    ORDER BY COUNT(*) DESC, i.source
"""

_TABLE_SIZE_SQL = """
    SELECT (SELECT COUNT(*) FROM interactions),
           pg_total_relation_size('interactions')
"""


def engagement_stats(
    conn: psycopg.Connection[Any], *, days: int = GATE_WINDOW_DAYS
) -> EngagementStats:
    """Count document engagement over the trailing ``days``, and size the table.

    Raises:
        ValueError: ``days`` is below 1.
        psycopg.Error: a query failed (for example, a migration is missing);
            propagated so the CLI can map it.
    """
    if days < 1:
        raise ValueError(f"days must be at least 1, got {days}")
    params = {
        "days": days,
        "reads": list(READ_ACTIONS),
        "ratings": list(RATING_ACTIONS),
    }
    totals = conn.execute(_TOTALS_SQL, params).fetchone()
    assert totals is not None  # an aggregate always returns one row
    surfaces = conn.execute(_BY_SURFACE_SQL, params).fetchall()
    size = conn.execute(_TABLE_SIZE_SQL).fetchone()
    assert size is not None
    opens, clicks, documents, useful, irrelevant, ratings, from_search = totals
    return EngagementStats(
        days=days,
        opens=int(opens),
        clicks=int(clicks),
        documents_opened=int(documents),
        rated_useful=int(useful),
        rated_irrelevant=int(irrelevant),
        ratings=int(ratings),
        search_session_reads=int(from_search),
        by_surface=tuple(
            SurfaceEngagement(surface=str(r[0]), opens=int(r[1]), clicks=int(r[2]))
            for r in surfaces
        ),
        interactions_rows=int(size[0]),
        interactions_bytes=int(size[1]),
    )


def engagement_gate(stats: EngagementStats) -> EngagementGate:
    """Judge RM-22b's gate from stats computed over :data:`GATE_WINDOW_DAYS`.

    Raises:
        ValueError: ``stats`` covers any other window. Without this, a caller
            could hand over ``--days 365`` stats and meet the gate by widening
            the window, which the ticket forbids.
    """
    if stats.days != GATE_WINDOW_DAYS:
        raise ValueError(
            f"the engagement gate is judged over {GATE_WINDOW_DAYS} days, "
            f"not {stats.days}"
        )
    return EngagementGate(
        reads=stats.reads, documents=stats.documents_opened, ratings=stats.ratings
    )
