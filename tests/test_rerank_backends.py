"""``brain.rerank_backends``: llama.cpp over loopback HTTP, and Voyage (RM-14a).

The llama.cpp tests run against an ``httpx.MockTransport`` that speaks the
request and response shape the llama.cpp server README documents for
``POST /v1/rerank`` (checked 2026-10-29): body ``{query, documents, top_n}``,
reply ``{"results": [{"index", "relevance_score"}, ...]}``, sorted by score.
The Voyage tests pass a fake client to the constructor. Nothing touches a
network. All data is synthetic.
"""
from __future__ import annotations

import json
from collections import namedtuple
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from brain.errors import describe_failure
from brain.rerank import RerankError
from brain.rerank_backends import LlamaCppReranker, VoyageReranker, make_reranker
from brain.settings.rerank import load_rerank_settings

URL = "http://127.0.0.1:8081"
PASSAGES = ["Note A\nfirst", "Note B\nsecond", "Note C\nthird"]
#: Placed in every error body; it must never reach a describe_failure string.
BACKEND_SECRET = "synthetic-backend-detail-0b7a"


class Recorder:
    """An ``httpx.MockTransport`` handler that records requests and replies as told."""

    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if callable(self.reply):
            return self.reply(request)
        return self.reply


def _llamacpp(handler: Any) -> LlamaCppReranker:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return LlamaCppReranker(url=URL, model="bge-reranker-v2-m3", client=client)


def _documented_reply(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    # Sorted by score, as the server returns them: index 2 best, then 0, then 1.
    scores = {2: 0.95, 0: 0.4, 1: -1.2}
    results = [
        {"index": i, "relevance_score": scores[i]}
        for i in sorted(range(len(body["documents"])), key=lambda i: -scores[i])
    ]
    return httpx.Response(200, json={"model": body.get("model"), "results": results})


# --- llama.cpp ---------------------------------------------------------------


def test_llamacpp_posts_the_documented_request() -> None:
    recorder = Recorder(_documented_reply)

    _llamacpp(recorder).score("synthetic query", PASSAGES, timeout=2.0)

    (request,) = recorder.requests
    assert request.method == "POST"
    assert str(request.url) == f"{URL}/v1/rerank"
    assert json.loads(request.content) == {
        "model": "bge-reranker-v2-m3",
        "query": "synthetic query",
        "documents": PASSAGES,
        "top_n": len(PASSAGES),
    }
    assert request.extensions["timeout"] == {
        "connect": 2.0,
        "read": 2.0,
        "write": 2.0,
        "pool": 2.0,
    }


def test_llamacpp_maps_scores_back_by_index() -> None:
    scores = _llamacpp(Recorder(_documented_reply)).score("q", PASSAGES, timeout=1.0)
    assert scores == [0.4, -1.2, 0.95]


def test_llamacpp_is_local() -> None:
    reranker = _llamacpp(Recorder(_documented_reply))
    assert reranker.name == "llamacpp"
    assert reranker.model == "bge-reranker-v2-m3"
    assert reranker.hosted_egress is False


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(500, text=BACKEND_SECRET),
        httpx.Response(200, text=f"not json {BACKEND_SECRET}"),
        httpx.Response(200, json={"error": BACKEND_SECRET}),
        httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 1.0}]}),
        httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 1.0},
                    {"index": 0, "relevance_score": 0.5},
                    {"index": 1, "relevance_score": 0.2},
                ]
            },
        ),
        httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 1.0},
                    {"index": 1, "relevance_score": "high"},
                    {"index": 2, "relevance_score": 0.2},
                ]
            },
        ),
        httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 1.0},
                    {"index": 7, "relevance_score": 0.5},
                    {"index": 2, "relevance_score": 0.2},
                ]
            },
        ),
        httpx.Response(200, json=[1, 2, 3]),
    ],
    ids=[
        "http-500",
        "not-json",
        "no-results",
        "too-few",
        "duplicate-index",
        "string-score",
        "index-out-of-range",
        "not-an-object",
    ],
)
def test_llamacpp_refuses_a_bad_reply(reply: httpx.Response) -> None:
    with pytest.raises(RerankError) as info:
        _llamacpp(Recorder(reply)).score("q", PASSAGES, timeout=1.0)
    assert BACKEND_SECRET not in describe_failure(info.value)


def test_llamacpp_wraps_a_timeout() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(BACKEND_SECRET, request=request)

    with pytest.raises(RerankError) as info:
        _llamacpp(Recorder(timeout)).score("q", PASSAGES, timeout=1.0)
    assert describe_failure(info.value) == "RerankError from ReadTimeout"


def test_llamacpp_wraps_a_refused_connection() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(BACKEND_SECRET, request=request)

    with pytest.raises(RerankError) as info:
        _llamacpp(Recorder(refused)).score("q", PASSAGES, timeout=1.0)
    assert describe_failure(info.value) == "RerankError from ConnectError"


