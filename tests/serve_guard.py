"""Serve tests fail at once instead of listening when a refusal regresses (RM-46, rev-118 round 3).

``serve_http``'s default runner calls ``uvicorn.Server.run``, which blocks on a
real port (127.0.0.1:8787 by default) until the process is stopped. A test that
expects ``brain mcp serve`` to refuse, and does not get the refusal, therefore
hung: rev-118's mutant without ``check_uvicorn_internals`` ran for ten minutes.
Each serve test module applies :func:`never_listens`, so reaching ``Server.run``
fails that test immediately, naming why.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

REACHED_SERVER_RUN = (
    "brain mcp serve reached uvicorn.Server.run: a refusal this test expects did not happen"
)


@pytest.fixture(autouse=True)
def never_listens(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``uvicorn.Server.run`` fails the test instead of serving."""
    import uvicorn

    def refuse(self: Any, *args: Any, **kwargs: Any) -> None:
        pytest.fail(REACHED_SERVER_RUN, pytrace=False)

    monkeypatch.setattr(uvicorn.Server, "run", refuse)
    yield
