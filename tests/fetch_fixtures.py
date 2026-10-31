"""A transport that never answers, for the ``--fetch`` deadline and breaker tests (RM-41)."""
from __future__ import annotations

import threading

import httpx


class StallingTransport(httpx.BaseTransport):
    """Never answers until released, like a server trickling its headers.

    ``calls`` counts the requests that reached it. Release it in a ``finally`` so the
    abandoned fetch workers it holds can end.
    """

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.calls += 1
        self.entered.set()
        self.release.wait(30)
        raise httpx.ReadTimeout("released", request=request)
