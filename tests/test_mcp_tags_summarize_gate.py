"""Over HTTP without ``--include-confidential``, brain_tags_summarize keeps its lens (RM-55).

The confidential gate refuses the argument before the tool runs, so the hosted caller gets the
default lens (``"normal"``), which keeps confidential summaries out of the prompt. That half is
pinned against the test DB in ``tests/test_mcp_tools_tags.py``. Pure ASGI here: no SDK, no socket,
no database.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.testclient import TestClient

from brain.mcp_http_guard import ConfidentialGateMiddleware
from brain.mcp_tool_classes import READ_ONLY_TOOLS

pytestmark = pytest.mark.nodb

ENDPOINT = "/mcp"


class _App:
    """Answers 200 with the body it received, and records every body."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
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


def _summarize_call(arguments: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "brain_tags_summarize", "arguments": arguments},
        }
    ).encode()


def _post(app: _App, body: bytes) -> Any:
    return TestClient(ConfidentialGateMiddleware(app, max_body_bytes=1024 * 1024)).post(
        ENDPOINT, content=body
    )


def test_the_tool_is_served_read_only_over_http() -> None:
    assert "brain_tags_summarize" in READ_ONLY_TOOLS


def test_include_confidential_true_is_refused_before_the_tool_runs() -> None:
    app = _App()

    response = _post(app, _summarize_call({"tag": "nebula", "include_confidential": True}))

    assert response.json()["error"]["code"] == -32602
    assert "--include-confidential" in response.json()["error"]["message"]
    assert app.bodies == []


@pytest.mark.parametrize(
    "arguments",
    [{"tag": "nebula"}, {"tag": "nebula", "include_confidential": False, "limit": 5}],
)
def test_a_default_lens_call_reaches_the_tool_unchanged(arguments: dict[str, Any]) -> None:
    app = _App()
    body = _summarize_call(arguments)

    _post(app, body)

    assert app.bodies == [body]
