"""Each embedder backend marks its own embed failures transient or permanent.

``brain eval`` degrades a row only on a TRANSIENT failure (a retry may succeed)
and aborts on a permanent one (spec
docs/specs/2026-10-16-eval-degraded-all-embedders-design.md section 2.2). The
backend is the only place that still holds the precise cause, so this file pins
its classification for every failure the transport or SDK can produce.
"""
from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from voyageai import error as voyage_error

from brain.embeddings import (
    EmbedDisabledError,
    NullEmbedder,
    OllamaEmbedError,
    VoyageEmbedError,
)
from brain.errors import EmbedError
from tests.conftest import FakeEmbedder
from tests.embed_failure_doubles import OllamaFailure, arctic_failing_on, voyage_failing_on

_TEXT = "transience probe"


def _ollama_error(failure: OllamaFailure) -> OllamaEmbedError:
    embedder = arctic_failing_on(FakeEmbedder(), {_TEXT: failure})
    with pytest.raises(OllamaEmbedError) as caught:
        embedder.embed([_TEXT], input_type="query")
    return caught.value


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadError,
     httpx.RemoteProtocolError, 408, 429, 500, 502, 503],
    ids=str,
)
def test_ollama_retryable_failures_are_transient(failure: OllamaFailure) -> None:
    """A timeout, a refused or dropped connection, 408/429 and any 5xx: the same
    request can succeed later. 500 is what an out-of-memory model load returns."""
    assert _ollama_error(failure).transient is True


@pytest.mark.parametrize(
    "failure", [400, 401, 404, 422, httpx.UnsupportedProtocol, httpx.ProxyError], ids=str
)
def test_ollama_non_retryable_failures_are_permanent(failure: OllamaFailure) -> None:
    """Model not pulled (404), a bad request, a host URL with no scheme: the same
    request fails the same way every time."""
    assert _ollama_error(failure).transient is False


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"no_embeddings": []}', b'{"embeddings": [[0.1], [0.2]]}'],
    ids=["non-json", "missing-list", "wrong-count"],
)
def test_ollama_unusable_responses_are_permanent(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    from brain.embeddings import ArcticEmbedder

    client = httpx.Client(base_url="http://ollama.invalid", transport=httpx.MockTransport(handler))
    embedder = ArcticEmbedder(host="http://ollama.invalid", client=client)
    with pytest.raises(OllamaEmbedError) as caught:
        embedder.embed([_TEXT], input_type="query")
    assert caught.value.transient is False


_VOYAGE_TRANSIENT: list[type[voyage_error.VoyageError]] = [
    voyage_error.RateLimitError,
    voyage_error.ServiceUnavailableError,
    voyage_error.Timeout,
    voyage_error.APIConnectionError,
    voyage_error.ServerError,
    voyage_error.TryAgain,
]
_VOYAGE_PERMANENT: list[type[voyage_error.VoyageError]] = [
    voyage_error.AuthenticationError,
    voyage_error.InvalidRequestError,
    voyage_error.MalformedRequestError,
    voyage_error.APIError,
]


def _voyage_error(cls: type[voyage_error.VoyageError]) -> VoyageEmbedError:
    make: Callable[[], Exception] = lambda: cls("synthetic voyage failure")  # noqa: E731
    embedder = voyage_failing_on(FakeEmbedder(), {_TEXT: make})
    with pytest.raises(VoyageEmbedError) as caught:
        embedder.embed([_TEXT], input_type="query")
    assert isinstance(caught.value.__cause__, cls)
    return caught.value


@pytest.mark.parametrize("cls", _VOYAGE_TRANSIENT, ids=lambda c: c.__name__)
def test_voyage_retryable_failures_are_transient(cls: type[voyage_error.VoyageError]) -> None:
    """The SDK's own retry set (rate limit, 502/503/504, timeout) plus a
    transport failure, a 500 and ``TryAgain`` — the same HTTP meaning as Ollama's."""
    assert _voyage_error(cls).transient is True


@pytest.mark.parametrize("cls", _VOYAGE_PERMANENT, ids=lambda c: c.__name__)
def test_voyage_non_retryable_failures_are_permanent(cls: type[voyage_error.VoyageError]) -> None:
    """A bad API key, a rejected or malformed request: no retry fixes it."""
    assert _voyage_error(cls).transient is False


def test_the_two_voyage_lists_cover_every_sdk_error_class() -> None:
    """A new SDK class must be classified on purpose, not inherit the default by
    omission. (``VideoProcessingError`` is not reachable from a text embed.)"""
    declared = {
        c for c in vars(voyage_error).values()
        if isinstance(c, type) and issubclass(c, voyage_error.VoyageError)
    }
    classified = set(_VOYAGE_TRANSIENT) | set(_VOYAGE_PERMANENT)
    assert declared - classified == {voyage_error.VoyageError, voyage_error.VideoProcessingError}


def test_an_unclassified_embed_error_is_permanent() -> None:
    """Degrading a row is a tolerance and needs positive evidence: an embed error
    raised by code that did not classify it aborts, as every one did pre-PR #33."""
    assert EmbedError("unclassified").transient is False
    assert OllamaEmbedError("unclassified").transient is False


def test_transient_is_keyword_only_and_kept() -> None:
    err = OllamaEmbedError("synthetic", transient=True)
    assert err.transient is True
    assert str(err) == "synthetic"


def test_the_disabled_backend_is_permanent() -> None:
    with pytest.raises(EmbedDisabledError) as caught:
        NullEmbedder().embed(["anything"], input_type="query")
    assert caught.value.transient is False
