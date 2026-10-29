"""Test doubles: search stages for the RM-09 pipeline tests.

Each class implements :class:`brain.search_pipeline.SearchStage` and records the
calls it received, so a test can see what the pipeline handed it. The raised
message carries :data:`STAGE_SECRET` so a test can prove only the exception's
class reaches a report. All data is synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.search import SearchResult
from brain.search_pipeline import StageOutcome, StageReport

#: Text placed in every raised stage error. It must never reach a report.
STAGE_SECRET = "synthetic-stage-detail-41c9"


class StageBackendDown(RuntimeError):
    """What a stage raises when its backend is unreachable."""


class _RecordingStage:
    """Shared bookkeeping: the name, and the inputs of every ``apply`` call."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    def candidate_limit(self, limit: int) -> int:
        return limit

    def signature(self) -> dict[str, Any]:
        return {self.name: True}

    def _record(
        self, query: str, session_id: str | None, results: list[SearchResult]
    ) -> None:
        self.calls.append(
            {"query": query, "session_id": session_id, "results": list(results)}
        )


class ReverseStage(_RecordingStage):
    """Reverses the result order: the cheapest visible reordering."""

    def __init__(self, name: str = "reverse", *, depth: int | None = None) -> None:
        super().__init__(name)
        self._depth = depth

    def candidate_limit(self, limit: int) -> int:
        return limit if self._depth is None else self._depth

    def apply(
        self,
        conn: psycopg.Connection[Any] | None,
        *,
        query: str,
        session_id: str | None,
        results: list[SearchResult],
    ) -> StageOutcome:
        self._record(query, session_id, results)
        return StageOutcome(
            results=list(reversed(results)),
            report=StageReport(name=self.name, applied=True, skipped_reason=None, ms=0.0),
        )


class RaisingStage(_RecordingStage):
    """Raises from ``apply``, as a stage with a dead backend would."""

    def __init__(self, name: str = "raising") -> None:
        super().__init__(name)

    def apply(
        self,
        conn: psycopg.Connection[Any] | None,
        *,
        query: str,
        session_id: str | None,
        results: list[SearchResult],
    ) -> StageOutcome:
        self._record(query, session_id, results)
        raise StageBackendDown(f"connection refused: {STAGE_SECRET}")


class DeclinedStage(_RecordingStage):
    """Declines (``applied=False``) but returns different rows anyway.

    The pipeline must pass its INPUT on, not these rows: a stage that says it
    did not apply has not earned the right to change the ranking.
    """

    def __init__(self, name: str = "declined") -> None:
        super().__init__(name)

    def apply(
        self,
        conn: psycopg.Connection[Any] | None,
        *,
        query: str,
        session_id: str | None,
        results: list[SearchResult],
    ) -> StageOutcome:
        self._record(query, session_id, results)
        return StageOutcome(
            results=[],
            report=StageReport(
                name=self.name, applied=False, skipped_reason="disabled", ms=0.0
            ),
        )
