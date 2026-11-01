"""Connection and work limits for ``brain mcp serve`` (RM-46, sec-118 #3 and #4).

**The idle deadline** (:func:`idle_deadline_protocol`). uvicorn arms its
keep-alive timer only once a response completes, and cancels it as soon as any
byte arrives. A client that connects and sends nothing, or dribbles one header
byte at a time, therefore holds a connection forever, and uvicorn's connection
cap then answers everyone 503. The subclass arms a separate deadline at connect
and again after every response. Received bytes do NOT cancel it. When it
fires, the connection is closed unless a request is in flight (its headers
complete). In-flight work is bounded by the per-call deadline instead.

**The work limit** (:class:`WorkLimitMiddleware`). Connections are cheap and
capped loosely (``MAX_CONNECTIONS``); WORK is capped tightly, in the app:

- at most ``max_calls`` POSTs in flight (503 beyond);
- the event loop's worker-thread limiter set to the same number, so sync tools
  share ``max_calls`` threads.

The response guard's 504 stops the wait, not a sync tool already on a worker
thread. That thread keeps its limiter token until it returns, so orphaned work
stays counted. A call that would only queue behind orphans is answered 503 at
once instead of waiting into its own 504.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import anyio

logger = logging.getLogger("brain.mcp.http")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: Seconds a connection may sit without a request in flight: before its first
#: request's headers are complete, and between keep-alive requests.
IDLE_TIMEOUT_S = 10.0
#: Open connections uvicorn accepts before answering 503. Loose on purpose:
#: idle ones are closed by the deadline, and work has its own cap.
MAX_CONNECTIONS = 256
#: POSTs in flight, and worker threads for sync tools (each holds a database
#: connection while it runs).
MAX_CALLS = 16


def idle_deadline_protocol(idle_timeout_s: float) -> type[asyncio.Protocol]:
    """uvicorn's h11 protocol with an idle deadline that bytes cannot reset.

    Built on demand, so importing this module never imports uvicorn.
    """
    from uvicorn.protocols.http.h11_impl import H11Protocol

    class IdleDeadlineProtocol(H11Protocol):
        """Closes a connection that has had no request in flight for ``idle_timeout_s``."""

        _idle_deadline: asyncio.TimerHandle | None = None

        def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
            super().connection_made(transport)
            self._arm_idle_deadline()

        def on_response_complete(self) -> None:
            super().on_response_complete()
            self._arm_idle_deadline()

        def connection_lost(self, exc: Exception | None) -> None:
            self._disarm_idle_deadline()
            super().connection_lost(exc)

        def _arm_idle_deadline(self) -> None:
            self._disarm_idle_deadline()
            self._idle_deadline = self.loop.call_later(idle_timeout_s, self._idle_deadline_expired)

        def _disarm_idle_deadline(self) -> None:
            if self._idle_deadline is not None:
                self._idle_deadline.cancel()
                self._idle_deadline = None

        def _idle_deadline_expired(self) -> None:
            self._idle_deadline = None
            if self.cycle is not None and not self.cycle.response_complete:
                return  # a request is being served; on_response_complete re-arms
            if not self.transport.is_closing():
                self.transport.close()

    return IdleDeadlineProtocol


async def _send_busy(send: Send) -> None:
    # Local import: mcp_http_guard holds the one JSON-response helper.
    from .mcp_http_guard import send_json

    await send_json(send, 503, {"error": "busy"})


class WorkLimitMiddleware:
    """Admit at most ``max_calls`` POSTs, and none that would only wait on orphaned workers."""

    def __init__(self, app: ASGIApp, *, max_calls: int) -> None:
        self._app = app
        self._max = max_calls
        self._calls: anyio.CapacityLimiter | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self._app(scope, receive, send)
            return
        threads = anyio.to_thread.current_default_thread_limiter()
        if threads.total_tokens != self._max:
            threads.total_tokens = self._max
        if self._calls is None:
            self._calls = anyio.CapacityLimiter(self._max)
        if threads.available_tokens < 1:
            logger.warning("refused a call: every worker thread is still busy")
            await _send_busy(send)
            return
        try:
            self._calls.acquire_nowait()
        except anyio.WouldBlock:
            logger.warning("refused a call: %d already in flight", self._max)
            await _send_busy(send)
            return
        try:
            await self._app(scope, receive, send)
        finally:
            self._calls.release()
