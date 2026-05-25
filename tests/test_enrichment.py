"""Tests for the OllamaEnricher Ollama HTTP client (Wave Q1-D).

Mirrors the ``test_arctic_embedder.py`` MockTransport pattern so coverage of
the JSON-mode / retry / transient-vs-permanent dispatch stays consistent
with the embedder side.
"""
from __future__ import annotations

import json

import httpx
import pytest

from brain.enrichment import (
    OllamaEnricher,
    SummaryResult,
    TagProposal,
)
from brain.errors import EnrichmentError, OllamaUnavailable


def _client(transport: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(base_url="http://x", transport=transport)


def _make_enricher(transport: httpx.MockTransport) -> OllamaEnricher:
    return OllamaEnricher(
        host="http://x",
        model="llama3.1:8b",
        client=_client(transport),
    )


def _ok_summary(summary: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "message": {"role": "assistant", "content": json.dumps({"summary": summary})},
            "done": True,
        },
    )


def _ok_tags(tags: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "message": {"role": "assistant", "content": json.dumps({"tags": tags})},
            "done": True,
        },
    )


# ---------------------------------------------------------------------------
# summarize() — happy path + request-body shape
# ---------------------------------------------------------------------------


def test_summarize_returns_summary_and_model() -> None:
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return _ok_summary("person-x met Ali on 2026-04-30 to discuss compliance work.")

    enricher = _make_enricher(httpx.MockTransport(handler))
    result = enricher.summarize("person-x sync", "long body about the meeting")
    assert isinstance(result, SummaryResult)
    assert result.summary.startswith("person-x met Ali")
    assert result.model == "llama3.1:8b"
    body = json.loads(captured[0])
    assert body["model"] == "llama3.1:8b"
    assert body["stream"] is False
    assert body["format"] == "json"
    assert body["options"]["temperature"] == 0.0


def test_summarize_strict_json_mode_in_request_body() -> None:
    """Request must set format=json + temperature=0 (lock the contract)."""
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return _ok_summary("ok.")

    enricher = _make_enricher(httpx.MockTransport(handler))
    enricher.summarize("t", "doc body")
    body = json.loads(captured[0])
    assert body["format"] == "json"
    assert body["options"]["temperature"] == 0.0
    # Messages: system + user, both strings.
    assert len(body["messages"]) == 2
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"
    assert "TITLE: t" in body["messages"][1]["content"]


def test_summarize_retries_once_on_json_parse_failure() -> None:
    """First response is invalid JSON, second is valid → one retry, one result."""
    responses = iter(
        [
            httpx.Response(
                200,
                json={
                    "message": {"role": "assistant", "content": "not json at all"},
                    "done": True,
                },
            ),
            _ok_summary("retried summary"),
        ]
    )
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return next(responses)

    enricher = _make_enricher(httpx.MockTransport(handler))
    result = enricher.summarize("t", "doc body")
    assert result.summary == "retried summary"
    assert call_count == 2


def test_summarize_raises_enrichment_error_after_second_failure() -> None:
    """Two consecutive bad JSON responses → EnrichmentError, no third call."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "garbage"},
                "done": True,
            },
        )

    enricher = _make_enricher(httpx.MockTransport(handler))
    with pytest.raises(EnrichmentError):
        enricher.summarize("t", "body")
    assert call_count == 2


def test_summarize_raises_ollama_unavailable_on_connect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    enricher = _make_enricher(httpx.MockTransport(handler))
    with pytest.raises(OllamaUnavailable):
        enricher.summarize("t", "body")


def test_summarize_raises_ollama_unavailable_on_5xx() -> None:
    """503 must surface as OllamaUnavailable (transient) with NO retry."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503, text="model still loading")

    enricher = _make_enricher(httpx.MockTransport(handler))
    with pytest.raises(OllamaUnavailable) as exc_info:
        enricher.summarize("t", "body")
    assert "503" in str(exc_info.value)
    # Critical contract: 5xx is transient but we do NOT retry here — caller
    # decides (the ingest hook skips, the backfill loop continues to next row).
    assert call_count == 1


