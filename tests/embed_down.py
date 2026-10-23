"""Test double: an embedder whose QUERY embeds always fail with an ``EmbedError``.

Models the embedder being unreachable (Ollama down, Voyage erroring) for the
FTS-fallback tests. Document embeds delegate to the wrapped embedder, so one
instance can seed the corpus and then fail the search.

The raised message carries :data:`SECRET_DETAIL` so a test can prove the message
never reaches stderr, JSON, or the MCP wire (only the class name may).
All data is synthetic.
"""
from __future__ import annotations

from brain.embeddings import OllamaEmbedError
from brain.ingest import Embedder

#: Text placed in every raised exception's message. It must never be printed.
SECRET_DETAIL = "synthetic-embed-detail-7f3a"


class QueryEmbedDownEmbedder:
    """Delegate document embeds; raise ``OllamaEmbedError`` on every query embed.

    ``transient`` is passed straight to the error, so one class covers both a
    down daemon (``True``) and a configuration problem (``False``).
    ``query_calls`` counts the failed query embeds.
    """

    def __init__(self, inner: Embedder, *, transient: bool) -> None:
        self._inner = inner
        self._transient = transient
        self.dim = inner.dim
        self.query_calls = 0

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        if input_type == "query":
            self.query_calls += 1
            raise OllamaEmbedError(
                f"connection refused: {SECRET_DETAIL}", transient=self._transient
            )
        return self._inner.embed(texts, input_type=input_type)

    def count_tokens(self, text: str) -> int:
        return self._inner.count_tokens(text)


class BrokenEmbedder(QueryEmbedDownEmbedder):
    """A query embed that fails with a NON-embed error (a programming bug).

    The fallback must not swallow it: only ``EmbedError`` falls back.
    """

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        if input_type == "query":
            raise RuntimeError("synthetic programming error")
        return self._inner.embed(texts, input_type=input_type)
