"""Eval runner: EvalResult / EvalReport dataclasses and the run_eval() function.

The dataclasses are defined here and re-exported from ``brain.eval``.
``run_eval()`` wraps ``hybrid_search`` once per query and scores three
metrics (nDCG@5, MRR, recall@20) against the golden corpus.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import psycopg

from ..errors import EmbedError
from ..ingest import Embedder
from ..search import hybrid_search
from .corpus import EvalQuery
from .errors import EvalCorpusError
from .metrics import (
    mrr as _mrr,
)
from .metrics import (
    ndcg_at_k as _ndcg_at_k,
)
from .metrics import (
    recall_at_k as _recall_at_k,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvalResult:
    """Scores for a single eval query."""

    query: str
    category: str
    expected_doc_ids: list[str]  # canonicalized to full UUIDs
    actual_doc_ids: list[str]  # full UUIDs from hybrid_search, in rank order
    ndcg_at_5: float
    mrr: float
    recall_at_20: float
    # True when the query's embed call failed TRANSIENTLY (any backend's
    # EmbedError with ``transient=True``): the 0.0 metrics above then measure
    # an unhealthy embedder, not retrieval. Ad-hoc runs keep
    # scoring the row; ``save_baseline`` refuses it and ``diff_reports``
    # excludes it (spec docs/specs/2026-10-15-eval-record-degraded-design.md).
    # Never serialized into a baseline file, which cannot contain a degraded row.
    degraded: bool = False


@dataclass(frozen=True)
class CategorySummary:
    """Aggregate eval metrics for one category."""

    category: str
    count: int
    mean_ndcg_at_5: float
    mean_mrr: float
    mean_recall_at_20: float


@dataclass(frozen=True)
class EvalReport:
    """Full eval run report: per-query results + aggregates + config snapshot."""

    results: list[EvalResult]  # one per EvalQuery, in input order
    mean_ndcg_at_5: float
    mean_mrr: float
    mean_recall_at_20: float
    per_category: dict[str, CategorySummary]
    config_signature: dict[str, Any]  # {"recency_halflife_days": ..., "embedder": ..., ...}
    generated_at: datetime  # UTC


def _utc_midnight(day: date | None) -> datetime | None:
    """UTC-aware midnight for a corpus ``after``/``before`` date (``None`` passes).

    Built aware here rather than left naive for ``hybrid_search``'s
    ``_ensure_utc`` to stamp, so a pinned eval bound cannot depend on the
    session ``TimeZone`` or on a helper in another module staying as it is.
    """
    if day is None:
        return None
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _normalize_ids(conn: psycopg.Connection[Any], ids: list[str]) -> list[str]:
    """Resolve 8-char hex prefixes to full UUIDs; pass full UUIDs through.

    A full UUID is detected by length >= 32 characters OR presence of a
    hyphen.  Shorter strings are treated as hex prefixes and resolved via a
    ``LIKE`` query.

    Silently drops IDs that match zero documents (treated as stale/uncurated).

    Raises:
        EvalCorpusError: When a prefix matches two or more documents
            (ambiguous prefix).
    """
    result: list[str] = []
    for doc_id in ids:
        if len(doc_id) >= 32 or "-" in doc_id:
            # Full UUID — pass through without a DB round-trip.
            result.append(doc_id)
        else:
            pattern = f"{doc_id}%"
            rows = conn.execute(
                "SELECT id::text FROM documents WHERE id::text LIKE %s",
                (pattern,),
            ).fetchall()
            if len(rows) == 0:
                _logger.debug("expected_doc_id prefix %r matched no documents; skipping", doc_id)
            elif len(rows) > 1:
                raise EvalCorpusError(
                    f"expected_doc_id prefix {doc_id!r} is ambiguous: "
                    f"matches {len(rows)} documents"
                )
            else:
                result.append(str(rows[0][0]))
    return result


def run_eval(
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder,
    queries: Sequence[EvalQuery],
    limit_per_query: int = 20,
    recency_halflife_days: float | None = None,
    snippet_context_tokens: int = 0,
    vector_sim_floor: float = 0.0,
    embedder_name: str = "unknown",
) -> EvalReport:
    """Run the eval harness over ``queries`` and return a scored :class:`EvalReport`.

    One ``hybrid_search`` call per query at ``limit=limit_per_query`` (default 20
    so recall@20 is computable).  Threads filter kwargs from :class:`EvalQuery`
    through to the search function, including the absolute ``after``/``before``
    bounds a pinned ``recency`` query uses in place of ``since_days``.

    Tolerates a TRANSIENT :exc:`~brain.errors.EmbedError` from any backend by
    skipping the affected query with a warning and recording
    ``actual_doc_ids=[]`` / metrics=0.0, with ``degraded=True`` on that row so
    the record and gate paths can refuse it. A permanent one (``transient`` is
    ``False``: a model not pulled, a bad API key, an unusable response) is
    re-raised and aborts the run, because every later query would fail the same
    way and "retry" would be the wrong advice (spec
    docs/specs/2026-10-16-eval-degraded-all-embedders-design.md).

    Args:
        conn: Live psycopg connection (test_db or prod).
        embedder: Embedding backend (any :class:`~brain.ingest.Embedder`).
        queries: Sequence of :class:`EvalQuery` from the golden corpus.
        limit_per_query: How many results to fetch per query (default 20).
        recency_halflife_days: Passed to ``hybrid_search`` unchanged.
        snippet_context_tokens: Passed to ``hybrid_search`` unchanged.
        vector_sim_floor: Passed to ``hybrid_search`` unchanged.
        embedder_name: Logged in ``config_signature`` for baseline diffs.

    Returns:
        A frozen :class:`EvalReport` with per-query and aggregate scores.
    """
    results: list[EvalResult] = []

    for q in queries:
        degraded = False
        try:
            search_results = hybrid_search(
                conn,
                embedder=embedder,
                query=q.query,
                limit=limit_per_query,
                source_kind=q.source_filter,
                tag=q.tag_filter,
                since_days=q.since_days,
                after=_utc_midnight(q.after),
                before=_utc_midnight(q.before),
                vector_sim_floor=vector_sim_floor,
                recency_halflife_days=recency_halflife_days,
                snippet_context_tokens=snippet_context_tokens,
            )
            actual_ids = [r.document_id for r in search_results]
        except EmbedError as exc:
            if not exc.transient:
                raise
            _logger.warning(
                "%s for query %r — recording 0.0 metrics: %s",
                type(exc).__name__,
                q.query,
                exc,
            )
            actual_ids = []
            degraded = True

        expected_ids = _normalize_ids(conn, list(q.expected_doc_ids))

        if not expected_ids:
            # All expected IDs failed to resolve (uncurated / stale corpus).
            ndcg = mrr_score = recall = 0.0
        else:
            ndcg = _ndcg_at_k(actual_ids, expected_ids, k=5)
            mrr_score = _mrr(actual_ids, expected_ids)
            recall = _recall_at_k(actual_ids, expected_ids, k=limit_per_query)

        results.append(
            EvalResult(
                query=q.query,
                category=q.category,
                expected_doc_ids=expected_ids,
                actual_doc_ids=actual_ids,
                ndcg_at_5=ndcg,
                mrr=mrr_score,
                recall_at_20=recall,
                degraded=degraded,
            )
        )

    # Aggregate means.
    n = len(results)
    mean_ndcg = sum(r.ndcg_at_5 for r in results) / n if n else 0.0
    mean_mrr = sum(r.mrr for r in results) / n if n else 0.0
    mean_recall = sum(r.recall_at_20 for r in results) / n if n else 0.0

    # Per-category aggregates.
    cat_buckets: dict[str, list[EvalResult]] = {}
    for r in results:
        cat_buckets.setdefault(r.category, []).append(r)
    per_category: dict[str, CategorySummary] = {}
    for cat, cat_results in cat_buckets.items():
        nc = len(cat_results)
        per_category[cat] = CategorySummary(
            category=cat,
            count=nc,
            mean_ndcg_at_5=sum(r.ndcg_at_5 for r in cat_results) / nc,
            mean_mrr=sum(r.mrr for r in cat_results) / nc,
            mean_recall_at_20=sum(r.recall_at_20 for r in cat_results) / nc,
        )

    config_signature: dict[str, Any] = {
        "recency_halflife_days": recency_halflife_days,
        "snippet_context_tokens": snippet_context_tokens,
        "vector_sim_floor": vector_sim_floor,
        "embedder": embedder_name,
    }

    return EvalReport(
        results=results,
        mean_ndcg_at_5=mean_ndcg,
        mean_mrr=mean_mrr,
        mean_recall_at_20=mean_recall,
        per_category=per_category,
        config_signature=config_signature,
        generated_at=datetime.now(tz=UTC),
    )


def degraded_results(report: EvalReport) -> list[EvalResult]:
    """The rows of *report* whose embed call failed, in result order.

    The single definition of "degraded" that the baseline refusal, the diff
    exclusion and the CLI warnings all read, so they cannot disagree.
    """
    return [r for r in report.results if r.degraded]


def embed_failure_message(exc: EmbedError, *, command: str) -> str:
    """One stderr line for a run that an embed failure stopped.

    ``command`` prefixes the line (``"eval"``, ``"eval --answer"``). Names the
    exception class and carries the backend's own text (built from the HTTP
    status, the response body or the transport error — never the request input),
    and gives the advice that matches ``exc.transient``. It is never given query
    text, so it cannot print any: the golden corpus is personal.
    """
    kind = type(exc).__name__
    if exc.transient:
        return (
            f"{command}: the embedder failed with a retryable error ({kind}: {exc}) — "
            f"the run stopped and no report was written; retry when the embedder is "
            f"healthy."
        )
    return (
        f"{command}: the embedder failed with an error a retry will not fix "
        f"({kind}: {exc}) — the run stopped and no report or baseline was written. "
        f"Check the embedder configuration (model pulled, API key, host URL)."
    )
