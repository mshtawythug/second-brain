"""Tests for the Qwen3Embedder Ollama HTTP client."""
import json

import httpx
import pytest

from brain.embeddings import Qwen3Embedder, Qwen3EmbedError, _format_query


def _transport_returning(
    payload: dict[str, object], *, captured: list[bytes] | None = None
) -> httpx.MockTransport:
    """Mock transport that returns ``payload`` as JSON for any /api/embed POST."""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request.read())
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(base_url="http://x", transport=transport)


def test_embed_documents_no_prefix() -> None:
    captured: list[bytes] = []
    transport = _transport_returning(
        {"embeddings": [[0.1] * 4096, [0.2] * 4096]}, captured=captured
    )
    emb = Qwen3Embedder(host="http://x", client=_client(transport))
    out = emb.embed(["doc1", "doc2"], input_type="document")
    assert len(out) == 2
    assert len(out[0]) == 4096
    body = json.loads(captured[0])
    assert body["input"] == ["doc1", "doc2"]
    assert body["model"] == "qwen3-embedding:8b"


def test_embed_query_prepends_instruct_prefix() -> None:
    captured: list[bytes] = []
    transport = _transport_returning(
        {"embeddings": [[0.0] * 4096]}, captured=captured
    )
    emb = Qwen3Embedder(host="http://x", client=_client(transport))
    emb.embed(["what did person-a say?"], input_type="query")
    body = json.loads(captured[0])
    assert len(body["input"]) == 1
    formatted = body["input"][0]
    assert formatted.startswith("Instruct: ")
    assert formatted.endswith("Query:what did person-a say?")
    # And the helper round-trips to the same formatted string.
    assert formatted == _format_query("what did person-a say?")


def test_embed_splits_into_batches() -> None:
    requests: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body_bytes = request.read()
        requests.append(body_bytes)
        body = json.loads(body_bytes)
        n = len(body["input"])
        return httpx.Response(200, json={"embeddings": [[0.0] * 4096] * n})

    transport = httpx.MockTransport(handler)
    emb = Qwen3Embedder(
        host="http://x", client=_client(transport), batch_size=3
    )
    out = emb.embed(["a", "b", "c", "d", "e"], input_type="document")
    assert len(out) == 5
    assert len(requests) == 2  # 3 + 2


def test_network_error_raises_qwen3_embed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    emb = Qwen3Embedder(host="http://x", client=_client(transport))
    with pytest.raises(Qwen3EmbedError, match="Ollama request failed"):
        emb.embed(["doc"], input_type="document")


def test_5xx_raises_qwen3_embed_error_with_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model still loading")

    transport = httpx.MockTransport(handler)
    emb = Qwen3Embedder(host="http://x", client=_client(transport))
    with pytest.raises(Qwen3EmbedError) as exc_info:
        emb.embed(["doc"], input_type="document")
    msg = str(exc_info.value)
    assert "503" in msg
    assert "model still loading" in msg


def test_4xx_raises_qwen3_embed_error_with_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid model name")

    transport = httpx.MockTransport(handler)
    emb = Qwen3Embedder(host="http://x", client=_client(transport))
    with pytest.raises(Qwen3EmbedError) as exc_info:
        emb.embed(["doc"], input_type="document")
    assert "400" in str(exc_info.value)


def test_malformed_response_raises() -> None:
    transport = _transport_returning({"oops": "no embeddings key"})
    emb = Qwen3Embedder(host="http://x", client=_client(transport))
    with pytest.raises(Qwen3EmbedError, match="missing 'embeddings'"):
        emb.embed(["doc"], input_type="document")


def test_count_tokens_uses_local_tokenizer() -> None:
    emb = Qwen3Embedder(
        host="http://x", client=_client(_transport_returning({"embeddings": []}))
    )
    n = emb.count_tokens("hello world this is a test")
    assert n > 0
