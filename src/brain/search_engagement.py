"""The engagement stage: results the owner opens and rates well rank a little higher (RM-22b).

:class:`EngagementStage` is a :class:`brain.search_pipeline.SearchStage`. For every
row a search returned it reads, in one statement (:func:`fetch_engagement`), the
document's decayed opens and its ratings, turns them into a multiplier
(:func:`engagement_multiplier`, clamped to ``[0.8, 1.25]``), and re-sorts by
multiplier times a positional base (:func:`engagement_order`). It ships **off**
(``BRAIN_ENGAGEMENT_ALPHA=0``, ``BRAIN_ENGAGEMENT_BETA=0``, see
:mod:`brain.settings.engagement`) and is wired once, in
:func:`brain.search_pipeline.build_stages`, **last**.

The rulings (plan ``docs/plans/2026-11-12-rm-22b-engagement-stage.md``):

- **Positional base, not ``SearchResult.score``** (D2). After the reranker, the
  document leg or a ``--rewrite`` fusion, ``score`` is out of step with the order;
  sorting on ``score × multiplier`` would undo them. The base is the row's
  reciprocal rank (k = :data:`~brain.search.RRF_K`), so the order that arrived is
  what the multiplier nudges. ``score`` is never overwritten (RM-14a.D1).
- **Last, after the reranker** (D3): a reranker re-sorts its whole head, which
  would erase a boost applied before it.
- **Whose reads count** (spec AC3, plan D4). A read in the current request's
  session never counts, so a result is not boosted by a click from its own
  search. A read with no session (``brain show --query``, a CLI rating) always
  counts. A read with a session counts only when that session names a logged
  search: ``brain ask`` logs one ``opened`` row per **citation** under the ask's
  own session, and counting those would let the stage feed itself (boost, cite,
  log, boost). That is RM-22a's own rule for "from a search session"
  (:mod:`brain.engagement`).
- **``applied`` even with no data** (D6): a multiplier of 1.0 everywhere is a
  measurement, so ``brain eval`` does not refuse a corpus nobody has opened.
- **It adds no candidates.** It only reorders rows the gated search returned, so
  every filter and the sensitivity lens hold by construction.
"""
from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import psycopg

from .engagement import READ_ACTIONS
from .search import RRF_K, SearchResult
from .search_pipeline import StageOutcome, StageReport

#: The stage's name in ``StageReport``s, the ``stages`` key and timings.
ENGAGEMENT_STAGE_NAME = "engagement"

#: The multiplier's clamp (spec RM-22b): a boost never beats one RRF leg's worth
#: of rank, and a demotion never buries a result.
MIN_MULTIPLIER = 0.8
MAX_MULTIPLIER = 1.25

#: An open's weight halves every 30 days (spec RM-22b).
ENGAGEMENT_HALF_LIFE_DAYS = 30

#: Reads older than ten half-lives (weight < 0.001) are not read at all: it bounds
#: the scan and moves no visible order.
ENGAGEMENT_WINDOW_DAYS = 10 * ENGAGEMENT_HALF_LIFE_DAYS

_SECONDS_PER_DAY = 86400

# One statement, by document id (``interactions_document_at_idx``). No
# sensitivity predicate, deliberately: it reads counts for ids the (gated)
# search already returned and returns no content. It does not filter ``draft``,
# so it is outside the F6 residue grep set; this comment is its recorded reason
# all the same.
#
# GREATEST(age, 0): a future-dated row (clock skew) weighs no more than a fresh one.
# ``IS DISTINCT FROM`` a NULL session (``brain eval``) is true, so it excludes nothing.
_ENGAGEMENT_SQL = """
    SELECT i.document_id::text,
           COALESCE(SUM(
               power(0.5::float8,
                     GREATEST(EXTRACT(EPOCH FROM NOW() - i.at)::float8, 0) / %(half_life_s)s)
           ) FILTER (
               WHERE i.action = ANY(%(reads)s)
                 AND i.at >= NOW() - make_interval(days => %(window_days)s)
                 AND (i.session_id IS NULL
                      OR (i.session_id IS DISTINCT FROM %(session)s::uuid
                          AND EXISTS (SELECT 1 FROM search_queries s
                                      WHERE s.session_id = i.session_id)))
           ), 0),
           COUNT(*) FILTER (WHERE i.action = 'rated_useful'),
           COUNT(*) FILTER (WHERE i.action = 'rated_irrelevant')
    FROM interactions i
    WHERE i.document_id = ANY(%(ids)s::uuid[])
    GROUP BY i.document_id
"""


