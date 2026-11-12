"""One search pipeline for every user-facing surface (roadmap ticket RM-09, G4).

``brain search``, ``brain explain``, MCP ``brain_search``, ``brain ui``'s
``GET /api/search``, ``brain recall`` and the retrieval of ``brain ask`` all
search through :func:`run_search`. It composes, in one order, what used to be
wired per surface: the search itself (``hybrid_search`` or a surface's patch
point), the text-only fallback (:mod:`brain.search_fallback`), and the ranking
STAGES (a reranker, RM-14a; a document leg, RM-20; engagement, RM-22b). A new
stage is wired once, in :func:`build_stages`, and reaches every surface. No
stage edits the ranking code in :mod:`brain.search`.

The opt-in query rewrite (RM-54) is not a stage: a stage sees rows and cannot
search, and a stage would reach every surface when only the CLI asks for it. It
is the ``rewriter`` argument of :func:`run_search`, and it still reports through
a :class:`StageReport`.

Stages see :class:`~brain.search.SearchResult` rows, not chunks (RM-09.D1): a
stage that needs chunk text reads the snippet, which already carries
``snippet_context_tokens`` of context.

Out of scope, each with its recorded reason for calling ``hybrid_search``
directly: ``brain eval`` (:mod:`brain.eval.runner`), which measures the real
pipeline and never falls back (spec §2.7) and so applies stages itself with
:func:`size_stages` and :func:`apply_stages` (RM-14a); :mod:`brain.graph_rag.fuse`; and
:func:`brain.demo.query_demo`. ``tests/test_search_pipeline_boundary.py`` fails
on any other module that calls ``hybrid_search`` or ``search_with_fts_fallback``.

A stage that needs the query's vector (RM-20's document leg) declares it
(:class:`QueryVectorStage`), and :func:`apply_stages` skips it after a
text-only search, so it never embeds the query a second time after the embed
failed, or at all under ``--fts-only``.

Every string here is class-only (:func:`brain.errors.describe_failure`): a
stage's exception message can carry text the user did not choose to print.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol

import psycopg

from .errors import describe_failure
from .ingest import Embedder
from .query_rewrite import (
    NO_USABLE_PARAPHRASES,
    REWRITE_FETCH_FACTOR,
    REWRITE_STAGE_NAME,
    FusedDocs,
    QueryRanking,
    Rewriter,
    clean_paraphrases,
    fuse_rankings,
)
from .search import SearchResult
from .search_fallback import (
    SearchFallback,
    SearchFn,
    fallback_json,
    search_with_fts_fallback,
)

logger = logging.getLogger(__name__)

#: Search kwargs only the original query gets. Everything else, every filter included, goes to
#: each paraphrase too: a denylist, so a filter added later cannot be missed and widen a
#: paraphrase's set. A paraphrase writing to the holder would corrupt what ``brain gaps`` and the
#: footer read.
_ORIGINAL_ONLY_KWARGS = frozenset({"diagnostics", "total_count"})


@dataclass(frozen=True)
class StageReport:
    """What one stage did to one search. ``skipped_reason`` is class-only."""

    name: str
    applied: bool
    skipped_reason: str | None
    ms: float


@dataclass(frozen=True)
class StageOutcome:
    """A stage's rows and its report."""

    results: list[SearchResult]
    report: StageReport


class SearchStage(Protocol):
    """One ranking stage, applied after the search to its ``SearchResult`` rows."""

    # A read-only property, so a frozen dataclass stage conforms (RM-14a's
    # ``RerankStage``); a plain attribute satisfies it too.
    @property
    def name(self) -> str:
        """The stage's name in its ``StageReport`` and the ``stages`` key."""
        ...

    def candidate_limit(self, limit: int) -> int:
        """How many rows the stage needs to see to return ``limit`` good ones."""
        ...

    def signature(self) -> dict[str, Any]:
        """The stage's settings, merged into ``brain eval``'s ``config_signature``."""
        ...

    def apply(
        self,
        conn: psycopg.Connection[Any],
        *,
        query: str,
        session_id: str | None,
        results: list[SearchResult],
    ) -> StageOutcome:
        """Return the reordered (or filtered) rows.

        A stage that cannot run (timeout, unreachable backend) returns its input
        unchanged with ``applied=False`` and a class-only ``skipped_reason``.
        """
        ...