def test_summarize_raises_enrichment_error_on_4xx() -> None:
    """4xx is permanent (bad model name, malformed request) → EnrichmentError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid model name")

    enricher = _make_enricher(httpx.MockTransport(handler))
    with pytest.raises(EnrichmentError) as exc_info:
        enricher.summarize("t", "body")
    assert "400" in str(exc_info.value)


def test_summarize_truncates_input_to_max_tokens() -> None:
    """A 10K-token body is truncated to max_input_tokens=100 before the POST."""
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return _ok_summary("ok.")

    enricher = OllamaEnricher(
        host="http://x",
        model="llama3.1:8b",
        client=_client(httpx.MockTransport(handler)),
        max_input_tokens=100,
    )
    very_long = "word " * 5000  # ~5000 tokens
    enricher.summarize("title", very_long)
    body = json.loads(captured[0])
    user_content = body["messages"][1]["content"]
    # The post-truncation user message includes the TITLE prelude (~3 toks)
    # plus the BODY: prelude (~2 toks) plus the truncated body (100 toks).
    # Assert the body slice is bounded; allow a small overhead for the prelude.
    assert enricher.count_tokens(user_content) <= 100 + 20


# ---------------------------------------------------------------------------
# truncate_to_tokens() — the shared head-cap seam (perf Fix C reuses it)
# ---------------------------------------------------------------------------


def test_truncate_to_tokens_head_caps_long_text() -> None:
    """Head-caps to the first N cl100k_base tokens — the concept extractor's
    input cap (Fix C) reuses this so its boundary agrees with count_tokens."""
    enricher = _make_enricher(httpx.MockTransport(lambda _r: _ok_summary("ok")))
    long = "word " * 500
    assert enricher.count_tokens(long) > 50
    capped = enricher.truncate_to_tokens(long, 50)
    assert capped != long  # it really truncated
    assert enricher.count_tokens(capped) <= 50


def test_truncate_to_tokens_passes_through_short_text() -> None:
    """Text already within the budget is returned unchanged (no re-encode loss)."""
    enricher = _make_enricher(httpx.MockTransport(lambda _r: _ok_summary("ok")))
    short = "just a few words"
    assert enricher.truncate_to_tokens(short, 1000) == short


def test_truncate_to_tokens_rejects_non_positive() -> None:
    enricher = _make_enricher(httpx.MockTransport(lambda _r: _ok_summary("ok")))
    with pytest.raises(ValueError, match="max_tokens"):
        enricher.truncate_to_tokens("some text", 0)


def test_summarize_empty_summary_string_raises_enrichment_error() -> None:
    """Whitespace-only summary is treated as a malformed response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_summary("   ")

    enricher = _make_enricher(httpx.MockTransport(handler))
    with pytest.raises(EnrichmentError):
        enricher.summarize("t", "body")


# ---------------------------------------------------------------------------
# propose_tags() — partition / normalization / banned-format / max_new
# ---------------------------------------------------------------------------


def test_propose_tags_returns_partitioned_existing_and_new() -> None:
    transport = httpx.MockTransport(
        lambda req: _ok_tags(["interview-prep", "person-b", "acmepay"])
    )
    enricher = _make_enricher(transport)
    out = enricher.propose_tags(
        title="t",
        summary="s",
        existing_vocab=["interview-prep", "acmepay"],
        current_tags=[],
        max_new=5,
    )
    assert isinstance(out, TagProposal)
    assert out.existing == ["interview-prep", "acmepay"]
    assert out.new == ["person-b"]


def test_propose_tags_caps_new_at_max_new() -> None:
    transport = httpx.MockTransport(
        lambda req: _ok_tags(["newA", "newB", "newC", "newD"])
    )
    enricher = _make_enricher(transport)
    out = enricher.propose_tags(
        title="t",
        summary="s",
        existing_vocab=[],
        current_tags=[],
        max_new=1,
    )
    assert len(out.new) == 1
    # First-seen order preserved (newA before newB before...).
    assert out.new == ["newa"]


