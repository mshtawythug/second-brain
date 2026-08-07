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
# Lexical-miss pass. The headline knowledge-gap signal.
#
# Design bug found in live QA: hybrid search's VECTOR leg always returns
# nearest neighbours, so an off-corpus query logs ``result_count > 0`` (filler)
# and a ``result_count = 0`` predicate NEVER fires on the CLI path. The real
# signal is the FTS (lexical) leg matching ZERO chunks (``fts_count = 0``): the
# corpus has no lexical trace of the query. We keep the ``'zero_results'`` kind
# label (see ``top_search_failures`` / the detector) for backward compatibility
# — the *meaning* ("the corpus had no useful answer") is unchanged; only the
# detection mechanism moved from ``result_count`` to ``fts_count``.
#
# Old rows predate migration 023 and carry ``fts_count IS NULL``. For them we
# fall back to the historical ``result_count = 0`` semantics so no past
# zero-result evidence is lost; the new signal applies to rows written after
# the fix. Both branches are index-backed (migration 019's result_count=0
# partial index + migration 023's fts_count=0 partial index → a BitmapOr plan).
#: The lexical-miss predicate on its own, so any surface that needs to count
#: or filter failed searches expresses "this search failed" identically
#: instead of re-deriving the two-branch rule and drifting from the detector.
ZERO_RESULT_PREDICATE_SQL = (
    "fts_count = 0 OR (fts_count IS NULL AND result_count = 0)"
)

_ZERO_RESULT_SQL = f"""
    SELECT query, COUNT(*) AS n
    FROM search_queries
    WHERE tenant_id = %s
      AND ({ZERO_RESULT_PREDICATE_SQL})
      AND at >= NOW() - make_interval(days => %s)
    GROUP BY query
    ORDER BY n DESC, query
"""

