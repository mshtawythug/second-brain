"""Embedder backends — Arctic, Voyage, Qwen3 — selected at setup time.

Three implementations satisfy the :class:`brain.ingest.Embedder` Protocol:

- :class:`ArcticEmbedder` — Snowflake Arctic Embed v2 over local Ollama (default).
  Native 1024-dim, free, indexable under pgvector's HNSW cap.
- :class:`VoyageEmbedder` — Voyage AI SDK. 1024-dim, paid SaaS.
- :class:`Qwen3Embedder` — Qwen3-Embedding-8B over local Ollama. 4096-dim,
  free, but exceeds pgvector's HNSW cap so search uses sequential scan.
- :class:`NullEmbedder` — FTS-only backend (``BRAIN_EMBEDDER=none``). Produces
  no vectors; for users with no Ollama. Ingest + lexical search + doctor work;
  the vector leg of hybrid search is skipped.

Token counting is via tiktoken (cl100k_base) — offline and good enough for
chunker budgeting. Each backend's :meth:`embed` accepts ``input_type`` to
dispatch query vs document prompt formatting; the formatting is per-backend
because each model is trained with a different convention (see comments).
"""
from typing import Any, NoReturn

import httpx
import tiktoken

from .config import Config, ConfigError, keep_alive_wire_value
from .errors import EmbedError
from .ingest import Embedder

# Shared Ollama transport defaults — both Ollama-backed embedders use these.
_DEFAULT_OLLAMA_BATCH = 32
_DEFAULT_OLLAMA_TIMEOUT_S = 60.0

# Module-level keep_alive fallback — used when an embedder is constructed
# without an explicit ``keep_alive`` kwarg (e.g. in tests or legacy call
# sites). Production always threads the value from ``Config.ollama_keep_alive``
# (set at construction time, not re-read per request).
_DEFAULT_OLLAMA_KEEP_ALIVE = "30m"


# Wire-boundary sentinel coercion now lives in ``brain.config`` (shared with
# the chat path in ``brain.chat``); kept under the historical private name so
# existing call sites and tests stay valid.
_keep_alive_payload = keep_alive_wire_value

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


# The FTS-only backend surfaces this exact message everywhere it must explain
# that semantic search is off. Kept as a module constant so the exception class
# and any future reference stay in lockstep (DRY).
_EMBED_DISABLED_MESSAGE = (
    "semantic search is disabled (BRAIN_EMBEDDER=none) — install Ollama, set "
    "BRAIN_EMBEDDER=arctic, then run 'brain init' and 'brain reembed' to enable it"
)


class EmbedDisabledError(EmbedError):
    """Raised when an embed is attempted under the FTS-only ``none`` backend.

    A sibling of :class:`OllamaEmbedError` / :class:`VoyageEmbedError` — it is an
    :class:`~brain.errors.EmbedError`, so every ``except EmbedError`` handler
    (the MCP server's ``_wrap_embed_error``, ``brain eval``'s per-query
    tolerance) catches it uniformly. Distinct from the transport-failure
    siblings because nothing *failed*: the ``NullEmbedder`` never produces
    vectors by design, so any code path that reaches an actual embed call under
    the ``none`` backend (e.g. ``brain ask`` / ``graphrag --mode fuse``) gets a
    clear "install Ollama to enable it" message rather than a crash. The
    ingest / search / doctor paths never reach it — they degrade earlier via the
    duck-typed ``produces_embeddings`` flag.
    """


class OllamaEmbedError(EmbedError):
    """Raised when an Ollama-backed embed call fails (network / 4xx / 5xx / shape)."""