class QueryVectorStage(SearchStage, Protocol):
    """A stage that needs the query's vector, which a text-only search never computed.

    The flag is optional on a stage: :func:`stage_needs_query_vector` is its one
    reader, and treats a stage that does not declare it as ``False``, so stages
    with no use for the vector (the reranker, the test fakes) need not mention it.
    :func:`build_stages` types the document leg as this Protocol, so mypy checks
    that it declares the flag.
    """

    # Read-only, as ``SearchStage.name`` is, so a frozen dataclass conforms.
    @property
    def needs_query_vector(self) -> bool: ...


#: The ``skipped_reason`` of a :class:`QueryVectorStage` after a text-only search.
TEXT_ONLY_SKIP_REASON = "text_only_search"


def stage_needs_query_vector(stage: object) -> bool:
    """True when ``stage`` declares :attr:`QueryVectorStage.needs_query_vector`.

    The only reader of the flag. ``getattr`` with a ``False`` default, the
    ``embeddings.is_hosted_embedder`` precedent: a stage that does not declare
    it does not need the vector.
    """
    return bool(getattr(stage, "needs_query_vector", False))


def search_was_text_only(
    embedder: Embedder, *, fallback: SearchFallback | None, fts_only: bool
) -> bool:
    """True when the search ran without a query vector.

    It fell back after a failed embed, it was asked for ``fts_only``, or the
    embedder produces no vectors (``NullEmbedder``, which ``hybrid_search``
    coerces to ``fts_only`` itself).
    """
    produces = bool(getattr(embedder, "produces_embeddings", True))
    return fallback is not None or fts_only or not produces


@dataclass(frozen=True)
class PipelineResult:
    """A search's rows, the fallback that produced them, and each stage's report.

    ``fused`` is each query's part in the ranking, only when a rewriter was passed (RM-54).
    :func:`pipeline_json` does not read it, so MCP and ``brain ui`` payloads cannot carry it.
    """

    results: list[SearchResult]
    fallback: SearchFallback | None
    stages: tuple[StageReport, ...]
    fused: FusedDocs | None = None


def build_stages(
    env: Mapping[str, str], *, embedder: Embedder, rerank: bool | None = None
) -> tuple[SearchStage, ...]:
    """The stages every surface applies, read from the settings leaves.

    ``env`` is ``os.environ`` after the surface's ``Config.load()`` has
    published the dotenv chain into it (spec §2.3). The document leg (RM-20) is
    added when ``BRAIN_DOC_LEG`` is on, bound to ``embedder`` (the search's own,
    whose cached query vector it reads); the reranker (RM-14a) when
    ``BRAIN_RERANKER`` names a backend; the engagement stage (RM-22b) when
    ``BRAIN_ENGAGEMENT_ALPHA`` or ``BRAIN_ENGAGEMENT_BETA`` is above zero. The leg
    comes first, so it can lift a document into the head the reranker scores;
    engagement comes last, because a reranker re-sorts its whole head and would
    erase a boost applied before it. All three ship off, so by default this is
    empty and every surface is unchanged.

    ``rerank`` is a one-run override (``brain search --rerank/--no-rerank``):
    ``False`` drops the stage, ``True`` requires a configured backend (a
    ``ConfigError`` otherwise: picking one would choose between a local and a
    hosted service for the user), and ``None`` follows the setting; it does not
    touch the document leg or engagement. A bad rerank, document-leg or
    engagement knob is a ``ConfigError`` here even while its stage is off.
    """
    # Imported here: ``brain.rerank``, ``brain.search_doc_leg`` and
    # ``brain.search_engagement`` import this module's ``StageOutcome`` and
    # ``StageReport``, so a top-level import would be a cycle.
    from . import rerank_backends
    from .config import ConfigError
    from .rerank import RerankStage
    from .search_doc_leg import DocLegStage
    from .search_engagement import EngagementStage
    from .settings.doc_leg import load_doc_leg_settings
    from .settings.engagement import load_engagement_settings
    from .settings.rerank import load_rerank_settings

    doc_leg = load_doc_leg_settings(env)
    settings = load_rerank_settings(env)
    engagement = load_engagement_settings(env)
    if rerank is True and not settings.enabled:
        raise ConfigError(
            "--rerank needs BRAIN_RERANKER set to llamacpp or voyage (it is none)"
        )
    stages: list[SearchStage] = []
    if doc_leg.enabled:
        leg: QueryVectorStage = DocLegStage(embedder=embedder, weight=doc_leg.weight)
        stages.append(leg)
    if settings.enabled and rerank is not False:
        stages.append(
            RerankStage(
                reranker=rerank_backends.make_reranker(settings),
                depth=settings.depth,
                max_chars=settings.max_chars,
                timeout=settings.timeout_s,
            )
        )
    if engagement.enabled:
        stages.append(EngagementStage(alpha=engagement.alpha, beta=engagement.beta))
    return tuple(stages)


