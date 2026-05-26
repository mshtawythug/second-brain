"""Embedder backends — Arctic, Voyage, Qwen3 — selected at setup time.

Three implementations satisfy the :class:`brain.ingest.Embedder` Protocol:

- :class:`ArcticEmbedder` — Snowflake Arctic Embed v2 over local Ollama (default).
  Native 1024-dim, free, indexable under pgvector's HNSW cap.
- :class:`VoyageEmbedder` — Voyage AI SDK. 1024-dim, paid SaaS.
- :class:`Qwen3Embedder` — Qwen3-Embedding-8B over local Ollama. 4096-dim,
  free, but exceeds pgvector's HNSW cap so search uses sequential scan.

Token counting is via tiktoken (cl100k_base) — offline and good enough for
chunker budgeting. Each backend's :meth:`embed` accepts ``input_type`` to
dispatch query vs document prompt formatting; the formatting is per-backend
because each model is trained with a different convention (see comments).
"""
from typing import Any

import httpx
import tiktoken

from .config import Config, ConfigError
from .errors import BrainError
from .ingest import Embedder

# Shared Ollama transport defaults — both Ollama-backed embedders use these.
_DEFAULT_OLLAMA_BATCH = 32
_DEFAULT_OLLAMA_TIMEOUT_S = 60.0

# Module-level keep_alive fallback — used when an embedder is constructed
# without an explicit ``keep_alive`` kwarg (e.g. in tests or legacy call
# sites). Production always threads the value from ``Config.ollama_keep_alive``
# (set at construction time, not re-read per request).
_DEFAULT_OLLAMA_KEEP_ALIVE = "30m"

# Arctic Embed v2 query prefix per Snowflake's HF model card guidance:
#   https://huggingface.co/Snowflake/snowflake-arctic-embed-l-v2.0
# "Use the query prefix below (just on the query)" → 'query: '. Documents
# get no prefix.
_ARCTIC_QUERY_PREFIX = "query: "
_ARCTIC_DEFAULT_MODEL = "snowflake-arctic-embed2"

# Qwen3-Embedding query mode prepends an Instruct prompt that primes the model
# for retrieval over a domain-specific corpus. Documents skip the prefix.
_QWEN3_QUERY_TASK = (
    "Given a search query, retrieve relevant passages from a personal knowledge "
    "base of career documents, transcripts, and emails"
)
_QWEN3_DEFAULT_MODEL = "qwen3-embedding:8b"

# Voyage SDK model. Per the plan we pin the named-current production model
# (voyage-3.5). voyage-4 also works against the same SDK signature; bump here
# if the user wants the newer generation.
_VOYAGE_MODEL = "voyage-3.5"
_VOYAGE_DEFAULT_BATCH = 128


class OllamaEmbedError(BrainError):
    """Raised when an Ollama-backed embed call fails (network / 4xx / 5xx / shape)."""


