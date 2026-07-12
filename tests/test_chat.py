"""Tests for the shared JSON-mode Ollama chat helper (``brain.chat``).

Mirrors the ``test_enrichment.py`` MockTransport pattern. The public
``chat_json`` builds its own client from ``cfg.ollama_host`` via the
``_build_client`` seam, which tests swap for a MockTransport-backed client
(a standard test double — no production monkey-patching). ``chat_json_with_client``
is exercised directly with an injected client, the same path the enricher uses.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from brain import chat
from brain.chat import chat_json, chat_json_with_client, coerce_bool
from brain.config import Config
from brain.errors import EnrichmentError, OllamaUnavailable


def _client(transport: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(base_url="http://x", transport=transport)


def _ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "message": {"role": "assistant", "content": json.dumps(payload)},
            "done": True,
        },
    )


def _raw(content: str) -> httpx.Response:
    """A 200 OK whose message.content is a raw (possibly malformed) string."""
    return httpx.Response(
        200,
        json={"message": {"role": "assistant", "content": content}, "done": True},
    )


def _cfg() -> Config:
    return Config(
        database_url="postgresql://u:p@localhost:5/db",
        ollama_host="http://x",
        enrich_model="test-model",
        enrich_timeout_seconds=12.0,
        ollama_keep_alive="30m",
    )


# ---------------------------------------------------------------------------
# chat_json_with_client — core round-trip + retry
# ---------------------------------------------------------------------------


def test_chat_json_with_client_returns_parsed_object() -> None:
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return _ok({"answer": "hello", "extra": 1})

    result = chat_json_with_client(
        _client(httpx.MockTransport(handler)),
        model="m",
        messages=[{"role": "user", "content": "q"}],
        required_keys=("answer",),
        keep_alive="30m",
    )
    assert result == {"answer": "hello", "extra": 1}
    # Request body carries the JSON-mode + temperature=0 contract.
    body = json.loads(captured[0])
    assert body["format"] == "json"
    assert body["model"] == "m"
    assert body["keep_alive"] == "30m"
    assert body["options"]["temperature"] == 0.0
    assert body["messages"] == [{"role": "user", "content": "q"}]


def test_chat_json_with_client_retries_on_truncated_json() -> None:
    # First reply is an unterminated JSON string (truncated mid-value); the
    # second attempt succeeds. The num_predict budget must double on the retry.
    budgets: list[int] = []
    replies = [_raw('{"answer": "trunca'), _ok({"answer": "complete"})]

    def handler(request: httpx.Request) -> httpx.Response:
        budgets.append(json.loads(request.read())["options"]["num_predict"])
        return replies[len(budgets) - 1]

    result = chat_json_with_client(
        _client(httpx.MockTransport(handler)),
        model="m",
        messages=[{"role": "user", "content": "q"}],
        required_keys=("answer",),
        keep_alive="30m",
        num_predict=128,
    )
    assert result == {"answer": "complete"}
    assert budgets == [128, 256]  # doubled on the Unterminated retry


def test_chat_json_with_client_retries_on_mid_object_truncation() -> None:
    """REGRESSION (Wave 3, item 3.7): a mid-object truncation (cut after a comma,
    key, or colon — NOT an unterminated string) must ALSO double the num_predict
    budget on retry.

    The old heuristic only bumped on ``"Unterminated"`` in the decode error, so a
    response cut off mid-object ("Expecting property name …", "Expecting ','
    delimiter") kept the SAME budget on the retry — which, at temperature 0.0,
    deterministically re-truncates, making the retry a no-op. Widening the
    detection to "the decoder ran out of input" (error position at/after EOF)
    makes the retry meaningful.
    """
    budgets: list[int] = []
    # First reply is truncated right after a comma → JSONDecodeError
    # "Expecting property name enclosed in double quotes" at EOF (NOT
    # "Unterminated"). Second reply completes.
    replies = [_raw('{"answer": "hello",'), _ok({"answer": "complete"})]

    def handler(request: httpx.Request) -> httpx.Response:
        budgets.append(json.loads(request.read())["options"]["num_predict"])
        return replies[len(budgets) - 1]

    result = chat_json_with_client(
        _client(httpx.MockTransport(handler)),
        model="m",
        messages=[{"role": "user", "content": "q"}],
        required_keys=("answer",),
        keep_alive="30m",
        num_predict=128,
    )
    assert result == {"answer": "complete"}
    assert budgets == [128, 256]  # doubled on the mid-object truncation retry


def test_chat_json_with_client_raises_after_two_bad_responses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _raw("not json at all")

    with pytest.raises(EnrichmentError):
        chat_json_with_client(
            _client(httpx.MockTransport(handler)),
            model="m",
            messages=[{"role": "user", "content": "q"}],
            required_keys=("answer",),
            keep_alive="30m",
        )


def test_chat_json_with_client_raises_on_missing_required_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"other": "value"})

    with pytest.raises(EnrichmentError):
        chat_json_with_client(
            _client(httpx.MockTransport(handler)),
            model="m",
            messages=[{"role": "user", "content": "q"}],
            required_keys=("answer",),
            keep_alive="30m",
        )


def test_chat_json_with_client_5xx_is_unavailable_no_retry() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="overloaded")

    with pytest.raises(OllamaUnavailable):
        chat_json_with_client(
            _client(httpx.MockTransport(handler)),
            model="m",
            messages=[{"role": "user", "content": "q"}],
            required_keys=("answer",),
            keep_alive="30m",
        )
    assert calls == [1]  # fail fast, no retry on transport error


def test_chat_json_with_client_4xx_is_enrichment_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model not found")

    with pytest.raises(EnrichmentError):
        chat_json_with_client(
            _client(httpx.MockTransport(handler)),
            model="m",
            messages=[{"role": "user", "content": "q"}],
            required_keys=("answer",),
            keep_alive="30m",
        )


def test_chat_json_with_client_raises_on_non_object_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _raw("[1, 2, 3]")  # valid JSON, but a list not an object

    with pytest.raises(EnrichmentError):
        chat_json_with_client(
            _client(httpx.MockTransport(handler)),
            model="m",
            messages=[{"role": "user", "content": "q"}],
            required_keys=("answer",),
            keep_alive="30m",
        )


def test_chat_json_with_client_missing_message_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"done": True})  # no "message" key

    with pytest.raises(EnrichmentError):
        chat_json_with_client(
            _client(httpx.MockTransport(handler)),
            model="m",
            messages=[{"role": "user", "content": "q"}],
            required_keys=("answer",),
            keep_alive="30m",
        )


def test_chat_json_with_client_non_string_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"message": {"role": "assistant", "content": 42}}
        )

    with pytest.raises(EnrichmentError):
        chat_json_with_client(
            _client(httpx.MockTransport(handler)),
            model="m",
            messages=[{"role": "user", "content": "q"}],
            required_keys=("answer",),
            keep_alive="30m",
        )


def test_build_client_returns_configured_httpx_client() -> None:
    client = chat._build_client("http://localhost:11434", 7.5)
    try:
        assert isinstance(client, httpx.Client)
        assert str(client.base_url) == "http://localhost:11434"
    finally:
        client.close()


# ---------------------------------------------------------------------------
# chat_json — public Config-based wrapper
# ---------------------------------------------------------------------------


def test_chat_json_builds_single_user_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))
        return _ok({"suggestions": ["a", "b"]})

    monkeypatch.setattr(
        chat,
        "_build_client",
        lambda host, timeout: _client(httpx.MockTransport(handler)),
    )
    result = chat_json(
        "do the thing",
        schema={"suggestions": "list of next steps"},
        cfg=_cfg(),
    )
    assert result == {"suggestions": ["a", "b"]}
    # Single user message; model + keep_alive defaulted from cfg.
    assert captured[0]["messages"] == [{"role": "user", "content": "do the thing"}]
    assert captured[0]["model"] == "test-model"
    assert captured[0]["keep_alive"] == "30m"


def test_chat_json_honors_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))
        return _ok({"answer": "ok"})

    monkeypatch.setattr(
        chat,
        "_build_client",
        lambda host, timeout: _client(httpx.MockTransport(handler)),
    )
    chat_json(
        "q",
        schema={"answer": "..."},
        cfg=_cfg(),
        model="override-model",
        num_predict=512,
    )
    assert captured[0]["model"] == "override-model"
    assert captured[0]["options"]["num_predict"] == 512


def test_chat_json_propagates_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        chat,
        "_build_client",
        lambda host, timeout: _client(httpx.MockTransport(handler)),
    )
    with pytest.raises(OllamaUnavailable):
        chat_json("q", schema={"answer": "..."}, cfg=_cfg())


# ---------------------------------------------------------------------------
# coerce_bool — stringified/int boolean coercion (Task 2.7 shared helper)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Real booleans pass through.
        (True, True),
        (False, False),
        # Stringified booleans (the failure mode `bool(...)` mishandles).
        ("true", True),
        ("false", False),
        ("True", True),
        ("FALSE", False),
        ("  yes  ", True),
        ("no", False),
        ("1", True),
        ("0", False),
        # Ints.
        (1, True),
        (0, False),
        # Anything unrecognised falls back to the default (False here).
        (None, False),
        ("maybe", False),
        ("", False),
        (2, False),
        (1.0, False),
        ({"x": 1}, False),
    ],
)
def test_coerce_bool_normalises(value: Any, expected: bool) -> None:
    assert coerce_bool(value) is expected


def test_coerce_bool_custom_default_used_only_for_unrecognised() -> None:
    # Unrecognised values honour the caller's default ...
    assert coerce_bool("garbage", default=True) is True
    assert coerce_bool(None, default=True) is True
    # ... but a recognised token always wins over the default.
    assert coerce_bool("false", default=True) is False
    assert coerce_bool("true", default=False) is True