@dataclass(frozen=True)
class DocEngagement:
    """One document's engagement: decayed reads and undecayed ratings."""

    opens_decayed: float
    useful: int
    irrelevant: int


def fetch_engagement(
    conn: psycopg.Connection[Any],
    document_ids: Sequence[str],
    *,
    session_id: uuid.UUID | None,
) -> dict[str, DocEngagement]:
    """Each document's engagement, keyed by id; a document with no rows is absent."""
    params = {
        "ids": list(document_ids),
        "reads": list(READ_ACTIONS),
        "session": session_id,
        "half_life_s": ENGAGEMENT_HALF_LIFE_DAYS * _SECONDS_PER_DAY,
        "window_days": ENGAGEMENT_WINDOW_DAYS,
    }
    rows = conn.execute(_ENGAGEMENT_SQL, params).fetchall()
    return {
        str(row[0]): DocEngagement(
            opens_decayed=float(row[1]), useful=int(row[2]), irrelevant=int(row[3])
        )
        for row in rows
    }


def engagement_multiplier(found: DocEngagement, *, alpha: float, beta: float) -> float:
    """``clamp(1 + α·log1p(opens) + β·(useful − irrelevant), 0.8, 1.25)``."""
    raw = (
        1.0
        + alpha * math.log1p(found.opens_decayed)
        + beta * (found.useful - found.irrelevant)
    )
    return min(MAX_MULTIPLIER, max(MIN_MULTIPLIER, raw))


def engagement_order(n: int, multipliers: Mapping[int, float]) -> list[int]:
    """The new order of ``n`` rows, as incoming indices.

    Each row's key is ``m / (k + index + 1)``: its reciprocal rank times its
    multiplier (1.0 when absent). Written as one division rather than
    ``m * rrf_contribution(index)`` so an exact tie (``1.25/125`` and ``0.8/80``)
    rounds to the same float and the stable sort keeps the incoming order.
    """
    return sorted(range(n), key=lambda i: -multipliers.get(i, 1.0) / (RRF_K + i + 1))


def engagement_reach(limit: int) -> int:
    """How many rows the stage must see so that any row that could end in the top ``limit`` is seen.

    A row at index ``i`` boosted to the maximum overtakes the row at ``limit − 1``
    demoted to the minimum only while ``i < (MAX/MIN)·(k + limit) − k − 1``.
    Exact arithmetic, so the boundary is not at the mercy of float rounding.
    """
    ratio = Fraction(str(MAX_MULTIPLIER)) / Fraction(str(MIN_MULTIPLIER))
    bound = ratio * (RRF_K + limit) - RRF_K - 1
    return max(limit, math.ceil(bound))


def _session_uuid(session_id: str | None) -> uuid.UUID | None:
    """The request's session; a malformed one can match no row, so it excludes nothing."""
    if session_id is None:
        return None
    try:
        return uuid.UUID(session_id)
    except ValueError:
        return None


@dataclass(frozen=True)
class EngagementStage:
    """Re-sorts a search's rows by how much the owner opened and rated each document."""

    alpha: float
    beta: float
    name: str = ENGAGEMENT_STAGE_NAME

    def candidate_limit(self, limit: int) -> int:
        return engagement_reach(limit)

    def signature(self) -> dict[str, Any]:
        return {"engagement_alpha": self.alpha, "engagement_beta": self.beta}

    def apply(
        self,
        conn: psycopg.Connection[Any],
        *,
        query: str,
        session_id: str | None,
        results: list[SearchResult],
    ) -> StageOutcome:
        del query  # the stage scores the owner's reads, not the query
        if not results:
            return self._outcome(results)
        found = fetch_engagement(
            conn, [r.document_id for r in results], session_id=_session_uuid(session_id)
        )
        multipliers = {
            i: engagement_multiplier(found[r.document_id], alpha=self.alpha, beta=self.beta)
            for i, r in enumerate(results)
            if r.document_id in found
        }
        order = engagement_order(len(results), multipliers)
        return self._outcome([results[i] for i in order])

    def _outcome(self, results: list[SearchResult]) -> StageOutcome:
        # ``ms`` is measured by ``apply_stages``, which times every stage alike.
        report = StageReport(name=self.name, applied=True, skipped_reason=None, ms=0.0)
        return StageOutcome(results=results, report=report)
