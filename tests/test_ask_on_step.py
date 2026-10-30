"""`ask`'s ``on_step`` hook: the Agent tab's progress and cancel points (RM-18a, A3/A4).

Unit tests over a scripted chat and a stubbed retriever-free corpus: the hybrid leg
runs against the 5434 test database with a fake embedder, so no Ollama is needed.
All data is synthetic.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from brain.ask import ask, ask_no_loop
from brain.config import Config
from brain.errors import BrainError
from tests.conftest import FakeEmbedder


def _cfg() -> Config:
    return Config(database_url="postgresql://unused/none")


class _Chat:
    """Returns queued bodies in call order; records which step each call was."""

    def __init__(self, bodies: list[dict[str, Any]], log: list[str]) -> None:
        self._bodies = list(bodies)
        self._log = log

    def __call__(self, prompt: str, *, schema: dict[str, Any], **_: Any) -> dict[str, Any]:
        self._log.append("chat:" + ",".join(sorted(schema)))
        return self._bodies.pop(0)


class _Steps:
    def __init__(self, log: list[str]) -> None:
        self.calls: list[tuple[str, int]] = []
        self._log = log

    def __call__(self, step: str, iteration: int) -> None:
        self.calls.append((step, iteration))
        self._log.append(f"step:{step}")


def test_loop_reports_each_step_in_order_before_it_runs(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    log: list[str] = []
    steps = _Steps(log)
    chat = _Chat(
        [
            {"sub_queries": ["alpha"]},
            {"sufficient": False, "follow_up_queries": ["beta"]},
            {"answer": "No sources."},
        ],
        log,
    )
    seed_doc(title="Alpha note", content="alpha body")

    ask(
        test_db, _cfg(), embedder=FakeEmbedder(), chat=chat, question="q",
        max_iterations=2, on_step=steps,
    )

    assert steps.calls == [
        ("plan", 1), ("retrieve", 1), ("reflect", 1), ("retrieve", 2), ("synthesize", 2),
    ]
    # Each chat call is preceded by its own step report.
    assert log == [
        "step:plan", "chat:sub_queries",
        "step:retrieve", "step:reflect", "chat:follow_up_queries,sufficient",
        "step:retrieve", "step:synthesize", "chat:answer",
    ]


def test_no_loop_reports_retrieve_then_synthesize(test_db: psycopg.Connection[Any]) -> None:
    log: list[str] = []
    steps = _Steps(log)
    chat = _Chat([{"answer": "Nothing."}], log)

    ask_no_loop(
        test_db, _cfg(), embedder=FakeEmbedder(), chat=chat, question="q", on_step=steps
    )

    assert steps.calls == [("retrieve", 1), ("synthesize", 1)]


def test_ask_forwards_on_step_to_the_no_loop_path(test_db: psycopg.Connection[Any]) -> None:
    log: list[str] = []
    steps = _Steps(log)

    ask(
        test_db, _cfg(), embedder=FakeEmbedder(), chat=_Chat([{"answer": "x"}], log),
        question="q", no_loop=True, on_step=steps,
    )

    assert steps.calls == [("retrieve", 1), ("synthesize", 1)]


class _Stop(BrainError):
    """Stands in for ``brain.ui.jobs.JobCancelled`` (also a ``BrainError``)."""


def test_an_exception_from_on_step_propagates_out_of_ask(
    test_db: psycopg.Connection[Any],
) -> None:
    # A4: ask has no except clause, so a cancel raised at a step boundary reaches
    # the job runner and is never turned into a partial answer.
    log: list[str] = []
    chat = _Chat([{"sub_queries": ["alpha"]}, {"answer": "never"}], log)

    def stop_at_retrieve(step: str, iteration: int) -> None:
        if step == "retrieve":
            raise _Stop("cancel requested")

    with pytest.raises(_Stop):
        ask(
            test_db, _cfg(), embedder=FakeEmbedder(), chat=chat, question="q",
            on_step=stop_at_retrieve,
        )
    assert log == ["chat:sub_queries"]  # synthesis never ran