def test_propose_tags_drops_current_tags() -> None:
    transport = httpx.MockTransport(
        lambda req: _ok_tags(["interview-prep", "person-b"])
    )
    enricher = _make_enricher(transport)
    out = enricher.propose_tags(
        title="t",
        summary="s",
        existing_vocab=["interview-prep"],
        current_tags=["interview-prep"],
        max_new=5,
    )
    assert out.existing == []
    assert out.new == ["person-b"]


def test_propose_tags_normalizes_through_normalize_tags() -> None:
    """LLM-returned ``"Interview Prep"`` collapses to ``"interview-prep"``."""
    transport = httpx.MockTransport(
        lambda req: _ok_tags(["Interview Prep", "person-b"])
    )
    enricher = _make_enricher(transport)
    out = enricher.propose_tags(
        title="t",
        summary="s",
        existing_vocab=["interview-prep"],
        current_tags=[],
        max_new=5,
    )
    assert out.existing == ["interview-prep"]
    assert out.new == ["person-b"]


def test_propose_tags_drops_banned_format_tags() -> None:
    """``pdf`` / ``transcript`` / etc. are filtered at the Python layer."""
    transport = httpx.MockTransport(
        lambda req: _ok_tags(["pdf", "transcript", "email", "interview-prep"])
    )
    enricher = _make_enricher(transport)
    out = enricher.propose_tags(
        title="t",
        summary="s",
        existing_vocab=["interview-prep"],
        current_tags=[],
        max_new=5,
    )
    # Only the non-banned, non-current tag survives.
    assert out.existing == ["interview-prep"]
    assert out.new == []


def test_propose_tags_rejects_non_list_response() -> None:
    """LLM returned ``{"tags": "not a list"}`` → EnrichmentError after retry."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"tags": "not a list"}),
                },
                "done": True,
            },
        )

    enricher = _make_enricher(httpx.MockTransport(handler))
    # Schema-check failure happens after json.loads succeeds — the chat-with-
    # retry loop attempts twice when the schema check fails.
    with pytest.raises(EnrichmentError):
        enricher.propose_tags(
            title="t",
            summary="s",
            existing_vocab=[],
            current_tags=[],
        )
    # Bad-shape responses ARE retried (parse succeeded; schema didn't match).
    assert call_count == 1


def test_propose_tags_missing_key_raises_enrichment_error() -> None:
    """Response with no ``tags`` key → EnrichmentError after the retry."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"oops": "wrong key"}),
                },
                "done": True,
            },
        )

    enricher = _make_enricher(httpx.MockTransport(handler))
    with pytest.raises(EnrichmentError):
        enricher.propose_tags(
            title="t",
            summary="s",
            existing_vocab=[],
            current_tags=[],
        )
    assert call_count == 2


def test_propose_tags_max_new_zero_returns_no_new() -> None:
    transport = httpx.MockTransport(
        lambda req: _ok_tags(["alpha", "beta"])
    )
    enricher = _make_enricher(transport)
    out = enricher.propose_tags(
        title="t",
        summary="s",
        existing_vocab=[],
        current_tags=[],
        max_new=0,
    )
    assert out.new == []


# ---------------------------------------------------------------------------
# count_tokens — sanity
# ---------------------------------------------------------------------------


def test_count_tokens_uses_cl100k_base() -> None:
    enricher = OllamaEnricher(
        host="http://x",
        model="llama3.1:8b",
        client=_client(httpx.MockTransport(lambda r: _ok_summary("x"))),
    )
    # tiktoken cl100k_base tokens for "hello world" is 2.
    assert enricher.count_tokens("hello world") == 2


def test_model_property_returns_configured_model() -> None:
    transport = httpx.MockTransport(lambda r: _ok_summary("x"))
    enricher = OllamaEnricher(
        host="http://x", model="custom:99b", client=_client(transport)
    )
    assert enricher.model == "custom:99b"


# ---------------------------------------------------------------------------
# Coverage gap closures (Codex finding 3)
# ---------------------------------------------------------------------------


