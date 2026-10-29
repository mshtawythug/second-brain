"""Eval runner: EvalResult / EvalReport dataclasses and the run_eval() function.

The dataclasses are defined here and re-exported from ``brain.eval``.
``run_eval()`` wraps ``hybrid_search`` once per query and scores three
metrics (nDCG@5, MRR, recall@20) against the golden corpus.

**Ranking stages** (RM-14a). ``run_eval`` applies the configured stages (the
reranker, and the later ones RM-09 composes) itself, with
:func:`brain.search_pipeline.apply_stages` after its own ``hybrid_search``.
It never calls ``run_search``, so it never falls back to text-only matching:
an embed failure stays a visible degraded row (spec §2.7). Each stage's
``signature()`` joins ``config_signature``, so with every stage off the
signature, and any baseline recorded under it, is exactly what it was.
"""

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import psycopg

from ..errors import EmbedError, describe_failure
from ..ingest import Embedder
from ..search import hybrid_search
from ..search_pipeline import SearchStage, apply_stages, build_stages, size_stages
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
    # Each applied-or-skipped stage's time for this query, by stage name
    # (``{"rerank": 412.0}``); ``{}`` with no stage, or on a degraded row. RM-14c
    # reads its latency from here. In ``brain eval --json``; never in a baseline
    # file, because latency is a property of the machine, not of the corpus.
    # Only APPLIED stages are timed here: a skipped one is in ``stages_skipped``.
    stage_ms: dict[str, float] = field(default_factory=dict)
    # Names of the stages that SKIPPED for this query (a reranker that timed
    # out). The row was then scored in hybrid order while ``config_signature``
    # names the stage, so, like a degraded row, it is refused by
    # ``save_baseline`` and kept out of ``diff_reports``' deltas. In ``brain
    # eval --json``; never in a baseline file, which cannot contain one.
    stages_skipped: tuple[str, ...] = ()


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
    stages: Sequence[SearchStage] | None = None,
    env: Mapping[str, str] | None = None,
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
        stages: The ranking stages to apply after each search. ``None`` builds
            them with :func:`~brain.search_pipeline.build_stages` from ``env``.
        env: Read only when ``stages`` is ``None``; ``None`` means
            ``os.environ``, which ``eval_cmd``'s ``Config.load()`` has already
            populated (spec §2.3's carve-out, so ``cli.py`` is not edited).
            Tests pass ``stages`` or ``env``, never neither.

    Returns:
        A frozen :class:`EvalReport` with per-query and aggregate scores.
    """
    results: list[EvalResult] = []
    if stages is None:
        stages = build_stages(os.environ if env is None else env)
    stages, fetch = size_stages(stages, limit_per_query)

    for q in queries:
        degraded = False
        stage_ms: dict[str, float] = {}
        skipped: tuple[str, ...] = ()
        try:
            search_results = hybrid_search(
                conn,
                embedder=embedder,
                query=q.query,
                limit=fetch,
                source_kind=q.source_filter,
                tag=q.tag_filter,
                since_days=q.since_days,
                after=_utc_midnight(q.after),
                before=_utc_midnight(q.before),
                vector_sim_floor=vector_sim_floor,
                recency_halflife_days=recency_halflife_days,
                snippet_context_tokens=snippet_context_tokens,
            )
            search_results, stage_ms, skipped = _apply_stages(
                conn, q.query, search_results, stages
            )
            actual_ids = [r.document_id for r in search_results[:limit_per_query]]
        except EmbedError as exc:
            if not exc.transient:
                raise
            _logger.warning(
                "%s for query %r — recording 0.0 metrics: %s",
                type(exc).__name__,
                q.query,
                describe_failure(exc),
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
                stage_ms=stage_ms,
                stages_skipped=skipped,
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
    for stage in stages:
        # After the base keys, so an all-off run's signature is byte-identical.
        config_signature.update(stage.signature())

    return EvalReport(
        results=results,
        mean_ndcg_at_5=mean_ndcg,
        mean_mrr=mean_mrr,
        mean_recall_at_20=mean_recall,
        per_category=per_category,
        config_signature=config_signature,
        generated_at=datetime.now(tz=UTC),
    )


def _apply_stages(
    conn: psycopg.Connection[Any],
    query: str,
    results: list[Any],
    stages: Sequence[SearchStage],
) -> tuple[list[Any], dict[str, float], tuple[str, ...]]:
    """Run ``stages`` over one query's rows.

    Returns the rows, each APPLIED stage's time, and the names of the stages
    that skipped. A skipped stage leaves the row in hybrid order, so it measured
    a pipeline without that stage: the row is marked (``stages_skipped``) and
    refused like a degraded one, and the skip is logged at WARNING by stage name
    and failure class only (never the query, which is personal).
    """
    if not stages:
        return results, {}, ()
    staged, reports = apply_stages(
        conn, query=query, session_id=None, results=results, stages=stages
    )
    for report in reports:
        if not report.applied:
            _logger.warning(
                "eval: stage %s skipped for one query (%s); that row cannot be "
                "recorded or judged",
                report.name,
                report.skipped_reason,
            )
    applied = {report.name: report.ms for report in reports if report.applied}
    skipped = tuple(report.name for report in reports if not report.applied)
    return staged, applied, skipped


def skipped_stage_results(report: EvalReport) -> list[EvalResult]:
    """The rows of *report* in which a ranking stage skipped, in result order (RM-14a).

    The single definition the baseline refusal and the diff exclusion read.
    Kept apart from :func:`degraded_results`, whose callers say a degraded row
    "counted as 0.0": a skipped row is scored, in hybrid order.
    """
    return [r for r in report.results if r.stages_skipped]


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
        f"Fix the cause it names (e.g. a model not pulled, an API key, a host URL, "
        f"a context window too small for the input) and run again."
    )
