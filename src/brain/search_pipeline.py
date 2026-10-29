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
pipeline and never falls back (spec §2.7) and so will apply stages itself with
:func:`apply_stages` (RM-14a); :mod:`brain.graph_rag.fuse`; and
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

    name: str

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


def build_stages(env: Mapping[str, str]) -> tuple[SearchStage, ...]:
    """The stages every surface applies, read from the settings leaves.

    ``env`` is ``os.environ`` after the surface's ``Config.load()`` has
    published the dotenv chain into it (spec §2.3). No stage has shipped yet,
    so this is empty; RM-14a adds the reranker here, which is what puts it on
    every surface at once.
    """
    del env  # read by the first stage that ships (RM-14a)
    return ()


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
    """
    if stages is None:
        stages = build_stages(os.environ)
    fetch = max([limit, *(stage.candidate_limit(limit) for stage in stages)])
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
