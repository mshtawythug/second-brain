"""Eval runner: EvalResult / EvalReport dataclasses and the run_eval() function.

The dataclasses are defined here and re-exported from ``brain.eval``.
``run_eval()`` wraps ``hybrid_search`` once per query and scores three
metrics (nDCG@5, MRR, recall@20) against the golden corpus.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from ..embeddings import OllamaEmbedError
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
    through to the search function.

    Tolerates :exc:`~brain.embeddings.OllamaEmbedError` by skipping the affected
    query with a warning and recording ``actual_doc_ids=[]`` / metrics=0.0.

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
        try:
            search_results = hybrid_search(
                conn,
                embedder=embedder,
                query=q.query,
                limit=limit_per_query,
                source_kind=q.source_filter,
                tag=q.tag_filter,
                since_days=q.since_days,
                vector_sim_floor=vector_sim_floor,
                recency_halflife_days=recency_halflife_days,
                snippet_context_tokens=snippet_context_tokens,
            )
            actual_ids = [r.document_id for r in search_results]
        except OllamaEmbedError as exc:
            _logger.warning(
                "OllamaEmbedError for query %r — recording 0.0 metrics: %s",
                q.query,
                exc,
            )
            actual_ids = []

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
