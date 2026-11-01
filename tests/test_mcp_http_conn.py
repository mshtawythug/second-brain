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
def _server(*, limit: int = 4, app: Any = None) -> Iterator[int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        HealthzRoute(_slow_or_fast) if app is None else app,
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


# ------------------------------------------- rev-118 round 2: uvicorn internals


def test_the_installed_uvicorn_has_every_internal_the_deadline_needs() -> None:
    from brain.mcp_http_conn import check_uvicorn_internals

    check_uvicorn_internals()  # no exception on the version the tests run on


class _NoCycleProtocol:
    def __init__(self) -> None:
        self.loop = None
        self.transport = None

    def connection_made(self, transport: Any) -> None: ...

    def connection_lost(self, exc: Any) -> None: ...

    def on_response_complete(self) -> None: ...


class _Cycle:
    def __init__(self) -> None:
        self.response_complete = False


class _ConfigWithoutHttp:
    def __init__(self, app: Any, host: str = "", limit_concurrency: int = 0) -> None: ...


@pytest.mark.parametrize(
    ("protocol", "cycle", "config", "missing"),
    [
        (_NoCycleProtocol, _Cycle, uvicorn.Config, "cycle"),
        (object, _Cycle, uvicorn.Config, "on_response_complete"),
        (None, _Cycle, uvicorn.Config, "H11Protocol"),
        ("real", object, uvicorn.Config, "response_complete"),
        ("real", _Cycle, _ConfigWithoutHttp, "http"),
    ],
)
def test_missing_internals_fail_closed_naming_what_is_missing(
    protocol: Any, cycle: Any, config: Any, missing: str
) -> None:
    from uvicorn.protocols.http import h11_impl

    from brain.errors import McpTransportError
    from brain.mcp_http_conn import check_uvicorn_internals

    real = h11_impl.H11Protocol if protocol == "real" else protocol

    with pytest.raises(McpTransportError) as excinfo:
        check_uvicorn_internals(protocol=real, cycle=cycle, config=config)

    assert missing in str(excinfo.value)
    assert "0.54.0" in str(excinfo.value)


def test_an_uninspectable_config_counts_as_missing_every_option() -> None:
    from uvicorn.protocols.http import h11_impl

    from brain.errors import McpTransportError
    from brain.mcp_http_conn import check_uvicorn_internals

    with pytest.raises(McpTransportError, match=r"Config\(http=\)"):
        check_uvicorn_internals(
            protocol=h11_impl.H11Protocol, cycle=h11_impl.RequestResponseCycle, config=42
        )


# ------------------------------------- sec-118b N2/N3/N4, on a real uvicorn


SECRET = "A" * 43


@contextmanager
def _mcp_server(server: Any, *, timeout: float = 1.0, max_calls: int = 16) -> Iterator[str]:
    """The full HTTP app around ``server`` on a loopback port, under the real protocol."""
    from brain.mcp_auth import TokenInfo, fingerprint
    from brain.mcp_http import HttpServeOptions, build_http_app, validate_options

    token = TokenInfo(path=None, secret=SECRET, mode_octal="", mode_ok=True,
                      fingerprint=fingerprint(SECRET))
    opts = HttpServeOptions(token=token, call_timeout_s=timeout)

    async def probe() -> dict[str, Any]:
        return {"status": "ok"}

    app = build_http_app(server, opts, validate_options(opts), probe=probe, max_calls=max_calls)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    uv = uvicorn.Server(uvicorn.Config(
        app, http=idle_deadline_protocol(5.0), log_level="warning", lifespan="on"
    ))
    thread = threading.Thread(target=uv.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not uv.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert uv.started
        yield f"http://127.0.0.1:{port}"
    finally:
        uv.should_exit = True
        thread.join(timeout=15)
        sock.close()
        assert not thread.is_alive()


_HEADERS = {
    "Authorization": f"Bearer {SECRET}",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _tool_call(base: str, name: str, arguments: dict[str, Any] | None = None) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}
    return httpx.post(f"{base}/mcp", json=body, headers=_HEADERS, timeout=30)


def _slow_server(seconds: float) -> Any:
    import asyncio

    from brain.mcp_compat import make_server

    server = make_server("throwaway")

    def brain_show(id_prefix: str) -> dict[str, Any]:
        """Blocks its worker thread."""
        time.sleep(seconds)
        return {"id": id_prefix}

    async def brain_status() -> dict[str, Any]:
        """Counts the event loop's tasks."""
        return {"tasks": len(asyncio.all_tasks())}

    def brain_search(query: str) -> dict[str, Any]:
        """Answers at once."""
        return {"echo": query}

    for tool in (brain_show, brain_status, brain_search):
        server.tool()(tool)
    return server


def _tasks(base: str) -> int:
    return int(_tool_call(base, "brain_status").json()["result"]["structuredContent"]["tasks"])


def test_timed_out_calls_leave_no_tasks_behind() -> None:
    with _mcp_server(_slow_server(1.5), timeout=1.0) as base:
        baseline = _tasks(base)
        started = time.monotonic()
        statuses = [_tool_call(base, "brain_show", {"id_prefix": "abcdef"}).status_code
                    for _ in range(3)]
        elapsed = time.monotonic() - started
        time.sleep(1.0)
        after = _tasks(base)

    assert statuses == [504, 504, 504]
    assert elapsed < 3 * 1.4  # each 504 arrives at the deadline, not after the tool
    assert after == baseline


def test_a_call_behind_a_timed_out_one_is_503_until_it_returns() -> None:
    results: dict[str, int] = {}
    with _mcp_server(_slow_server(2.0), timeout=1.0, max_calls=1) as base:

        def slow() -> None:
            results["slow"] = _tool_call(base, "brain_show", {"id_prefix": "abcdef"}).status_code

        worker = threading.Thread(target=slow)
        worker.start()
        time.sleep(1.3)  # past the slow call's deadline, before its tool returns
        results["behind"] = _tool_call(base, "brain_search", {"query": "q"}).status_code
        worker.join(timeout=10)
        time.sleep(1.0)
        results["after"] = _tool_call(base, "brain_search", {"query": "q"}).status_code

    assert results == {"slow": 504, "behind": 503, "after": 200}


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "OPTIONS"])
def test_every_non_post_method_on_the_endpoint_is_405(method: str) -> None:
    with _mcp_server(_slow_server(0.0)) as base:
        response = httpx.request(
            method, f"{base}/mcp", headers={**_HEADERS, "Accept": "text/event-stream"}, timeout=5
        )
        ordinary = _tool_call(base, "brain_search", {"query": "q"})

    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    assert ordinary.status_code == 200


