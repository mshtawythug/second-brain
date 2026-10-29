"""One search pipeline for every user-facing surface (roadmap ticket RM-09, G4).

``brain search``, ``brain explain``, MCP ``brain_search``, ``brain ui``'s
``GET /api/search``, ``brain recall`` and the retrieval of ``brain ask`` all
search through :func:`run_search`. It composes, in one order, what used to be
wired per surface: the search itself (``hybrid_search`` or a surface's patch
point), the text-only fallback (:mod:`brain.search_fallback`), and the ranking
STAGES a later ticket adds (a reranker, RM-14a; a document leg, RM-20;
engagement, RM-22b; a query rewrite, RM-54). A new stage is wired once, in
:func:`build_stages`, and reaches every surface. No stage edits the ranking code
in :mod:`brain.search`.

Stages see :class:`~brain.search.SearchResult` rows, not chunks (RM-09.D1): a
stage that needs chunk text reads the snippet, which already carries
``snippet_context_tokens`` of context.

Out of scope, each with its recorded reason for calling ``hybrid_search``
directly: ``brain eval`` (:mod:`brain.eval.runner`), which measures the real
pipeline and never falls back (spec §2.7) and so applies stages itself with
:func:`size_stages` and :func:`apply_stages` (RM-14a); :mod:`brain.graph_rag.fuse`; and
:func:`brain.demo.query_demo`. ``tests/test_search_pipeline_boundary.py`` fails
on any other module that calls ``hybrid_search`` or ``search_with_fts_fallback``.

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
from .search import SearchResult
from .search_fallback import (
    SearchFallback,
    SearchFn,
    fallback_json,
    search_with_fts_fallback,
)

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class PipelineResult:
    """A search's rows, the fallback that produced them, and each stage's report."""

    results: list[SearchResult]
    fallback: SearchFallback | None
    stages: tuple[StageReport, ...]


def build_stages(
    env: Mapping[str, str], *, rerank: bool | None = None
) -> tuple[SearchStage, ...]:
    """The stages every surface applies, read from the settings leaves.

    ``env`` is ``os.environ`` after the surface's ``Config.load()`` has
    published the dotenv chain into it (spec §2.3). The reranker (RM-14a) is
    added when ``BRAIN_RERANKER`` names a backend; it ships ``none``, so by
    default this is empty and every surface is unchanged.

    ``rerank`` is a one-run override (``brain search --rerank/--no-rerank``):
    ``False`` drops the stage, ``True`` requires a configured backend (a
    ``ConfigError`` otherwise: picking one would choose between a local and a
    hosted service for the user), and ``None`` follows the setting. A bad
    rerank knob is a ``ConfigError`` here even while the stage is off.
    """
    # Imported here: ``brain.rerank`` imports this module's ``StageOutcome``
    # and ``StageReport``, so a top-level import would be a cycle.
    from . import rerank_backends
    from .config import ConfigError
    from .rerank import RerankStage
    from .settings.rerank import load_rerank_settings

    settings = load_rerank_settings(env)
    if rerank is True and not settings.enabled:
        raise ConfigError(
            "--rerank needs BRAIN_RERANKER set to llamacpp or voyage (it is none)"
        )
    if not settings.enabled or rerank is False:
        return ()
    return (
        RerankStage(
            reranker=rerank_backends.make_reranker(settings),
            depth=settings.depth,
            max_chars=settings.max_chars,
            timeout=settings.timeout_s,
        ),
    )


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
) -> tuple[list[SearchResult], tuple[StageReport, ...]]:
    """Run ``stages`` over ``results`` in order. Never raises.

    Each report is named after the stage and timed here, so every stage is
    measured the same way. A stage that reports ``applied=False`` has its INPUT
    passed on, whatever rows it returned: a stage that did not apply has not
    changed the ranking.
    """
    reports: list[StageReport] = []
    for stage in stages:
        started = perf_counter()
        skipped_reason: str | None
        try:
            outcome = stage.apply(
                conn, query=query, session_id=session_id, results=results
            )
        # The one broad catch in the search path, and deliberate: a stage is an
        # optional improvement, so ANY failure inside one (a timeout, a dead
        # backend, a bug) must cost that stage, never the search (RM-09).
        except Exception as exc:  # noqa: BLE001
            reason = describe_failure(exc)
            # INFO, not WARNING, for the reason ``search_with_fts_fallback``
            # gives: the CLI configures no logging, so a WARNING would print an
            # unformatted line to stderr.
            logger.info("search stage %s skipped: %s", stage.name, reason)
            applied, skipped_reason = False, reason
        else:
            applied = outcome.report.applied
            skipped_reason = outcome.report.skipped_reason
            if applied:
                results = outcome.results
        reports.append(
            StageReport(
                name=stage.name,
                applied=applied,
                skipped_reason=skipped_reason,
                ms=(perf_counter() - started) * 1000.0,
            )
        )
    return results, tuple(reports)


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
    **kwargs: Any,
) -> PipelineResult:
    """Search, fall back if asked to, apply the stages, and truncate to ``limit``.

    ``search_fn`` is asked for as many rows as the deepest stage needs (never
    fewer than ``limit``). ``fallback=True`` goes through
    :func:`~brain.search_fallback.search_with_fts_fallback`; ``fallback=False``
    lets an :class:`~brain.errors.EmbedError` propagate (``brain eval
    --answer``, spec §2.7). ``limit`` is named, so it cannot also arrive in
    ``kwargs``; the rest of ``kwargs`` goes to ``search_fn`` unchanged.

    ``stages=None`` means :func:`build_stages` of ``os.environ``, so a surface
    that passes nothing gets every configured stage rather than silently none;
    ``stages=()`` runs none. With no stages the rows are exactly ``search_fn``'s.

    Never raises from a stage. A stage whose ``candidate_limit`` raises
    contributes ``limit`` to the fetch, is NOT applied, and is reported as
    skipped (class-only), like a stage whose ``apply`` raises (:func:`size_stages`).
    """
    stages, fetch = size_stages(
        build_stages(os.environ) if stages is None else stages, limit
    )
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
    results, reports = apply_stages(
        conn, query=query, session_id=session_id, results=results, stages=stages
    )
    return PipelineResult(results=results[:limit], fallback=fell_back, stages=reports)


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
