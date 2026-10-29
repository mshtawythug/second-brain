"""Reranker backends: ``llama-server`` over loopback HTTP, and Voyage (RM-14a, G7).

Ollama has no rerank endpoint, so the local backend is ``llama-server`` from
llama.cpp, started with ``--embedding --pooling rank --reranking`` and serving
``bge-reranker-v2-m3`` (RM-14b installs it). It needs no new Python
dependency: the request goes through the already-declared ``httpx``. Voyage is
the hosted alternative, through the already-declared ``voyageai``, and is the
only backend with ``hosted_egress`` set, which is what makes
:class:`brain.rerank.RerankStage` withhold confidential passages from it.

Both backends have an explicit timeout on every call, neither retries, and
every failure, including a reply that cannot be mapped back onto the passages,
is a :class:`brain.rerank.RerankError` chained from its cause.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from .rerank import Reranker, RerankError
from .settings.rerank import BACKEND_LLAMACPP, BACKEND_VOYAGE, RerankSettings

#: The documented path; ``/rerank``, ``/reranking`` and ``/v1/reranking`` are aliases.
LLAMACPP_RERANK_PATH = "/v1/rerank"


def _scores_by_index(pairs: Sequence[tuple[Any, Any]], expected: int) -> list[float]:
    """Turn ``(index, score)`` pairs, in any order, into scores in passage order.

    Refuses a reply that does not name every passage exactly once with a
    numeric score: a partial or duplicated reply cannot be mapped back.
    """
    scores: dict[int, float] = {}
    for index, score in pairs:
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < expected
            or index in scores
            or not isinstance(score, int | float)
            or isinstance(score, bool)
        ):
            raise RerankError("reranker reply does not map onto the passages")
        scores[index] = float(score)
    if len(scores) != expected:
        raise RerankError("reranker reply does not score every passage")
    return [scores[i] for i in range(expected)]


@dataclass
class LlamaCppReranker:
    """``POST {url}/v1/rerank`` on a local ``llama-server``.

    ``client`` is a seam for tests (an ``httpx.Client`` over a
    ``MockTransport``); without one, each call opens and closes its own, so a
    long-lived surface (MCP, ``brain ui``) holds no idle connection to a
    service that may not be running.
    """

    url: str
    model: str
    client: httpx.Client | None = None
    name: str = BACKEND_LLAMACPP
    hosted_egress: bool = False

    def score(self, query: str, passages: Sequence[str], *, timeout: float) -> list[float]:
        body = {
            "model": self.model,
            "query": query,
            "documents": list(passages),
            "top_n": len(passages),
        }
        endpoint = f"{self.url}{LLAMACPP_RERANK_PATH}"
        try:
            if self.client is not None:
                response = self.client.post(endpoint, json=body, timeout=httpx.Timeout(timeout))
            else:
                with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
                    response = client.post(endpoint, json=body)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # ``ValueError`` covers a body that is not JSON.
            raise RerankError("llama-server rerank request failed") from exc
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
            raise RerankError("llama-server rerank reply has no results list")
        return _scores_by_index(
            [(r.get("index"), r.get("relevance_score")) for r in results], len(passages)
        )


def _voyage_client(**kwargs: Any) -> Any:
    # Imported lazily, as ``embeddings.VoyageEmbedder`` does: a local-only
    # install never pays for the SDK's import.
    import voyageai

    return voyageai.Client(**kwargs)  # type: ignore[attr-defined]


@dataclass
class VoyageReranker:
    """Voyage's hosted reranker. ``hosted_egress`` is set: confidential rows are withheld.

    ``client`` and ``client_factory`` are seams for tests. Without a client,
    each call builds one with ``max_retries=0`` (the SDK default, made
    explicit) and the call's timeout, so the knob bounds every request.
    """

    api_key: str = field(repr=False)
    model: str
    client: Any = None
    client_factory: Callable[..., Any] = _voyage_client
    name: str = BACKEND_VOYAGE
    hosted_egress: bool = True

    def _client_for(self, timeout: float) -> Any:
        if self.client is not None:
            return self.client
        return self.client_factory(api_key=self.api_key, max_retries=0, timeout=timeout)

    def score(self, query: str, passages: Sequence[str], *, timeout: float) -> list[float]:
        from voyageai.error import VoyageError

        try:
            reply = self._client_for(timeout).rerank(
                query, list(passages), model=self.model, top_k=None
            )
        except VoyageError as exc:
            raise RerankError("Voyage rerank request failed") from exc
        results = getattr(reply, "results", None)
        if not isinstance(results, list):
            raise RerankError("Voyage rerank reply has no results list")
        return _scores_by_index(
            [(getattr(r, "index", None), getattr(r, "relevance_score", None)) for r in results],
            len(passages),
        )


def make_reranker(settings: RerankSettings) -> Reranker:
    """The configured backend. ``settings.backend`` must not be ``none``."""
    if settings.backend == BACKEND_LLAMACPP:
        return LlamaCppReranker(url=settings.url, model=settings.model)
    if settings.backend == BACKEND_VOYAGE and settings.voyage_api_key is not None:
        return VoyageReranker(api_key=settings.voyage_api_key, model=settings.model)
    raise ValueError(f"no reranker backend for {settings.backend!r} (is it 'none'?)")