def override_stages(
    rerank: bool | None, *, embedder: Embedder
) -> tuple[SearchStage, ...] | None:
    """A CLI run's stages for ``--rerank/--no-rerank``: ``None`` follows the setting.

    ``None`` is passed through so ``run_search`` builds the configured stages
    itself, exactly as when the flag is absent.
    """
    if rerank is None:
        return None
    return build_stages(os.environ, embedder=embedder, rerank=rerank)


@dataclass(frozen=True)
class _UnsizedStage:
    """Stands in for a stage whose ``candidate_limit`` raised: never applied.

    It passes its input on and reports the failure, so the stage still shows in
    ``stages`` (skipped, class-only) and in the same position.
    """

    name: str
    reason: str

    def candidate_limit(self, limit: int) -> int:
        return limit

    def signature(self) -> dict[str, Any]:
        return {}

    def apply(
        self,
        conn: psycopg.Connection[Any],
        *,
        query: str,
        session_id: str | None,
        results: list[SearchResult],
    ) -> StageOutcome:
        report = StageReport(
            name=self.name, applied=False, skipped_reason=self.reason, ms=0.0
        )
        return StageOutcome(results=results, report=report)


def size_stages(
    stages: Sequence[SearchStage], limit: int
) -> tuple[tuple[SearchStage, ...], int]:
    """Ask each stage how deep it needs to see; return the stages and the fetch size.

    A stage whose ``candidate_limit`` raises contributes ``limit`` and is
    replaced by an :class:`_UnsizedStage`: a stage that cannot say how many rows
    it needs is not applied to a guess, and must not cost the search either.
    Public because ``brain eval`` sizes its own fetch with it (RM-14a).
    """
    sized: list[SearchStage] = []
    fetch = limit
    for stage in stages:
        try:
            depth = stage.candidate_limit(limit)
        # Broad for the reason ``apply_stages`` gives: a stage never costs the search.
        except Exception as exc:  # noqa: BLE001
            reason = describe_failure(exc)
            logger.info("search stage %s skipped: %s", stage.name, reason)
            sized.append(_UnsizedStage(name=stage.name, reason=reason))
            continue
        sized.append(stage)
        fetch = max(fetch, depth)
    return tuple(sized), fetch


def apply_stages(
    conn: psycopg.Connection[Any],
    *,
    query: str,
    session_id: str | None,
    results: list[SearchResult],
    stages: Sequence[SearchStage],
    text_only: bool = False,
) -> tuple[list[SearchResult], tuple[StageReport, ...]]:
    """Run ``stages`` over ``results`` in order. Never raises.

    Each report is named after the stage and timed here, so every stage is
    measured the same way. A stage that reports ``applied=False`` has its INPUT
    passed on, whatever rows it returned: a stage that did not apply has not
    changed the ranking.

    ``text_only`` (:func:`search_was_text_only`) skips every stage that needs
    the query vector without calling it: after a failed embed it would embed
    again (``lru_cache`` caches no exception), and under ``--fts-only`` the user
    asked for no embed at all.
    """
    reports: list[StageReport] = []
    for stage in stages:
        started = perf_counter()
        skipped_reason: str | None
        if text_only and stage_needs_query_vector(stage):
            applied, skipped_reason = False, TEXT_ONLY_SKIP_REASON
        else:
            applied, skipped_reason, results = _apply_one(
                stage, conn, query=query, session_id=session_id, results=results
            )
        reports.append(
            StageReport(
                name=stage.name,
                applied=applied,
                skipped_reason=skipped_reason,
                ms=(perf_counter() - started) * 1000.0,
            )
        )
    return results, tuple(reports)


