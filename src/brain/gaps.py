"""Search-query logging and search-failure-driven knowledge-gap detection."""
from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import psycopg

from .set_similarity import jaccard

if TYPE_CHECKING:
    from .elicit.schema import Gap

logger = logging.getLogger(__name__)

# Strip punctuation to whitespace before tokenizing so "q3-hiring" and
# "q3 hiring" normalize identically.
_PUNCT_RE = re.compile(r"[^\w\s]")

# Two SQL passes mined by both the read view (``top_search_failures``) and the
# detector (``SearchFailureDetector.detect``). Both are parameterized; the
# lookback window is bound as an integer day-count via ``make_interval``.
_ZERO_RESULT_SQL = """
    SELECT query, COUNT(*) AS n
    FROM search_queries
    WHERE tenant_id = %s
      AND result_count = 0
      AND at >= NOW() - make_interval(days => %s)
    GROUP BY query
    ORDER BY n DESC, query
"""

# No-click: a non-empty result set that the user never opened/clicked within
# the same search session. MCP-only — CLI searches carry ``session_id IS NULL``
# (no session to join), so the predicate excludes them automatically.
_NO_CLICK_SQL = """
    SELECT sq.query, COUNT(*) AS n
    FROM search_queries sq
    WHERE sq.tenant_id = %s
      AND sq.result_count > 0
      AND sq.session_id IS NOT NULL
      AND sq.at >= NOW() - make_interval(days => %s)
      AND NOT EXISTS (
          SELECT 1 FROM interactions i
          WHERE i.session_id = sq.session_id
            AND i.action IN ('clicked', 'opened')
      )
    GROUP BY sq.query
    ORDER BY n DESC, sq.query
"""


@dataclass(frozen=True)
class SearchFailure:
    """One ranked failed-query row for the ``brain gaps`` read view.

    ``kind`` is ``'zero_results'`` (the search returned nothing) or
    ``'no_click'`` (results were returned but never opened in-session).
    ``query`` is the raw stored string for the CLI surface, or the derived
    normalized canonical label when ``top_search_failures(normalize=True)``
    (the MCP surface — raw query strings stay server-side; see Plan 08 §6).
    """

    query: str
    count: int
    kind: str