def test_every_refusal_closes_its_connection() -> None:
    with _mcp_server(_slow_server(0.0)) as base:
        refusals = {
            "401": httpx.post(f"{base}/mcp", json={}, timeout=5),
            "400": httpx.get(f"{base}/healthz", headers={"Host": "evil.example.com"}, timeout=5),
            "403": httpx.post(f"{base}/mcp", json={"jsonrpc": "2.0", "id": 1,
                              "method": "tools/list"},
                              headers={**_HEADERS, "Origin": "https://evil.example.com"},
                              timeout=5),
            "404": httpx.get(f"{base}/nope", timeout=5),
            "405": httpx.get(f"{base}/mcp", headers=_HEADERS, timeout=5),
            "413": httpx.post(f"{base}/mcp", content=b"x" * (2 * 1024 * 1024),
                              headers=_HEADERS, timeout=5),
        }
        ordinary = _tool_call(base, "brain_search", {"query": "q"})

    for status, response in refusals.items():
        assert str(response.status_code) == status
        assert response.headers.get("connection") == "close", status
    assert ordinary.headers.get("connection") != "close"


# -------------------------------------------- sec-118c INFO-3: a crash's shape


async def _crashes(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Fails before answering, with a message that must not reach the client."""
    if scope["type"] != "http":
        return
    raise KeyError("synthetic-detail /synthetic/vault/private-note.md")


def test_a_crash_behind_the_response_guard_answers_as_a_bare_crash_does() -> None:
    # Since be064a05 the guard runs the app in a task group, so a crash leaves it
    # as an ExceptionGroup. uvicorn answers that exactly as it answers the bare
    # exception: the same status, the same fixed body, no detail.
    from brain.mcp_http_guard import ResponseGuardMiddleware

    guarded = ResponseGuardMiddleware(_crashes, max_response_bytes=100, timeout_s=1.0)
    responses = {}
    for name, app in (("guarded", guarded), ("bare", _crashes)):
        with _server(app=app) as port:
            responses[name] = httpx.post(f"http://127.0.0.1:{port}/mcp", timeout=10)

    assert responses["guarded"].status_code == responses["bare"].status_code == 500
    assert responses["guarded"].content == responses["bare"].content
    assert b"synthetic" not in responses["guarded"].content
