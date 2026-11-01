"""A deadline an ask may not pass, checked between its steps (RM-18a, RM-46).

Shared by the UI's Agent tab (:mod:`brain.ui.ask_service`, whose job budget is
``ask_max_iterations × 3 × ask_timeout_seconds``) and by ``brain_ask`` served over
HTTP, whose budget is ``--call-timeout`` (sec-118c LOW-2): the guard answers 504
at that point but the call keeps its work-limit slot until the tool returns, so
the ask itself has to stop. It stops at its next step boundary, so a timed-out
ask runs at most one more step (one chat call or one retrieval) past it.

A leaf: it imports nothing but the error base, so ``brain.ui.app`` may load it.
"""
from __future__ import annotations

from collections.abc import Callable

from .errors import BrainError


class AskTimedOut(BrainError):
    """The ask passed its deadline. The UI maps it to the ``timeout`` code."""


class Deadline:
    """A point on ``clock`` the job may not pass. Reaching it exactly is not late."""

    def __init__(self, clock: Callable[[], float], seconds: float) -> None:
        self._clock = clock
        self._ends = clock() + seconds

    def check(self) -> None:
        if self._clock() > self._ends:
            raise AskTimedOut("the ask job passed its deadline")


def deadline_on_step(deadline: Deadline) -> Callable[[str, int], None]:
    """An ``on_step`` hook for :func:`brain.ask.ask` that checks ``deadline``."""

    def on_step(_step: str, _iteration: int) -> None:
        deadline.check()

    return on_step