def _apply_one(
    stage: SearchStage,
    conn: psycopg.Connection[Any],
    *,
    query: str,
    session_id: str | None,
    results: list[SearchResult],
) -> tuple[bool, str | None, list[SearchResult]]:
    """Apply one stage: ``(applied, skipped_reason, rows)``. Never raises.

    The rows are the stage's only when it applied; otherwise its input.
    """
    try:
        outcome = stage.apply(conn, query=query, session_id=session_id, results=results)
    # The one broad catch in the search path, and deliberate: a stage is an
    # optional improvement, so ANY failure inside one (a timeout, a dead
    # backend, a bug) must cost that stage, never the search (RM-09).
    except Exception as exc:  # noqa: BLE001
        reason = describe_failure(exc)
        # INFO, not WARNING, for the reason ``search_with_fts_fallback``
        # gives: the CLI configures no logging, so a WARNING would print an
        # unformatted line to stderr.
        logger.info("search stage %s skipped: %s", stage.name, reason)
        return False, reason, results
    skipped_reason = outcome.report.skipped_reason
    if outcome.report.applied:
        return True, skipped_reason, outcome.results
    return False, skipped_reason, results


def _search_paraphrases(
    search_fn: SearchFn,
    conn: psycopg.Connection[Any],
    *,
    paraphrases: Sequence[str],
    embedder: Embedder,
    limit: int,
    fell_back: bool,
    kwargs: Mapping[str, Any],
) -> tuple[list[tuple[str, list[SearchResult]]], str | None]:
    """Search each paraphrase; a failure costs that paraphrase. Returns the failure's class too.

    Under the original's filters, without its diagnostics. If the original fell back, the embedder
    is down, so each goes text-only rather than waiting out another timeout. A paraphrase whose own
    embed fails is dropped, not fallen back: the fallback notice says semantic ranking is off for
    this search, which would be false of the original.

    The savepoint is because ``brain explain`` is not in autocommit: one failed statement would
    otherwise abort the transaction under everything after it. Broad for the reason
    :func:`apply_stages` gives: an optional improvement never costs the search.
    """
    forwarded = {k: v for k, v in kwargs.items() if k not in _ORIGINAL_ONLY_KWARGS}
    if fell_back:
        forwarded["fts_only"] = True
    found: list[tuple[str, list[SearchResult]]] = []
    failure: str | None = None
    for paraphrase in paraphrases:
        try:
            with conn.transaction():
                rows = search_fn(
                    conn, embedder=embedder, query=paraphrase, limit=limit, **forwarded
                )
        except Exception as exc:  # noqa: BLE001
            failure = describe_failure(exc)
            logger.info("paraphrase search dropped: %s", failure)
        else:
            found.append((paraphrase, rows))
    return found, failure


def _rewrite_leg(
    search_fn: SearchFn,
    conn: psycopg.Connection[Any],
    *,
    rewriter: Rewriter,
    embedder: Embedder,
    query: str,
    limit: int,
    results: list[SearchResult],
    fell_back: bool,
    kwargs: Mapping[str, Any],
) -> tuple[list[SearchResult], FusedDocs, StageReport]:
    """Ask for paraphrases, search them, and fuse. Never raises; the worst case is ``results``.

    The report's clock covers the chat call and the extra searches, so the footer's total is true.
    """
    started = perf_counter()
    found: list[tuple[str, list[SearchResult]]] = []
    reason: str | None = None
    try:
        paraphrases = clean_paraphrases(rewriter(query), original=query)
    # Broad for the reason ``apply_stages`` gives: an optional improvement never costs the search.
    except Exception as exc:  # noqa: BLE001
        reason = describe_failure(exc)
        logger.info("query rewrite skipped: %s", reason)
    else:
        found, reason = _search_paraphrases(
            search_fn,
            conn,
            paraphrases=paraphrases,
            embedder=embedder,
            limit=limit,
            fell_back=fell_back,
            kwargs=kwargs,
        )
        if not paraphrases:
            reason = NO_USABLE_PARAPHRASES
    fused = fuse_rankings(
        [
            QueryRanking(query, True, tuple(r.document_id for r in results)),
            *(
                QueryRanking(text, False, tuple(r.document_id for r in rows))
                for text, rows in found
            ),
        ]
    )
    # The original's row wins for a document both found: its snippet is about what was asked.
    rows_by_id: dict[str, SearchResult] = {}
    for row in (row for rows in (results, *(rows for _, rows in found)) for row in rows):
        rows_by_id.setdefault(row.document_id, row)
    report = StageReport(
        name=REWRITE_STAGE_NAME,
        applied=bool(found),
        skipped_reason=reason,
        ms=(perf_counter() - started) * 1000.0,
    )
    return [rows_by_id[document_id] for document_id in fused], fused, report


