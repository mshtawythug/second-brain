"""Voyage embeddings client wrapper.

Uses tiktoken (cl100k_base) for offline token counting — close enough to
Voyage's tokenizer for chunk-budget purposes (we don't need exact billing accuracy).
"""
from typing import Any

import tiktoken

MODEL = "voyage-4"
DIMS = 1024
MAX_BATCH = 128


class VoyageEmbedder:
    """Wraps the Voyage AI client with batching and offline token counting."""

    def __init__(
        self,
        *,
        api_key: str,
        client: Any | None = None,
        batch_size: int = MAX_BATCH,
    ) -> None:
        self._batch_size = batch_size
        self._tokenizer = tiktoken.get_encoding("cl100k_base")
        if client is not None:
            self._client = client
        else:  # pragma: no cover - exercised only against real Voyage service
            import voyageai

            self._client = voyageai.Client(api_key=api_key)  # type: ignore[attr-defined]

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        """Embed ``texts`` in batches of ``batch_size`` and return all vectors in order."""
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            response = self._client.embed(texts=batch, model=MODEL, input_type=input_type)
            out.extend(response.embeddings)
        return out

    def count_tokens(self, text: str) -> int:
        """Return the number of tokens in ``text`` per the local tiktoken tokenizer."""
        return len(self._tokenizer.encode(text))
