"""Test doubles for the document-leg (RM-20) tests: a fake connection and a vector stage.

``FakeVectorConn`` answers the stage's one lookup statement from a dict, so the
stage's logic is testable without a database. ``VectorReverseStage`` declares
``needs_query_vector`` so the pipeline's text-only gate has something to skip.
All data is synthetic.
"""
from __future__ import annotations

from typing import Any

from brain.search import SearchResult
from tests.search_stages import ReverseStage


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeVectorConn:
    """Returns ``cosines[doc_id]`` for every requested id that has one, under ``identity``."""

    def __init__(self, cosines: dict[str, float], *, identity: str | None = None) -> None:
        self.cosines = cosines
        self.identity = identity
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...]) -> _Rows:
        self.calls.append((sql, params))
        _query_vector, ids, identity = params
        if self.identity is not None and identity != self.identity:
            return _Rows([])
        return _Rows([(i, self.cosines[i]) for i in ids if i in self.cosines])


class VectorReverseStage(ReverseStage):
    """A :class:`ReverseStage` that needs the query vector, as the document leg does."""

    needs_query_vector = True


def result(n: int) -> SearchResult:
    """A synthetic row whose id is ``doc-<n>``."""
    return SearchResult(
        document_id=f"doc-{n}",
        title=f"Synthetic note {n}",
        source_kind="manual",
        snippet=f"snippet {n}",
        score=1.0 / (n + 1),
        content_type="note",
        tags=[],
    )