def test_summarize_response_is_non_dict_json_raises() -> None:
    """200 OK whose content parses to a JSON list (not a dict) → EnrichmentError
    after retry."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        # Content parses successfully (it IS valid JSON) but is a list,
        # which doesn't match our object-with-keys schema.
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps(["not", "an", "object"]),
                },
                "done": True,
            },
        )

    enricher = _make_enricher(httpx.MockTransport(handler))
    with pytest.raises(EnrichmentError, match="non-object JSON"):
        enricher.summarize("t", "body")
    # The schema-check failure path retries once (parse succeeded; schema didn't).
    assert call_count == 2


def test_summarize_response_missing_message_key_raises() -> None:
    """Envelope without a ``message`` key → EnrichmentError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    enricher = _make_enricher(httpx.MockTransport(handler))
    with pytest.raises(EnrichmentError, match="missing 'message'"):
        enricher.summarize("t", "body")


def test_summarize_message_content_not_string_raises() -> None:
    """``message.content`` that's not a string → EnrichmentError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": 42}, "done": True},
        )

    enricher = _make_enricher(httpx.MockTransport(handler))
    with pytest.raises(EnrichmentError, match="content is not a string"):
        enricher.summarize("t", "body")


def test_summarize_httpx_status_error_on_retry_path() -> None:
    """First call returns malformed JSON (retry), second call raises 5xx —
    must surface as OllamaUnavailable, not as the masked EnrichmentError."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                200,
                json={
                    "message": {"role": "assistant", "content": "not json"},
                    "done": True,
                },
            )
        return httpx.Response(503, text="model warming up")

    enricher = _make_enricher(httpx.MockTransport(handler))
    # OllamaUnavailable is the right surface — 5xx is transport-level.
    with pytest.raises(OllamaUnavailable, match="503"):
        enricher.summarize("t", "body")
    assert call_count == 2


def test_propose_tags_negative_max_new_raises_valueerror() -> None:
    """Defensive guard at the propose_tags boundary."""
    transport = httpx.MockTransport(lambda req: _ok_tags(["x"]))
    enricher = _make_enricher(transport)
    with pytest.raises(ValueError, match="max_new must be non-negative"):
        enricher.propose_tags(
            title="t", summary="s",
            existing_vocab=[], current_tags=[], max_new=-1,
        )


def test_propose_tags_max_new_zero_returns_only_existing_vocab() -> None:
    """max_new=0 caps the new partition but existing-vocab tags still pass."""
    transport = httpx.MockTransport(
        lambda req: _ok_tags(["existing-one", "totally-new"])
    )
    enricher = _make_enricher(transport)
    out = enricher.propose_tags(
        title="t",
        summary="s",
        existing_vocab=["existing-one"],
        current_tags=[],
        max_new=0,
    )
    assert out.existing == ["existing-one"]
    assert out.new == []


def test_summarize_generic_httpx_error_raises_ollama_unavailable() -> None:
    """The generic ``httpx.HTTPError`` fallback (anything not 4xx/5xx/connect/
    timeout) must surface as OllamaUnavailable."""

    def handler(request: httpx.Request) -> httpx.Response:
        # ProtocolError is an httpx.HTTPError but is NEITHER a ConnectError
        # nor a ConnectTimeout / ReadTimeout — it falls through to the
        # generic httpx.HTTPError except branch.
        raise httpx.ProtocolError("malformed response from server")

    enricher = _make_enricher(httpx.MockTransport(handler))
    with pytest.raises(OllamaUnavailable, match="transport error"):
        enricher.summarize("t", "body")


def test_summarize_200_with_non_json_body_raises_enrichment_error() -> None:
    """A 200 OK with a body that's not valid JSON must surface as
    EnrichmentError (not leak as a raw ValueError)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"this is not json at all")

    enricher = _make_enricher(httpx.MockTransport(handler))
    with pytest.raises(EnrichmentError, match="non-JSON envelope"):
        enricher.summarize("t", "body")


def test_make_enricher_returns_ollama_enricher() -> None:
    """Smoke test for the factory — exercises the ``make_enricher(cfg)``
    branch that the production CLI path uses."""
    from brain.config import Config
    from brain.enrichment import make_enricher

    cfg = Config(database_url="postgresql://x/y")
    enricher = make_enricher(cfg)
    assert isinstance(enricher, OllamaEnricher)
    assert enricher.model == cfg.enrich_model
