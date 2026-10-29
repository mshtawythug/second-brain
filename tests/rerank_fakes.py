"""Test doubles: rerankers for the RM-14a tests.

Each fake implements :class:`brain.rerank.Reranker` and records every call, so
a test can see exactly which passages left the process. All data is synthetic.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from brain.rerank import RerankError
from brain.search import SearchResult

#: Text placed in every raised reranker error. It must never reach a report.
RERANK_SECRET = "synthetic-rerank-detail-5d21"


class FakeReranker:
    """Scores each passage with ``scorer(passage)``; records every call."""

    def __init__(
        self,
        scorer: Callable[[str], float],
        *,
        name: str = "fake",
        model: str = "fake-model",
        hosted_egress: bool = False,
    ) -> None:
        self.name = name
        self.model = model
        self.hosted_egress = hosted_egress
        self._scorer = scorer
        self.calls: list[dict[str, Any]] = []

    def score(self, query: str, passages: Sequence[str], *, timeout: float) -> list[float]:
        self.calls.append({"query": query, "passages": list(passages), "timeout": timeout})
        return [self._scorer(passage) for passage in passages]

    @property
    def passages_sent(self) -> list[str]:
        return [p for call in self.calls for p in call["passages"]]


def reverse_scorer(passages_in_order: Sequence[str]) -> Callable[[str], float]:
    """Score so the LAST of ``passages_in_order`` wins: the visible reordering."""
    rank = {p: i for i, p in enumerate(passages_in_order)}
    return lambda passage: float(rank.get(passage, -1))


def title_scorer(order: Sequence[str]) -> Callable[[str], float]:
    """Score a passage by where its title (the first line) sits in ``order``: first is best."""
    position = {title: len(order) - i for i, title in enumerate(order)}
    return lambda passage: float(position.get(passage.split("\n", 1)[0], 0))


class RaisingReranker(FakeReranker):
    """Raises ``exc_type`` from ``score``, as a dead or slow backend would."""

    def __init__(self, exc_type: type[Exception] = RerankError, **kwargs: Any) -> None:
        super().__init__(lambda _p: 0.0, **kwargs)
        self._exc_type = exc_type

    def score(self, query: str, passages: Sequence[str], *, timeout: float) -> list[float]:
        self.calls.append({"query": query, "passages": list(passages), "timeout": timeout})
        raise self._exc_type(RERANK_SECRET)


class ShortReranker(FakeReranker):
    """Returns one score too few: a malformed backend response."""

    def score(self, query: str, passages: Sequence[str], *, timeout: float) -> list[float]:
        return super().score(query, passages, timeout=timeout)[:-1]


def ids(results: Sequence[SearchResult]) -> list[str]:
    return [r.document_id for r in results]