class _OllamaEmbedderBase:
    """Shared HTTP transport, batching, and tokenizer for Ollama-hosted models.

    Subclasses declare ``dim`` (native vector size) and override
    :meth:`_format_query` to apply the model-specific query-side prompt.
    Document-side text is sent verbatim by default.
    """

    dim: int  # subclasses set this as a class attribute

    def __init__(
        self,
        *,
        host: str,
        model: str,
        client: httpx.Client | None = None,
        batch_size: int = _DEFAULT_OLLAMA_BATCH,
        timeout: float = _DEFAULT_OLLAMA_TIMEOUT_S,
        keep_alive: str = _DEFAULT_OLLAMA_KEEP_ALIVE,
    ) -> None:
        self._host = host
        self._model = model
        self._batch_size = batch_size
        self._keep_alive = keep_alive
        self._tokenizer = tiktoken.get_encoding("cl100k_base")
        if client is not None:
            self._client = client
        else:
            self._client = httpx.Client(
                base_url=host, timeout=httpx.Timeout(timeout)
            )

    def _format_query(self, text: str) -> str:
        """Subclass hook: apply the model-specific query-side prompt."""
        raise NotImplementedError

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        """Embed ``texts`` in batches of ``batch_size`` and return all vectors in order.

        With ``input_type="query"`` each text is wrapped with the subclass's
        :meth:`_format_query`; ``"document"`` (the default) sends the raw
        text. An empty input returns an empty list with no HTTP I/O.
        Raises :class:`OllamaEmbedError` on any HTTP / decode / shape error.
        """
        if not texts:
            return []
        prepared = (
            [self._format_query(t) for t in texts]
            if input_type == "query"
            else list(texts)
        )
        out: list[list[float]] = []
        for start in range(0, len(prepared), self._batch_size):
            batch = prepared[start : start + self._batch_size]
            out.extend(self._embed_batch(batch))
        return out

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """Send one /api/embed request and return its vectors."""
        try:
            response = self._client.post(
                "/api/embed",
                json={
                    "model": self._model,
                    "input": batch,
                    "keep_alive": self._keep_alive,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as e:
            body = e.response.text if e.response is not None else "<no body>"
            raise OllamaEmbedError(
                f"Ollama returned HTTP {e.response.status_code}: {body}"
            ) from e
        except httpx.HTTPError as e:
            raise OllamaEmbedError(f"Ollama request failed: {e}") from e
        except ValueError as e:
            # json.JSONDecodeError is a ValueError — a 200 OK with non-JSON
            # body would otherwise leak as a raw decode error to callers
            # that contract for OllamaEmbedError.
            raise OllamaEmbedError(f"Ollama returned non-JSON response: {e}") from e
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list):
            raise OllamaEmbedError(
                f"Ollama response missing 'embeddings' list: {payload!r}"
            )
        if len(embeddings) != len(batch):
            raise OllamaEmbedError(
                f"Ollama returned {len(embeddings)} embeddings for {len(batch)} inputs"
            )
        return [list(v) for v in embeddings]

    def count_tokens(self, text: str) -> int:
        """Return the number of tokens in ``text`` per the local tiktoken tokenizer."""
        return len(self._tokenizer.encode(text))


class Qwen3Embedder(_OllamaEmbedderBase):
    """Ollama-hosted Qwen3-Embedding-8B (4096 native dims).

    Query mode prepends an ``Instruct: ... \\nQuery:`` prompt per the model
    card. pgvector's HNSW caps out at 2000 dims for ``vector``, so the chunks
    column for this backend stays index-free; search uses sequential scan
    (acceptable at personal-corpus scale).
    """

    dim: int = 4096

    def __init__(
        self,
        *,
        host: str,
        model: str = _QWEN3_DEFAULT_MODEL,
        client: httpx.Client | None = None,
        batch_size: int = _DEFAULT_OLLAMA_BATCH,
        timeout: float = _DEFAULT_OLLAMA_TIMEOUT_S,
        keep_alive: str = _DEFAULT_OLLAMA_KEEP_ALIVE,
    ) -> None:
        super().__init__(
            host=host,
            model=model,
            client=client,
            batch_size=batch_size,
            timeout=timeout,
            keep_alive=keep_alive,
        )

    def _format_query(self, text: str) -> str:
        return f"Instruct: {_QWEN3_QUERY_TASK}\nQuery:{text}"


class ArcticEmbedder(_OllamaEmbedderBase):
    """Ollama-hosted Snowflake Arctic Embed v2 (1024 native dims).

    Query mode prepends ``"query: "`` per Snowflake's published guidance
    (https://huggingface.co/Snowflake/snowflake-arctic-embed-l-v2.0):
    "use the query prefix below (just on the query)". Documents get no
    prefix. 1024 dims fits under pgvector's HNSW cap, so the chunks column
    for this backend gets a cosine HNSW index at finalize time.
    """

    dim: int = 1024

    def __init__(
        self,
        *,
        host: str,
        model: str = _ARCTIC_DEFAULT_MODEL,
        client: httpx.Client | None = None,
        batch_size: int = _DEFAULT_OLLAMA_BATCH,
        timeout: float = _DEFAULT_OLLAMA_TIMEOUT_S,
        keep_alive: str = _DEFAULT_OLLAMA_KEEP_ALIVE,
    ) -> None:
        super().__init__(
            host=host,
            model=model,
            client=client,
            batch_size=batch_size,
            timeout=timeout,
            keep_alive=keep_alive,
        )

    def _format_query(self, text: str) -> str:
        return f"{_ARCTIC_QUERY_PREFIX}{text}"


class VoyageEmbedder:
    """Wraps the Voyage AI SDK with batching and offline token counting.

    Voyage's SDK natively understands ``input_type="query"|"document"`` so we
    pass it through unchanged; no manual prefix dance.
    """

    dim: int = 1024

    def __init__(
        self,
        *,
        api_key: str,
        client: Any | None = None,
        batch_size: int = _VOYAGE_DEFAULT_BATCH,
        timeout: float = _DEFAULT_OLLAMA_TIMEOUT_S,
    ) -> None:
        self._batch_size = batch_size
        self._tokenizer = tiktoken.get_encoding("cl100k_base")
        if client is not None:
            self._client = client
        else:  # pragma: no cover - exercised only against real Voyage service
            import voyageai

            # The SDK accepts ``timeout=`` on the client constructor; pass it
            # explicitly per CLAUDE.md "every external HTTP/DB client MUST
            # have explicit timeouts".
            self._client = voyageai.Client(  # type: ignore[attr-defined]
                api_key=api_key, timeout=timeout
            )

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        """Embed ``texts`` in batches of ``batch_size`` and return all vectors in order."""
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            response = self._client.embed(
                texts=batch, model=_VOYAGE_MODEL, input_type=input_type
            )
            out.extend(response.embeddings)
        return out

    def count_tokens(self, text: str) -> int:
        """Return the number of tokens in ``text`` per the local tiktoken tokenizer."""
        return len(self._tokenizer.encode(text))


def make_embedder(cfg: Config) -> Embedder:
    """Return the active embedder based on ``BRAIN_EMBEDDER`` config.

    Dispatches on ``cfg.embedder`` ∈ ``{"arctic", "voyage", "qwen3"}``.
    Raises :class:`ConfigError` when the chosen backend's required config is
    missing (e.g. ``VOYAGE_API_KEY`` for the voyage backend) — earlier than
    the first embed call so ``brain init`` and ``brain doctor`` surface the
    misconfiguration cleanly.

    Returns the :class:`brain.ingest.Embedder` Protocol; callers should not
    depend on the concrete subclass.
    """
    if cfg.embedder == "arctic":
        return ArcticEmbedder(host=cfg.ollama_host, keep_alive=cfg.ollama_keep_alive)
    if cfg.embedder == "qwen3":
        return Qwen3Embedder(
            host=cfg.ollama_host,
            model=cfg.qwen3_model,
            keep_alive=cfg.ollama_keep_alive,
        )
    if cfg.embedder == "voyage":
        if cfg.voyage_api_key is None:
            raise ConfigError(
                "BRAIN_EMBEDDER=voyage requires VOYAGE_API_KEY (see .env.example)"
            )
        return VoyageEmbedder(api_key=cfg.voyage_api_key)
    raise ConfigError(
        f"BRAIN_EMBEDDER must be one of: arctic, voyage, qwen3 (got {cfg.embedder!r})"
    )
