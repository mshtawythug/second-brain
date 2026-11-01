"""Connection and work limits for ``brain mcp serve`` (RM-46, sec-118 #3 and #4).

#3: uvicorn arms its keep-alive timer only after a response, and cancels it on any
received byte, so a client that connects and sends nothing (or one byte now and
then) held a connection slot forever, and 16 such clients answered every request
503. The idle deadline closes a connection that has no request in flight.

#4: the 504 deadline stops the WAIT, not a sync tool already on a worker thread.
The worker-thread limiter is capped at the call limit, an orphaned thread keeps
its token until it returns, and a call that would wait on orphans is answered 503
at once.

The protocol tests run a real uvicorn on an ephemeral loopback port in a thread;
nothing else listens and the server is stopped in a ``finally``.
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import anyio
import httpx
import pytest
import uvicorn

from brain.mcp_http_conn import WorkLimitMiddleware, idle_deadline_protocol
from brain.mcp_http_guard import HealthzRoute, send_json

pytestmark = pytest.mark.nodb

IDLE_S = 0.5


async def _slow_or_fast(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Answers ``/slow`` after a second (longer than the idle deadline)."""
    if scope["type"] != "http":
        return
    if scope["path"] == "/slow":
        await anyio.sleep(1.0)
    await send_json(send, 200, {"path": scope["path"]})


@contextmanager
def _server(*, limit: int = 4) -> Iterator[int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        HealthzRoute(_slow_or_fast),
        http=idle_deadline_protocol(IDLE_S),
        limit_concurrency=limit,
        log_level="warning",
        lifespan="off",
        timeout_keep_alive=5,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        assert not thread.is_alive()


def _closed_by_server(conn: socket.socket, within: float) -> bool:
    conn.settimeout(within)
    try:
        return conn.recv(1) == b""
    except TimeoutError:
        return False
    except ConnectionResetError:
        return True


def test_idle_connections_are_closed_and_the_server_answers_again() -> None:
    with _server(limit=4) as port:
        idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(4)]
        try:
            assert all(_closed_by_server(conn, within=IDLE_S * 6) for conn in idle)
            response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=5)
        finally:
            for conn in idle:
                conn.close()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_a_client_dribbling_headers_is_closed() -> None:
    with _server() as port:
        conn = socket.create_connection(("127.0.0.1", port))
        try:
            conn.sendall(b"GET /healthz HTTP/1.1\r\n")
            closed = False
            started = time.monotonic()
            while time.monotonic() - started < IDLE_S * 8:
                try:
                    conn.sendall(b"X")
                except (BrokenPipeError, ConnectionResetError):
                    closed = True
                    break
                if _closed_by_server(conn, within=0.1):
                    closed = True
                    break
        finally:
            conn.close()

    assert closed


def test_a_keep_alive_client_reuses_its_connection() -> None:
    with _server() as port, httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
        responses = [client.get("/healthz") for _ in range(3)]

    assert [r.status_code for r in responses] == [200, 200, 200]


def test_a_request_slower_than_the_idle_deadline_still_completes() -> None:
    with _server() as port:
        response = httpx.get(f"http://127.0.0.1:{port}/slow", timeout=10)

    assert response.status_code == 200
    assert response.json() == {"path": "/slow"}


# ---------------------------------------------------------------- the work limit


_POST = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}


async def _receive() -> Any:
    return {"type": "http.request", "body": b"", "more_body": False}


def test_a_post_past_the_call_limit_is_503() -> None:
    async def run() -> list[int]:
        release = anyio.Event()
        entered = anyio.Event()
        statuses: list[int] = []

        async def gated(scope: Any, receive: Any, send: Any) -> None:
            entered.set()
            await release.wait()
            await send_json(send, 200, {})

        async def send(message: Any) -> None:
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        app = WorkLimitMiddleware(gated, max_calls=1)
        async with anyio.create_task_group() as group:
            group.start_soon(app, _POST, _receive, send)
            await entered.wait()
            await app(_POST, _receive, send)  # the one slot is taken: refused
            release.set()
        await app(_POST, _receive, send)  # free again (answers at once: release is set)
        return statuses

    assert anyio.run(run) == [503, 200, 200]


def test_a_get_is_never_counted() -> None:
    async def run() -> list[int]:
        statuses: list[int] = []

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            await send_json(send, 200, {})

        async def send(message: Any) -> None:
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        app = WorkLimitMiddleware(inner, max_calls=1)
        get = {"type": "http", "method": "GET", "path": "/healthz", "headers": []}
        threads = anyio.to_thread.current_default_thread_limiter()
        threads.total_tokens = 1
        owner = object()
        await threads.acquire_on_behalf_of(owner)  # every worker busy: a GET still passes
        for _ in range(3):
            await app(get, _receive, send)
        threads.release_on_behalf_of(owner)
        return statuses

    assert anyio.run(run) == [200, 200, 200]


def test_a_call_that_would_wait_on_orphaned_workers_is_503() -> None:
    async def run() -> list[int]:
        statuses: list[int] = []

        async def send(message: Any) -> None:
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        async def receive() -> Any:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            await send_json(send, 200, {})

        app = WorkLimitMiddleware(inner, max_calls=2)
        scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}
        await app(scope, receive, send)  # sets the thread limiter to 2
        threads = anyio.to_thread.current_default_thread_limiter()
        orphans = [object(), object()]
        for owner in orphans:
            await threads.acquire_on_behalf_of(owner)
        await app(scope, receive, send)  # every worker held: refused
        for owner in orphans:
            threads.release_on_behalf_of(owner)
        await app(scope, receive, send)  # a worker free again
        return statuses

    assert anyio.run(run) == [200, 503, 200]


def test_the_worker_thread_limit_follows_the_call_limit() -> None:
    async def run() -> float:
        async def inner(scope: Any, receive: Any, send: Any) -> None:
            await send_json(send, 200, {})

        async def receive() -> Any:
            return {"type": "http.request", "body": b""}

        async def send(message: Any) -> None:
            return None

        await WorkLimitMiddleware(inner, max_calls=3)(
            {"type": "http", "method": "POST", "path": "/mcp", "headers": []}, receive, send
        )
        return anyio.to_thread.current_default_thread_limiter().total_tokens

    assert anyio.run(run) == 3


def test_lifespan_passes_the_work_limit() -> None:
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    async def noop(*_: Any) -> Any:
        return {}

    anyio.run(WorkLimitMiddleware(inner, max_calls=1), {"type": "lifespan"}, noop, noop)

    assert seen == ["lifespan"]


class _Transport:
    def __init__(self, *, closing: bool) -> None:
        self.closing = closing
        self.closed = 0

    def is_closing(self) -> bool:
        return self.closing

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize(("closing", "closes"), [(False, 1), (True, 0)])
def test_the_deadline_closes_an_idle_connection_once(closing: bool, closes: int) -> None:
    cls = idle_deadline_protocol(IDLE_S)
    protocol = cls.__new__(cls)  # the handler needs only these attributes
    protocol.cycle = None  # type: ignore[assignment]
    protocol.transport = _Transport(closing=closing)  # type: ignore[assignment]

    protocol._idle_deadline_expired()

    assert protocol.transport.closed == closes  # type: ignore[attr-defined]
