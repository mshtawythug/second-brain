"""Real embedder backends over fake transports that fail on named query texts.

The eval tests need a backend that fails the way production fails: a real
:class:`~brain.embeddings.ArcticEmbedder` whose HTTP transport times out or
answers an error status, and a real :class:`~brain.embeddings.VoyageEmbedder`
whose SDK client raises a ``voyageai.error`` class. Both classify the failure
themselves, so a test built on them exercises the backend's classification and
``run_eval``'s handling together. Neither double touches the network, and
neither reopens a production module: the transport and the SDK client are the
constructor seams each backend already exposes.

Healthy texts are answered with the conftest ``FakeEmbedder``'s vectors, so a
document seeded with that embedder is still retrievable through these backends.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

import httpx

from brain.embeddings import ArcticEmbedder, VoyageEmbedder
from brain.ingest import Embedder

# An Ollama failure is an exception the transport raises, an HTTP status it
# answers with, or a (status, error text) pair for a body that matters (e.g.
# the context-overflow refusal, which is recognised by its text).
OllamaFailure = type[httpx.TransportError] | int | tuple[int, str]

# Ollama's refusal of an over-window input under ``truncate: false``.
OLLAMA_OVERFLOW = (400, "the input length exceeds the context length")

_ARCTIC_QUERY_PREFIX = "query: "


def arctic_failing_on(inner: Embedder, failures: Mapping[str, OllamaFailure]) -> ArcticEmbedder:
    """A real ``ArcticEmbedder`` whose transport fails for the named query texts.

    ``failures`` maps a query text to an ``httpx`` transport-error class (raised),
    an HTTP status (answered, with a small JSON error body), or a ``(status,
    error text)`` pair such as :data:`OLLAMA_OVERFLOW`. Every other
    request is answered with ``inner``'s vectors.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        texts = [t.removeprefix(_ARCTIC_QUERY_PREFIX) for t in json.loads(request.read())["input"]]
        for text in texts:
            failure = failures.get(text)
            if failure is None:
                continue
            if isinstance(failure, int):
                return httpx.Response(failure, json={"error": "synthetic failure"})
            if isinstance(failure, tuple):
                status, error_text = failure
                return httpx.Response(status, json={"error": error_text})
            raise failure("synthetic transport failure", request=request)
        return httpx.Response(200, json={"embeddings": inner.embed(texts, input_type="query")})

    client = httpx.Client(base_url="http://ollama.invalid", transport=httpx.MockTransport(handler))
    return ArcticEmbedder(host="http://ollama.invalid", client=client)


class _FakeVoyageClient:
    """Stands in for ``voyageai.Client``: raises a named SDK error per text."""

    def __init__(self, inner: Embedder, failures: Mapping[str, Callable[[], Exception]]) -> None:
        self._inner = inner
        self._failures = dict(failures)

    def embed(self, *, texts: list[str], model: str, input_type: str) -> Any:
        for text in texts:
            make_error = self._failures.get(text)
            if make_error is not None:
                raise make_error()
        return SimpleNamespace(embeddings=self._inner.embed(texts, input_type=input_type))


def voyage_failing_on(
    inner: Embedder, failures: Mapping[str, Callable[[], Exception]]
) -> VoyageEmbedder:
    """A real ``VoyageEmbedder`` whose SDK client raises for the named texts.

    ``failures`` maps a text to a zero-argument factory for the SDK exception,
    e.g. ``lambda: voyageai.error.RateLimitError("synthetic")``.
    """
    return VoyageEmbedder(api_key="synthetic-key", client=_FakeVoyageClient(inner, failures))
