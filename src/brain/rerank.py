"""The cross-encoder reranker stage (roadmap ticket RM-14a, decision G7).

:class:`RerankStage` is a :class:`brain.search_pipeline.SearchStage`: it scores
the first ``depth`` rows of a search with a :class:`Reranker` and reorders that
head by score, descending. Rows past the head keep their order after it. It is
wired once, in :func:`brain.search_pipeline.build_stages`, so it reaches every
surface on the pipeline, and it ships **off** (``BRAIN_RERANKER=none``).

Three rulings shape it:

- **``SearchResult.score`` is not overwritten** (RM-14a.D1). It stays the
  RRF-plus-recency score; the reranker's score goes to
  ``explain.reranker_score`` when the row has an explanation. A reranked list
  can therefore be non-monotonic in ``score``.
- **A hosted reranker never receives a confidential passage** (spec §2.5). The
  stage looks the head's sensitivity up in one statement, scores the rest, and
  leaves each confidential row at its original index.
- **Search never fails because of the reranker.** A timeout, a dead backend or
  a malformed response returns the input unchanged, with a class-only
  ``skipped_reason``; the CLI says so on stderr (:func:`rerank_notice`).

The backends (``llama-server`` over loopback, Voyage) are in
:mod:`brain.rerank_backends`; the knobs are in :mod:`brain.settings.rerank`.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

import psycopg

from .errors import BrainError, describe_failure
from .search import SearchResult
from .search_pipeline import StageOutcome, StageReport
from .sensitivity import CONFIDENTIAL

logger = logging.getLogger(__name__)

#: The stage's name in ``StageReport``s, the ``stages`` key and timings.
RERANK_STAGE_NAME = "rerank"

#: The stderr line the CLI prints when the stage was skipped.
RERANK_SKIPPED_NOTICE = "reranker unavailable; results are in hybrid order"

_CONFIDENTIAL_IDS_SQL = """
    SELECT id::text
    FROM documents
    WHERE id = ANY(%s::uuid[]) AND sensitivity = %s
"""


class RerankError(BrainError):
    """A reranker backend failed: transport, HTTP status, API error or a malformed reply.

    Backends raise it chained (``from exc``), so :func:`describe_failure` names
    both classes (``RerankError from ReadTimeout``) and never the message.
    """


class Reranker(Protocol):
    """A cross-encoder that scores ``(query, passage)`` pairs."""

    @property
    def name(self) -> str:
        """The backend: ``"llamacpp"`` or ``"voyage"``."""
        ...

    @property
    def model(self) -> str: ...

    @property
    def hosted_egress(self) -> bool:
        """True when passages leave the machine (mirrors ``embeddings.is_hosted_embedder``)."""
        ...

    def score(self, query: str, passages: Sequence[str], *, timeout: float) -> list[float]:
        """One score per passage, in passage order. Higher is more relevant."""
        ...


def _confidential_ids(conn: psycopg.Connection[Any], document_ids: list[str]) -> set[str]:
    """The subset of ``document_ids`` marked confidential: one round trip."""
    if not document_ids:
        return set()
    rows = conn.execute(_CONFIDENTIAL_IDS_SQL, (document_ids, CONFIDENTIAL)).fetchall()
    return {str(row[0]) for row in rows}


def _passage(result: SearchResult, max_chars: int) -> str:
    """``title + "\\n" + snippet``, cut to ``max_chars``."""
    return f"{result.title}\n{result.snippet}"[:max_chars]


def _checked(scores: Sequence[float], expected: int) -> list[float]:
    """Refuse a reply that cannot be mapped back onto the passages."""
    if len(scores) != expected:
        raise RerankError(f"reranker returned {len(scores)} scores for {expected} passages")
    checked = [float(s) for s in scores]
    if not all(math.isfinite(s) for s in checked):
        raise RerankError("reranker returned a non-finite score")
    return checked


def _with_score(result: SearchResult, score: float) -> SearchResult:
    """``result`` with ``explain.reranker_score`` set; unchanged when it has no explanation."""
    if result.explain is None:
        return result
    return replace(result, explain=replace(result.explain, reranker_score=score))


@dataclass(frozen=True)
class RerankStage:
    """Reorders the first ``depth`` rows by a :class:`Reranker`'s scores."""

    reranker: Reranker
    depth: int
    max_chars: int
    timeout: float
    name: str = RERANK_STAGE_NAME

    def candidate_limit(self, limit: int) -> int:
        return max(limit, self.depth)

    def signature(self) -> dict[str, Any]:
        return {
            "reranker": self.reranker.name,
            "reranker_model": self.reranker.model,
            "rerank_depth": self.depth,
        }

    def apply(
        self,
        conn: psycopg.Connection[Any],
        *,
        query: str,
        session_id: str | None,
        results: list[SearchResult],
    ) -> StageOutcome:
        del session_id  # a reranker scores the query, not the session
        head, tail = results[: self.depth], results[self.depth :]
        withheld = (
            _confidential_ids(conn, [r.document_id for r in head])
            if self.reranker.hosted_egress
            else set()
        )
        slots = [i for i, r in enumerate(head) if r.document_id not in withheld]
        if not slots:
            return self._outcome(results, applied=True)
        try:
            scores = _checked(
                self.reranker.score(
                    query,
                    [_passage(head[i], self.max_chars) for i in slots],
                    timeout=self.timeout,
                ),
                len(slots),
            )
        except RerankError as exc:
            reason = describe_failure(exc)
            # INFO, for the reason ``search_pipeline.apply_stages`` gives: the
            # CLI configures no logging, and it prints its own notice.
            logger.info("rerank skipped: %s", reason)
            return self._outcome(results, applied=False, skipped_reason=reason)
        # ``sorted`` is stable, so equal scores keep their prior order.
        ranked = sorted(zip(scores, slots, strict=True), key=lambda pair: -pair[0])
        reordered = list(head)
        for slot, (score, source) in zip(slots, ranked, strict=True):
            reordered[slot] = _with_score(head[source], score)
        return self._outcome(reordered + tail, applied=True)

    def _outcome(
        self, results: list[SearchResult], *, applied: bool, skipped_reason: str | None = None
    ) -> StageOutcome:
        # ``ms`` is measured by ``apply_stages``, which times every stage alike.
        report = StageReport(
            name=self.name, applied=applied, skipped_reason=skipped_reason, ms=0.0
        )
        return StageOutcome(results=results, report=report)


def rerank_notice(reports: Sequence[StageReport]) -> str | None:
    """The CLI's stderr line when the rerank stage ran and was skipped, else ``None``."""
    for report in reports:
        if report.name == RERANK_STAGE_NAME and not report.applied:
            return RERANK_SKIPPED_NOTICE
    return None
