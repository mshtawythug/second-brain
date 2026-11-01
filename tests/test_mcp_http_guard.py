"""The MCP HTTP transport's pure-ASGI guards (RM-46).

Each guard wraps a sentinel app here, so these tests need no SDK, no socket
and no database. Every refusal has a "keeps ordinary behaviour" twin next to it.
Hostnames and addresses are from the documentation ranges.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import pytest
from starlette.testclient import TestClient

from brain.mcp_http_guard import (
    MAX_JSON_DEPTH,
    ConfidentialGateMiddleware,
    HealthzRoute,
    ReadyzRoute,
    RequestLogMiddleware,
    ResponseGuardMiddleware,
    json_depth_exceeds,
)

pytestmark = pytest.mark.nodb

ENDPOINT = "/mcp"


class _Echo:
    """Answers 200 with the request body it received, and counts calls."""

    def __init__(self) -> None:
        self.calls = 0
        self.bodies: list[bytes] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.calls += 1
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        self.bodies.append(body)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body or b"{}"})


def _call(arguments: Any, *, method: str = "tools/call", rid: Any = 7) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": {"name": "brain_search", "arguments": arguments},
        }
    ).encode()


def _gate(echo: _Echo, *, cap: int = 1024 * 1024) -> TestClient:
    return TestClient(ConfidentialGateMiddleware(echo, endpoint=ENDPOINT, max_body_bytes=cap))


# ----------------------------------------------------------- the confidential gate


@pytest.mark.parametrize("value", [True, "true", 1, "yes", {}, [], "false", 0])
def test_a_confidential_request_is_refused(value: Any) -> None:
    echo = _Echo()

    body = _call({"query": "q", "include_confidential": value})

    response = _gate(echo).post(ENDPOINT, content=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == 7
    assert payload["error"]["code"] == -32602
    assert "--include-confidential" in payload["error"]["message"]
    assert echo.calls == 0


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "q"},
        {"query": "q", "include_confidential": False},
        {"query": "q", "include_confidential": None},
    ],
)
def test_an_ordinary_call_passes_byte_for_byte(arguments: dict[str, Any]) -> None:
    echo = _Echo()
    body = _call(arguments)

    response = _gate(echo).post(ENDPOINT, content=body)

    assert response.status_code == 200
    assert echo.bodies == [body]


def test_other_methods_and_paths_pass_untouched() -> None:
    echo = _Echo()
    client = _gate(echo)
    listing = _call({"include_confidential": True}, method="tools/list")

    assert client.post(ENDPOINT, content=listing).status_code == 200
    assert client.post("/elsewhere", content=b"not json").status_code == 200
    assert client.get(ENDPOINT).status_code == 200
    assert echo.calls == 3


def test_a_duplicate_key_is_refused() -> None:
    # Python keeps the LAST duplicate; a parser that kept the first would see a
    # different request than the one checked.
    echo = _Echo()
    body = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"brain_search",'
        b'"arguments":{"include_confidential":true,"include_confidential":false}}}'
    )

    response = _gate(echo).post(ENDPOINT, content=body)

    assert response.status_code == 400
    assert echo.calls == 0


@pytest.mark.parametrize(
    "body",
    [
        b"[" + _call({"query": "q"}) + b"]",  # a batch
        b'"tools/call"',
        b"not json",
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"x":NaN}}',
        "﻿".encode() + _call({"query": "q"}),  # a BOM
        _call({"query": "q"}).decode().encode("utf-16"),
        b"\xff\xfe",
    ],
)
def test_a_body_the_gate_cannot_read_strictly_is_refused(body: bytes) -> None:
    echo = _Echo()

    response = _gate(echo).post(ENDPOINT, content=body)

    assert response.status_code == 400
    assert response.json() == {"error": "malformed request"}
    assert echo.calls == 0


def test_deep_nesting_is_refused_by_status_not_by_crash() -> None:
    echo = _Echo()
    head = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
    deep = head + b"[" * 100_000 + b"]" * 100_000 + b"}"

    response = _gate(echo, cap=4 * 1024 * 1024).post(ENDPOINT, content=deep)

    assert response.status_code == 400
    assert echo.calls == 0


def test_depth_counting_ignores_brackets_inside_strings() -> None:
    nested = "[" * (MAX_JSON_DEPTH + 10)
    body = json.dumps({"query": nested, "escaped": '\\"[[[', "q": "\\\\"}).encode()

    assert json_depth_exceeds(body, MAX_JSON_DEPTH) is False
    assert json_depth_exceeds(b"[" * (MAX_JSON_DEPTH + 1), MAX_JSON_DEPTH) is True
    assert json_depth_exceeds(b"[\\]", MAX_JSON_DEPTH) is False
    at_limit = b"[" * MAX_JSON_DEPTH + b"]" * MAX_JSON_DEPTH
    assert json_depth_exceeds(at_limit, MAX_JSON_DEPTH) is False


def test_an_encoded_body_is_refused() -> None:
    echo = _Echo()

    response = _gate(echo).post(
        ENDPOINT, content=_call({"query": "q"}), headers={"Content-Encoding": "gzip"}
    )

    assert response.status_code == 415
    assert echo.calls == 0


def test_identity_encoding_passes() -> None:
    echo = _Echo()

    response = _gate(echo).post(
        ENDPOINT, content=_call({"query": "q"}), headers={"Content-Encoding": "identity"}
    )

    assert response.status_code == 200


def test_an_oversized_streamed_body_is_413_before_the_app() -> None:
    echo = _Echo()
    chunks = [b"x" * 600, b"x" * 600]

    response = _gate(echo, cap=1000).post(ENDPOINT, content=iter(chunks))

    assert response.status_code == 413
    assert response.json() == {"error": "request too large"}
    assert echo.calls == 0


def test_a_body_at_the_cap_passes() -> None:
    echo = _Echo()
    body = _call({"query": "q" * 50})

    response = _gate(echo, cap=len(body)).post(ENDPOINT, content=body)

    assert response.status_code == 200


# ------------------------------------------------- response size and the deadline


class _Answer:
    """Answers with ``body`` in ``chunks`` pieces, after ``delay`` seconds."""

    def __init__(self, body: bytes, *, chunks: int = 1, delay: float = 0.0) -> None:
        self.body, self.chunks, self.delay = body, chunks, delay

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if self.delay:
            await anyio.sleep(self.delay)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        size = max(1, len(self.body) // self.chunks)
        pieces = [self.body[i : i + size] for i in range(0, len(self.body), size)]
        for index, piece in enumerate(pieces):
            await send(
                {"type": "http.response.body", "body": piece, "more_body": index < len(pieces) - 1}
            )


def _guarded(app: Any, *, cap: int = 1000, timeout: float = 5.0) -> TestClient:
    return TestClient(
        ResponseGuardMiddleware(app, endpoint=ENDPOINT, max_response_bytes=cap, timeout_s=timeout)
    )


def test_an_oversized_result_is_replaced_by_an_error() -> None:
    response = _guarded(_Answer(b"x" * 3000, chunks=3)).post(ENDPOINT)

    assert response.status_code == 502
    assert response.json() == {"error": "result too large"}


def test_a_result_within_the_cap_is_delivered_whole() -> None:
    body = json.dumps({"result": "y" * 900}).encode()[:1000]

    response = _guarded(_Answer(body, chunks=4)).post(ENDPOINT)

    assert response.status_code == 200
    assert response.content == body


def test_a_slow_call_times_out_with_504() -> None:
    response = _guarded(_Answer(b"{}", delay=2.0), timeout=0.2).post(ENDPOINT)

    assert response.status_code == 504
    assert response.json() == {"error": "timed out"}


def test_a_prompt_call_is_not_timed_out() -> None:
    response = _guarded(_Answer(b"{}", delay=0.05), timeout=2.0).post(ENDPOINT)

    assert response.status_code == 200


def test_other_paths_are_not_buffered_or_capped() -> None:
    response = _guarded(_Answer(b"x" * 5000)).get("/elsewhere")

    assert response.status_code == 200
    assert len(response.content) == 5000


# ------------------------------------------------------------------ health routes


async def _ready() -> dict[str, Any]:
    return {"status": "ok", "database": "ok"}


def test_healthz_is_static_and_carries_nothing_else() -> None:
    echo = _Echo()
    client = TestClient(HealthzRoute(echo))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"
    assert echo.calls == 0


def test_healthz_refuses_other_methods_and_passes_other_paths() -> None:
    echo = _Echo()
    client = TestClient(HealthzRoute(echo))

    assert client.post("/healthz").status_code == 405
    assert client.get("/mcp").status_code == 200
    assert echo.calls == 1


def test_readyz_answers_from_its_probe() -> None:
    probe: Callable[[], Awaitable[dict[str, Any]]] = _ready
    client = TestClient(ReadyzRoute(_Echo(), probe=probe))

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
    assert client.post("/readyz").status_code == 405


# -------------------------------------------------------------------- request log


def test_the_log_line_names_a_route_label_not_the_raw_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(RequestLogMiddleware(_Echo(), endpoint=ENDPOINT), client=("192.0.2.7", 5))
    with caplog.at_level(logging.INFO, logger="brain.mcp.http"):
        client.post(ENDPOINT, content=b"{}")
        client.get("/%0d%0aINJECTED", headers={"X-Forwarded-For": "203.0.113.9"})
        client.request("BREW", "/healthz")

    lines = [record.getMessage() for record in caplog.records]
    assert lines[0].startswith("192.0.2.7 POST endpoint 200 ")
    assert lines[1].startswith("192.0.2.7 GET other 200 ")
    assert lines[2].startswith("192.0.2.7 OTHER healthz 200 ")
    assert not any("INJECTED" in line or "203.0.113.9" in line for line in lines)


def test_the_log_never_carries_a_body_or_header(caplog: pytest.LogCaptureFixture) -> None:
    secret_body = b'{"query":"synthetic-private-phrase"}'
    client = TestClient(RequestLogMiddleware(_Echo(), endpoint=ENDPOINT))
    with caplog.at_level(logging.DEBUG):
        client.post(ENDPOINT, content=secret_body, headers={"Authorization": "Bearer synthetic"})

    text = " ".join(record.getMessage() for record in caplog.records)
    assert "synthetic-private-phrase" not in text
    assert "Bearer" not in text


def test_a_failing_app_is_still_logged_and_the_error_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def boom(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("synthetic failure")

    client = TestClient(RequestLogMiddleware(boom, endpoint=ENDPOINT), raise_server_exceptions=True)
    with caplog.at_level(logging.INFO, logger="brain.mcp.http"), pytest.raises(RuntimeError):
        client.post(ENDPOINT)

    assert any(" endpoint 500 " in record.getMessage() for record in caplog.records)


def test_lifespan_and_non_http_scopes_pass_every_guard() -> None:
    seen: list[str] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    async def noop(*_: Any) -> Any:
        return {}

    guards = [
        ConfidentialGateMiddleware(inner, endpoint=ENDPOINT, max_body_bytes=10),
        ResponseGuardMiddleware(inner, endpoint=ENDPOINT, max_response_bytes=10, timeout_s=1),
        RequestLogMiddleware(inner, endpoint=ENDPOINT),
        HealthzRoute(inner),
        ReadyzRoute(inner, probe=_ready),
    ]
    for guard in guards:
        anyio.run(guard, {"type": "lifespan"}, noop, noop)

    assert seen == ["lifespan"] * len(guards)


def test_an_app_that_never_answers_gets_a_500() -> None:
    async def silent(scope: Any, receive: Any, send: Any) -> None:
        return None

    response = _guarded(silent).post(ENDPOINT)

    assert response.status_code == 500
    assert response.json() == {"error": "no response"}


def test_a_streamed_body_within_the_cap_is_reassembled() -> None:
    echo = _Echo()
    body = _call({"query": "q" * 200})
    half = len(body) // 2

    response = _gate(echo).post(ENDPOINT, content=iter([body[:half], body[half:]]))

    assert response.status_code == 200
    assert echo.bodies == [body]


def test_the_app_can_keep_receiving_after_the_replay() -> None:
    seen: list[str] = []

    async def listener(scope: Any, receive: Any, send: Any) -> None:
        seen.append((await receive())["type"])
        seen.append((await receive())["type"])

    gate = ConfidentialGateMiddleware(listener, endpoint=ENDPOINT, max_body_bytes=4096)
    messages = iter(
        [
            {"type": "http.request", "body": _call({"query": "q"}), "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def receive() -> Any:
        return next(messages)

    async def send(message: Any) -> None:
        raise AssertionError("the gate must not answer an ordinary call itself")

    scope = {"type": "http", "method": "POST", "path": ENDPOINT, "headers": []}
    anyio.run(gate, scope, receive, send)

    assert seen == ["http.request", "http.disconnect"]


def test_a_client_that_disconnects_mid_body_gets_no_response() -> None:
    echo = _Echo()
    gate = ConfidentialGateMiddleware(echo, endpoint=ENDPOINT, max_body_bytes=4096)
    messages = iter(
        [
            {"type": "http.request", "body": b'{"jsonrpc"', "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    sent: list[Any] = []

    async def receive() -> Any:
        return next(messages)

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": ENDPOINT, "headers": []}
    anyio.run(gate, scope, receive, send)

    assert sent == []
    assert echo.calls == 0