def test_llamacpp_without_a_client_builds_one_per_call_with_the_timeout() -> None:
    # No injected client: the backend opens (and closes) its own, with the
    # call's timeout. Pointed at a closed loopback port, it fails cleanly.
    reranker = LlamaCppReranker(url="http://127.0.0.1:9", model="m")
    with pytest.raises(RerankError):
        reranker.score("q", ["p"], timeout=0.5)


# --- Voyage ------------------------------------------------------------------

_Result = namedtuple("_Result", ["index", "document", "relevance_score"])


class FakeVoyageReply:
    def __init__(self, results: Sequence[_Result]) -> None:
        self.results = list(results)


class FakeVoyageClient:
    """Stands in for ``voyageai.Client``: records ``rerank`` calls."""

    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def rerank(self, query: str, documents: list[str], model: str, **kwargs: Any) -> Any:
        self.calls.append({"query": query, "documents": documents, "model": model, **kwargs})
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


def _voyage_reply() -> FakeVoyageReply:
    return FakeVoyageReply(
        [
            _Result(2, PASSAGES[2], 0.9),
            _Result(0, PASSAGES[0], 0.5),
            _Result(1, PASSAGES[1], 0.1),
        ]
    )


def test_voyage_calls_rerank_and_maps_by_index() -> None:
    client = FakeVoyageClient(_voyage_reply())
    reranker = VoyageReranker(api_key="pa-synthetic", model="rerank-3", client=client)

    scores = reranker.score("synthetic query", PASSAGES, timeout=3.0)

    assert scores == [0.5, 0.1, 0.9]
    assert client.calls == [
        {"query": "synthetic query", "documents": PASSAGES, "model": "rerank-3", "top_k": None}
    ]


def test_voyage_is_hosted() -> None:
    reranker = VoyageReranker(
        api_key="pa-synthetic", model="rerank-3", client=FakeVoyageClient(None)
    )
    assert reranker.name == "voyage"
    assert reranker.hosted_egress is True


def test_voyage_wraps_a_voyage_error() -> None:
    from voyageai.error import Timeout

    client = FakeVoyageClient(Timeout(BACKEND_SECRET))
    reranker = VoyageReranker(api_key="pa-synthetic", model="rerank-3", client=client)

    with pytest.raises(RerankError) as info:
        reranker.score("q", PASSAGES, timeout=1.0)
    assert describe_failure(info.value) == "RerankError from Timeout"


class _NoResults:
    """A Voyage reply with no ``results`` attribute at all."""


@pytest.mark.parametrize("reply", [FakeVoyageReply([]), _NoResults()], ids=["none", "missing"])
def test_voyage_refuses_a_reply_without_a_results_list(reply: Any) -> None:
    if isinstance(reply, FakeVoyageReply):
        reply.results = None  # type: ignore[assignment]
    reranker = VoyageReranker(
        api_key="pa-synthetic", model="rerank-3", client=FakeVoyageClient(reply)
    )
    with pytest.raises(RerankError, match="no results list"):
        reranker.score("q", PASSAGES, timeout=1.0)


def test_voyage_refuses_a_partial_reply() -> None:
    client = FakeVoyageClient(FakeVoyageReply([_Result(0, PASSAGES[0], 0.5)]))
    reranker = VoyageReranker(api_key="pa-synthetic", model="rerank-3", client=client)
    with pytest.raises(RerankError):
        reranker.score("q", PASSAGES, timeout=1.0)


def test_voyage_builds_its_client_without_retries_and_with_the_timeout() -> None:
    built: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> FakeVoyageClient:
        built.append(kwargs)
        return FakeVoyageClient(_voyage_reply())

    reranker = VoyageReranker(api_key="pa-synthetic", model="rerank-3", client_factory=factory)
    reranker.score("q", PASSAGES, timeout=4.0)

    assert built == [{"api_key": "pa-synthetic", "max_retries": 0, "timeout": 4.0}]


def test_the_default_voyage_factory_is_the_real_client() -> None:
    import voyageai

    reranker = VoyageReranker(api_key="pa-synthetic", model="rerank-3")
    client = reranker._client_for(2.0)
    assert isinstance(client, voyageai.Client)


def test_the_voyage_key_is_not_in_the_repr() -> None:
    reranker = VoyageReranker(api_key="pa-synthetic-secret", model="rerank-3")
    assert "pa-synthetic-secret" not in repr(reranker)


# --- make_reranker -------------------------------------------------------------


def test_make_reranker_builds_the_configured_backend() -> None:
    local = make_reranker(load_rerank_settings({"BRAIN_RERANKER": "llamacpp"}))
    assert isinstance(local, LlamaCppReranker)
    assert local.url == URL
    hosted = make_reranker(
        load_rerank_settings({"BRAIN_RERANKER": "voyage", "VOYAGE_API_KEY": "pa-synthetic"})
    )
    assert isinstance(hosted, VoyageReranker)
    assert hosted.model == "rerank-3"


def test_make_reranker_refuses_none() -> None:
    with pytest.raises(ValueError, match="none"):
        make_reranker(load_rerank_settings({}))