def run_search(
    search_fn: SearchFn,
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder,
    query: str,
    limit: int,
    session_id: str | None = None,
    fallback: bool = True,
    stages: Sequence[SearchStage] | None = None,
    rewriter: Rewriter | None = None,
    **kwargs: Any,
) -> PipelineResult:
    """Search, fall back if asked to, apply the stages, and truncate to ``limit``.

    ``search_fn`` is asked for as many rows as the deepest stage needs (never
    fewer than ``limit``). ``fallback=True`` goes through
    :func:`~brain.search_fallback.search_with_fts_fallback`; ``fallback=False``
    lets an :class:`~brain.errors.EmbedError` propagate (``brain eval
    --answer``, spec §2.7). ``limit`` is named, so it cannot also arrive in
    ``kwargs``; the rest of ``kwargs`` goes to ``search_fn`` unchanged.

    ``stages=None`` means :func:`build_stages` of ``os.environ`` and
    ``embedder``, so a surface that passes nothing gets every configured stage
    rather than silently none; ``stages=()`` runs none. With no stages the rows
    are exactly ``search_fn``'s. After a text-only search (a fallback,
    ``fts_only=True``, an embedder with no vectors) a stage that needs the query
    vector is reported skipped and not called (:func:`apply_stages`).

    Never raises from a stage. A stage whose ``candidate_limit`` raises
    contributes ``limit`` to the fetch, is NOT applied, and is reported as
    skipped (class-only), like a stage whose ``apply`` raises (:func:`size_stages`).

    ``rewriter`` (RM-54, ``--rewrite``) adds paraphrases fused with the original before the
    stages, which then see the fused rows and the ORIGINAL query. ``None`` is the path without it,
    unchanged.
    """
    stages, fetch = size_stages(
        build_stages(os.environ, embedder=embedder) if stages is None else stages, limit
    )
    if rewriter is not None:
        fetch = max(fetch, limit * REWRITE_FETCH_FACTOR)
    if fallback:
        searched = search_with_fts_fallback(
            search_fn, conn, embedder=embedder, query=query, limit=fetch, **kwargs
        )
        results, fell_back = searched.results, searched.fallback
    else:
        results = search_fn(
            conn, embedder=embedder, query=query, limit=fetch, **kwargs
        )
        fell_back = None
    text_only = search_was_text_only(
        embedder, fallback=fell_back, fts_only=bool(kwargs.get("fts_only"))
    )
    fused: FusedDocs | None = None
    leading: tuple[StageReport, ...] = ()
    if rewriter is not None:
        results, fused, rewrite_report = _rewrite_leg(
            search_fn,
            conn,
            rewriter=rewriter,
            embedder=embedder,
            query=query,
            limit=fetch,
            results=results,
            fell_back=fell_back is not None,
            kwargs=kwargs,
        )
        leading = (rewrite_report,)
    results, reports = apply_stages(
        conn,
        query=query,
        session_id=session_id,
        results=results,
        stages=stages,
        text_only=text_only,
    )
    return PipelineResult(
        results=results[:limit], fallback=fell_back, stages=(*leading, *reports), fused=fused
    )


def stage_reports_json(reports: Sequence[StageReport]) -> dict[str, Any]:
    """Project the reports as one always-present ``stages`` key.

    Shared by the ``brain search --json --meta`` envelope and MCP
    ``brain_search`` so the two cannot describe a stage differently. ``[]``
    means no stage is configured. ``ms`` is rounded to 0.1 ms, as
    ``timing_ms`` is.
    """
    return {
        "stages": [
            {
                "name": report.name,
                "applied": report.applied,
                "skipped_reason": report.skipped_reason,
                "ms": round(report.ms, 1),
            }
            for report in reports
        ]
    }


def pipeline_json(result: PipelineResult) -> dict[str, Any]:
    """The five always-present pipeline keys: the four ``fallback*``, then ``stages``.

    MCP ``brain_search`` splats this; the ``--json --meta`` envelope emits the
    same two projections in the same order (``format_search``).
    """
    return {**fallback_json(result.fallback), **stage_reports_json(result.stages)}
