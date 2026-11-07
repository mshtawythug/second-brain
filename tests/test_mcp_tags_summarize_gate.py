"""brain_tags_summarize over HTTP: it keeps its lens, and it stops at the call deadline (RM-55).

Without ``--include-confidential`` the confidential gate refuses the argument before the tool runs,
so the hosted caller gets the default lens (``"normal"``), which keeps confidential summaries out
of the prompt. That half is pinned against the test DB in ``tests/test_mcp_tools_tags.py``.

The HTTP guard answers 504 at ``--call-timeout`` but the call keeps its worker (and its Ollama
slot) until the tool returns (sec-124 LOW-1). So the tool's chat gets only the time left after the
fetch, split across ``chat_json``'s attempts, and none at all once the deadline has passed.

No SDK, no socket, no database, no real clock.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from brain import chat
from brain.ask_deadline import Deadline
from brain.errors import EnrichmentError
from brain.mcp_http_guard import ConfidentialGateMiddleware
from brain.mcp_tool_classes import READ_ONLY_TOOLS
from brain.mcp_tools.tags import MIN_CHAT_ATTEMPT_SECONDS, SummaryDeadlineError, chat_timeout
from tests.stepped_clock import SteppedClock

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


# --- the call deadline (sec-124 LOW-1) --------------------------------------------------------


def test_stdio_has_no_deadline_and_keeps_the_chat_default() -> None:
    assert chat_timeout(60.0, None) is None


def test_the_time_left_after_the_fetch_is_split_across_the_attempts() -> None:
    deadline = Deadline(SteppedClock(0.0, 1.0), 5.0)  # created at 0; the fetch took 1 second
    assert chat_timeout(60.0, deadline) == (5.0 - 1.0) / chat.CHAT_ATTEMPTS == 2.0


def test_a_generous_call_timeout_is_capped_by_the_enrich_timeout() -> None:
    assert chat_timeout(60.0, Deadline(SteppedClock(0.0, 0.5), 1000.0)) == 60.0


@pytest.mark.parametrize("after_fetch", [5.0, 6.0, 1e9])
def test_no_time_left_refuses_before_any_chat_call(after_fetch: float) -> None:
    with pytest.raises(SummaryDeadlineError):
        chat_timeout(60.0, Deadline(SteppedClock(0.0, after_fetch), 5.0))


def test_a_share_below_the_floor_refuses_rather_than_failing_as_ollama_down() -> None:
    """1.9 s left is 0.95 s an attempt: too little to answer, and a timeout there would read as
    "Ollama is not available". It takes the deadline path instead (rev-124 sliver floor)."""
    assert MIN_CHAT_ATTEMPT_SECONDS == 1.0
    with pytest.raises(SummaryDeadlineError):
        chat_timeout(60.0, Deadline(SteppedClock(0.0, 3.1), 5.0))


def test_a_share_exactly_at_the_floor_still_reaches_the_chat() -> None:
    assert chat_timeout(60.0, Deadline(SteppedClock(0.0, 3.0), 5.0)) == MIN_CHAT_ATTEMPT_SECONDS


def test_the_split_matches_how_many_attempts_chat_json_makes() -> None:
    calls: list[httpx.Request] = []

    def malformed(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"message": {"content": "not json"}, "done": True})

    client = httpx.Client(transport=httpx.MockTransport(malformed), base_url="http://x")
    with pytest.raises(EnrichmentError):
        chat.chat_json_with_client(
            client, model="m", messages=[{"role": "user", "content": "q"}],
            required_keys=("summary",), keep_alive="0", num_ctx=8192,
        )
    assert len(calls) == chat.CHAT_ATTEMPTS == 2