# No-click: a non-empty result set that the user never opened/clicked within
# the same search session. MCP-only — CLI searches carry ``session_id IS NULL``
# (no session to join), so the predicate excludes them automatically.
#
# Mutual exclusivity with the lexical-miss pass (matters since migration 023):
# a lexical miss now keys off ``fts_count = 0``, but such a row can still have
# ``result_count > 0`` (vector filler). Without the ``fts_count`` guard below,
# an MCP lexical-miss that was never opened would match BOTH passes and get
# counted twice (and emit two ``SearchFailure`` rows for one event). We exclude
# ``fts_count = 0`` rows here so each event is attributed to exactly one pass —
# the lexical miss is the stronger signal. ``fts_count IS NULL`` (legacy /
# pre-023 rows) stays eligible for no-click, preserving the prior behavior
# where ``result_count = 0`` and ``result_count > 0`` were already exclusive.
_NO_CLICK_SQL = """
    SELECT sq.query, COUNT(*) AS n
    FROM search_queries sq
    WHERE sq.tenant_id = %s
      AND sq.result_count > 0
      AND (sq.fts_count IS NULL OR sq.fts_count > 0)
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


#: ``search_queries`` columns added by a migration LATER than 019, mapped to
#: the migration that adds each. A binary can legitimately run against a DB
#: that has the table but not yet one of these, so both the write path and the
#: read path degrade with an actionable hint instead of failing.
#:
#: Membership test, NOT a substring match: adding a column here is a one-line
#: change for a future wave, and an unknown column still surfaces as the bug
#: it is.
_ADDITIVE_COLUMNS: dict[str, str] = {
    "fts_count": "023",
    "duration_ms": "024",
    "agent_id": "027",
}


def _missing_additive_column(exc: psycopg.Error) -> str | None:
    """Return the known additive column named by ``exc``, or ``None``."""
    message = str(exc)
    return next((col for col in _ADDITIVE_COLUMNS if col in message), None)


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
    fts_count: int | None = None,
    duration_ms: int | None = None,
    agent_id: str | None = None,
    tenant_id: str = "default",
) -> None:
    """INSERT one row into ``search_queries``. Best-effort on a transient blip.

    A single parameterized INSERT. Callers run with ``autocommit=True`` so this
    is one round-trip; logging must never slow or break the search response.

    Error contract (Plan 08 §3 / §3e):

    - :class:`psycopg.OperationalError` (a transient connection blip) is
      **swallowed** — a DB hiccup must not break a search the user already got
      results for. Logged at WARNING with the exception *type* only.
    - :class:`psycopg.errors.UndefinedTable` (migration 019 not applied) is
      **swallowed with a loud, actionable WARNING** naming ``brain init``.
      Search is the daily-driver command; a binary upgrade that lands before
      the operator re-runs ``brain init`` must never break search itself
      (observed live against a pre-019 prod DB). The operator still sees the
      warning on every search until the migration is applied, and the
      ``brain gaps`` surfaces fail loudly with the same hint. The INSERT runs
      inside its own ``conn.transaction()`` (savepoint when nested) so the
      failure never poisons or rolls back the caller's transaction.
    - :class:`psycopg.errors.UndefinedColumn` naming one of
      :data:`_ADDITIVE_COLUMNS` (``fts_count`` from migration 023,
      ``duration_ms`` from 024 — a DB that has the table but not yet the
      ``agent_id`` from 027 — a DB that has the table but not yet the
      later additive column) gets the **same swallow-with-hint** treatment,
      for the same daily-driver reason: a binary that writes a new column
      must not break search on a DB that hasn't run ``brain init`` since the
      upgrade. The guard is a SET-MEMBERSHIP test over the known additive
      columns, not a substring match, so a genuinely-unknown column still
      propagates as a real bug and a future wave adds one string rather than
      rewriting the guard.
    - Any other schema/programming error **propagates** — those are real bugs
      that must surface visibly, never be silently eaten. In particular a
      :class:`psycopg.errors.CheckViolation` from an unrecognised ``source``
      is NOT swallowed: an unknown surface is a code bug, not a migration lag.

    Privacy (Plan 08 §6 — firm contract): the raw ``query`` string MUST NOT
    appear at INFO level or above. It may only be logged at DEBUG (the blip
    path below) where local-only debugging is the explicit opt-in.
    """
    try:
        # The inner transaction() scopes the best-effort INSERT: a savepoint
        # when the caller is already in a transaction, a plain transaction
        # under autocommit. On failure only THIS insert rolls back — the
        # caller's prior work and open transaction state are untouched (a
        # bare conn.rollback() here would clobber both, and is forbidden
        # inside an explicit conn.transaction() block).
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO search_queries
                    (tenant_id, query, result_count, fts_count, duration_ms,
                     session_id, source, agent_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    query,
                    result_count,
                    fts_count,
                    duration_ms,
                    str(session_id) if session_id is not None else None,
                    source,
                    agent_id,
                ),
            )
    except psycopg.errors.UndefinedTable:
        # Migration 019 not applied yet (e.g. binary upgraded before `brain
        # init` re-ran). Search must keep working; nag until the operator
        # migrates.
        logger.warning(
            "search-query logging skipped: search_queries table missing "
            "(migration 019 not applied) — run `brain init` to enable "
            "`brain gaps`"
        )
    except psycopg.errors.UndefinedColumn as exc:
        # The table exists but lacks one of the later additive columns (e.g.
        # binary upgraded before `brain init` re-ran). Same daily-driver
        # contract as the missing-table case above: search must keep working;
        # nag until the operator migrates. Narrowed to the KNOWN additive
        # columns so a genuinely-unknown column still propagates as a real bug.
        missing = _missing_additive_column(exc)
        if missing is None:
            raise
        logger.warning(
            "search-query logging skipped: search_queries.%s column missing "
            "(migration %s not applied) — run `brain init` to restore full "
            "`brain gaps` signal",
            missing,
            _ADDITIVE_COLUMNS[missing],
        )
    except psycopg.OperationalError as exc:
        # Transient blip only — schema/programming errors other than the
        # missing-table case above propagate as real bugs.
        logger.warning(
            "search-query logging skipped (DB blip): %s", type(exc).__name__
        )
        logger.debug("search-query logging failed for query=%r: %s", query, exc)


def search_queries_schema_hint(exc: psycopg.Error) -> str | None:
    """Map a missing ``search_queries`` schema object to a `brain init` hint.

    The ``brain gaps`` read path (:func:`top_search_failures`,
    :class:`SearchFailureDetector`) reads the ``search_queries`` table and its
    later additive columns (:data:`_ADDITIVE_COLUMNS`). On a DB that hasn't
    applied migration 019 (no table) or a later additive migration (no
    column), the query raises, and the surfaces (CLI / MCP) must fail
    loudly-but-cleanly with an actionable hint instead of a traceback —
    mirroring the swallow-with-hint contract the search write path uses in
    :func:`record_search_query`.

    Returns the hint string for a known migration gap, or ``None`` for any
    other error (a genuinely-unknown column / real bug) so the caller
    re-raises it.
    """
    if isinstance(exc, psycopg.errors.UndefinedTable):
        return (
            "search_queries table missing (migration 019 not applied) — "
            "run `brain init` first"
        )
    if isinstance(exc, psycopg.errors.UndefinedColumn):
        missing = _missing_additive_column(exc)
        if missing is not None:
            return (
                f"search_queries.{missing} column missing (migration "
                f"{_ADDITIVE_COLUMNS[missing]} not applied) — run "
                "`brain init` first"
            )
    return None


def _normalize_tokens(query: str) -> frozenset[str]:
    """Casefold, strip punctuation, split on whitespace → distinct token set."""
    cleaned = _PUNCT_RE.sub(" ", query.casefold())
    return frozenset(cleaned.split())


def canonical_query_key(query: str) -> str:
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
            canonical_label = canonical_query_key(most_common)
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
            key = (canonical_query_key(query), kind)
            agg[key] = agg.get(key, 0) + n
        rows = [(label, n, kind) for (label, kind), n in agg.items()]

    rows.sort(key=lambda t: (-t[1], t[0]))
    return [
        SearchFailure(query=query, count=n, kind=kind)
        for query, n, kind in rows[:limit]
    ]