class VoyageEmbedError(EmbedError):
    """Raised when a Voyage SDK embed call fails (transport / API / rate-limit / shape).

    The Voyage sibling of :class:`OllamaEmbedError`: :meth:`VoyageEmbedder.embed`
    wraps any ``voyageai.error.VoyageError`` (its rate-limit / connection /
    timeout / API subclasses) in this so callers get the same typed embed error
    the Ollama backends raise instead of a leaked SDK exception. The originating
    SDK error is preserved as ``__cause__`` (``raise ... from e``).
    """


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
                    "keep_alive": _keep_alive_payload(self._keep_alive),
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
        """Embed ``texts`` in batches of ``batch_size`` and return all vectors in order.

        An empty input returns an empty list with no SDK round-trip (matching the
        Ollama base). Any Voyage SDK failure — rate limit, transport error,
        timeout, or a malformed/API error, all subclasses of
        ``voyageai.error.VoyageError`` — is wrapped in :class:`VoyageEmbedError`
        (an :class:`~brain.errors.EmbedError`) so callers get the same typed
        embed error the Ollama backends raise. The explicit per-request timeout
        set on the SDK client (see :meth:`__init__`) surfaces as a
        ``voyageai.error.Timeout``, which is wrapped here too.
        """
        if not texts:
            return []
        # ``voyageai`` is an optional dependency imported lazily (only the voyage
        # backend constructs this class), so reference its exception base lazily
        # too — a top-level import would break arctic/qwen3-only installs.
        from voyageai.error import VoyageError

        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            try:
                response = self._client.embed(
                    texts=batch, model=_VOYAGE_MODEL, input_type=input_type
                )
            except VoyageError as e:
                raise VoyageEmbedError(f"Voyage embed request failed: {e}") from e
            out.extend(response.embeddings)
        return out

    def count_tokens(self, text: str) -> int:
        """Return the number of tokens in ``text`` per the local tiktoken tokenizer."""
        return len(self._tokenizer.encode(text))


class NullEmbedder:
    """FTS-only backend: satisfies the Protocol but produces no vectors.

    Selected via ``BRAIN_EMBEDDER=none`` for a user with no Ollama — ingest,
    lexical (FTS) search, and ``brain doctor`` all work; only the vector leg of
    hybrid search is unavailable. Two contract points make the upgrade path
    painless:

    - ``dim == 1024`` matches the arctic / voyage schema, so switching to a real
      1024-dim backend later is a plain ``brain reembed`` backfill — NO
      destructive column rebuild (``db.ensure_embedding_column`` sees the dims
      already agree).
    - ``produces_embeddings = False`` is a duck-typed flag (NOT part of the
      :class:`brain.ingest.Embedder` Protocol — the real backends never declare
      it). Callers check it via ``getattr(embedder, "produces_embeddings",
      True)`` to degrade gracefully: the ingest pipeline stores NULL embeddings
      and :func:`brain.search.hybrid_search` coerces to ``fts_only``.

    :meth:`count_tokens` uses the same offline ``cl100k_base`` tokenizer as
    every other backend so the chunker's token budgeting is unchanged.
    :meth:`embed` never runs under the ingest / search / doctor paths (they
    degrade earlier); if any other path calls it, it raises
    :class:`EmbedDisabledError` with an upgrade hint rather than crashing
    opaquely.
    """

    dim: int = 1024
    produces_embeddings: bool = False

    def __init__(self) -> None:
        self._tokenizer = tiktoken.get_encoding("cl100k_base")

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> NoReturn:
        """Always raise :class:`EmbedDisabledError` — the null backend has no vectors.

        The keyword-only ``input_type`` default mirrors the
        :class:`brain.ingest.Embedder` Protocol so the signature is substitutable
        for the real backends.
        """
        raise EmbedDisabledError(_EMBED_DISABLED_MESSAGE)

    def count_tokens(self, text: str) -> int:
        """Return the number of tokens in ``text`` per the local tiktoken tokenizer."""
        return len(self._tokenizer.encode(text))


def make_embedder(cfg: Config) -> Embedder:
    """Return the active embedder based on ``BRAIN_EMBEDDER`` config.

    Dispatches on ``cfg.embedder`` ∈ ``{"arctic", "voyage", "qwen3", "none"}``.
    ``none`` returns the FTS-only :class:`NullEmbedder` (no Ollama / no API key
    required). Raises :class:`ConfigError` when the chosen backend's required
    config is missing (e.g. ``VOYAGE_API_KEY`` for the voyage backend) — earlier
    than the first embed call so ``brain init`` and ``brain doctor`` surface the
    misconfiguration cleanly.

    Returns the :class:`brain.ingest.Embedder` Protocol; callers should not
    depend on the concrete subclass.
    """
    if cfg.embedder == "none":
        return NullEmbedder()
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
        f"BRAIN_EMBEDDER must be one of: arctic, voyage, qwen3, none "
        f"(got {cfg.embedder!r})"
    )