def record_search_query(
    conn: psycopg.Connection[Any],
    *,
    query: str,
    result_count: int,
    session_id: uuid.UUID | None,
    source: str,
    tenant_id: str = "default",
) -> None:
    """INSERT one row into ``search_queries``. Best-effort on a transient blip.

    A single parameterized INSERT. Callers run with ``autocommit=True`` so this
    is one round-trip; logging must never slow or break the search response.

    Error contract (Plan 08 §3 / §3e):

    - :class:`psycopg.OperationalError` (a transient connection blip) is
      **swallowed** — a DB hiccup must not break a search the user already got
      results for. Logged at WARNING with the exception *type* only.
    - Schema errors such as :class:`psycopg.errors.UndefinedTable` (migration
      019 not applied) **propagate** — a missing table is an operator error
      that must surface visibly, never be silently eaten.

    Privacy (Plan 08 §6 — firm contract): the raw ``query`` string MUST NOT
    appear at INFO level or above. It may only be logged at DEBUG (the blip
    path below) where local-only debugging is the explicit opt-in.
    """
    try:
        conn.execute(
            """
            INSERT INTO search_queries
                (tenant_id, query, result_count, session_id, source)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                tenant_id,
                query,
                result_count,
                str(session_id) if session_id is not None else None,
                source,
            ),
        )
    except psycopg.OperationalError as exc:
        # Transient blip only — never the missing-table case (UndefinedTable is
        # a ProgrammingError, not an OperationalError, so it propagates above).
        logger.warning(
            "search-query logging skipped (DB blip): %s", type(exc).__name__
        )
        logger.debug("search-query logging failed for query=%r: %s", query, exc)


def _normalize_tokens(query: str) -> frozenset[str]:
    """Casefold, strip punctuation, split on whitespace → distinct token set."""
    cleaned = _PUNCT_RE.sub(" ", query.casefold())
    return frozenset(cleaned.split())


def _canonical_key(query: str) -> str:
    """Collision-resistant normalized label: sorted distinct tokens, space-joined.

    Aggressive normalization (casefold + dedup + sort) so two queries that
    differ only in token order or punctuation collapse to the same
    ``elicitation_gaps.target_id`` — a residual collision is a harmless
    score-update upsert (Plan 08 §6).
    """
    return " ".join(sorted(_normalize_tokens(query)))


def cluster_failed_queries(
    queries: list[str], *, threshold: float = 0.5
) -> list[list[str]]:
    """Greedily group queries by token-Jaccard similarity ≥ ``threshold``.

    Pure Python — no DB, no LLM. Each query is normalized to a distinct token
    set (:func:`_normalize_tokens`); a query joins the first existing cluster
    whose seed token set it overlaps by Jaccard ≥ ``threshold``, else it seeds a
    new cluster. Returned clusters preserve the original query strings (so the
    caller can count frequencies) and are ordered by descending size.

    Example at the default ``threshold=0.5``: ``"q3 hiring"`` and
    ``"q3 hiring plan"`` share 2 of 3 distinct tokens (Jaccard ≈ 0.67) → one
    cluster; ``"benefits policy"`` and ``"benefits plan"`` share 1 of 3
    (Jaccard ≈ 0.33) → separate clusters.
    """
    clusters: list[list[str]] = []
    seeds: list[frozenset[str]] = []
    for query in queries:
        tokens = _normalize_tokens(query)
        for idx, seed in enumerate(seeds):
            if jaccard(tokens, seed) >= threshold:
                clusters[idx].append(query)
                break
        else:
            clusters.append([query])
            seeds.append(tokens)
    order = sorted(
        range(len(clusters)), key=lambda i: (-len(clusters[i]), clusters[i][0])
    )
    return [clusters[i] for i in order]


def _failed_query_counts(
    conn: psycopg.Connection[Any], *, tenant_id: str, lookback_days: int
) -> dict[str, int]:
    """Merge zero-result + no-click occurrence counts per raw query string."""
    counts: dict[str, int] = {}
    for sql in (_ZERO_RESULT_SQL, _NO_CLICK_SQL):
        for query, n in conn.execute(sql, (tenant_id, lookback_days)).fetchall():
            counts[query] = counts.get(query, 0) + int(n)
    return counts


class SearchFailureDetector:
    """Surface repeated search failures as ``search_failure`` knowledge gaps.

    Implements the :class:`brain.elicit.detectors.GapDetector` protocol so it
    plugs into ``build_queue`` / ``DETECTOR_REGISTRY`` unmodified. Mines the
    ``search_queries`` log over ``lookback_days``, clusters near-duplicate
    failed queries, and emits one :class:`Gap` per cluster whose total
    occurrence count is ≥ ``min_cluster_size``.

    Each gap carries ``evidence_ids=[]`` — intentionally empty, since the
    defining property of a search-failure gap is that *no* document answers it.
    ``build_queue`` exempts ``search_failure`` from the evidence-docs guard
    exactly as it does ``user_flagged``.
    """

    signal_kind = "search_failure"

    def __init__(self, *, lookback_days: int, min_cluster_size: int) -> None:
        self._lookback_days = lookback_days
        self._min_cluster_size = min_cluster_size

    def detect(
        self, conn: psycopg.Connection[Any], *, tenant_id: str, limit: int
    ) -> list[Gap]:
        # Local import keeps ``brain.gaps`` import-light: ``elicit.schema`` (and
        # the ``brain.elicit`` package __init__ it triggers) is only loaded when
        # the detector actually runs, breaking the
        # detectors → gaps → elicit.schema → detectors cycle at module load.
        from .elicit.schema import Gap

        counts = _failed_query_counts(
            conn, tenant_id=tenant_id, lookback_days=self._lookback_days
        )
        if not counts:
            return []
        # Expand to a flat occurrence list so cluster size == raw frequency and
        # the canonical label is the most-frequent member of its cluster.
        expanded = [q for q, n in counts.items() for _ in range(n)]
        gaps: list[Gap] = []
        for cluster in cluster_failed_queries(expanded):
            size = len(cluster)
            if size < self._min_cluster_size:
                continue
            most_common = Counter(cluster).most_common(1)[0][0]
            canonical_label = _canonical_key(most_common)
            gaps.append(
                Gap(
                    gap_id=str(uuid.uuid4()),
                    signal_kind="search_failure",
                    target_type="topic",
                    target_id=canonical_label,
                    score=float(size),
                    evidence_ids=[],
                    rationale=(
                        f"Asked {size} time(s) with no useful result: "
                        f"'{canonical_label}'."
                    ),
                )
            )
            if len(gaps) >= limit:
                break
        return gaps


def top_search_failures(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str = "default",
    since_days: int,
    limit: int,
    normalize: bool = False,
) -> list[SearchFailure]:
    """Rank failed queries (zero-result + no-click) over the lookback window.

    Read-only view backing ``brain gaps`` / MCP ``brain_gaps`` — never writes to
    ``elicitation_gaps``. When ``normalize=True`` (the MCP surface) each query is
    collapsed to its normalized canonical label and counts are summed per
    (label, kind), so only derived strings — never raw stored query text — leave
    the server (Plan 08 §6). When ``normalize=False`` (the local CLI surface)
    raw query strings are returned as-is.
    """
    rows: list[tuple[str, int, str]] = []
    limited = " LIMIT %s"
    for query, n in conn.execute(
        _ZERO_RESULT_SQL + limited, (tenant_id, since_days, limit)
    ).fetchall():
        rows.append((query, int(n), "zero_results"))
    for query, n in conn.execute(
        _NO_CLICK_SQL + limited, (tenant_id, since_days, limit)
    ).fetchall():
        rows.append((query, int(n), "no_click"))

    if normalize:
        agg: dict[tuple[str, str], int] = {}
        for query, n, kind in rows:
            key = (_canonical_key(query), kind)
            agg[key] = agg.get(key, 0) + n
        rows = [(label, n, kind) for (label, kind), n in agg.items()]

    rows.sort(key=lambda t: (-t[1], t[0]))
    return [
        SearchFailure(query=query, count=n, kind=kind)
        for query, n, kind in rows[:limit]
    ]
