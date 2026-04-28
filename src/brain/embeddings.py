"""Qwen3-Embedding-8B client wrapper backed by a local Ollama server.

Calls the Ollama HTTP ``/api/embed`` endpoint to produce 4096-dim vectors.
Uses tiktoken (cl100k_base) for offline token counting — close enough to
Qwen3's tokenizer for chunk-budget purposes (we don't need exact accuracy).
"""
import httpx
import tiktoken

from .config import Config
from .errors import BrainError

DEFAULT_MODEL = "qwen3-embedding:8b"
DEFAULT_BATCH = 32
DEFAULT_TIMEOUT_S = 60.0

# Qwen3-Embedding query mode prepends an Instruct prompt that primes the model
# for retrieval over a domain-specific corpus. Documents skip the prefix.
_QUERY_TASK = (
    "Given a search query, retrieve relevant passages from a personal knowledge "
    "base of career documents, transcripts, and emails"
)


def _format_query(text: str) -> str:
    """Return the Instruct-prefixed form of ``text`` for query-side embedding."""
    return f"Instruct: {_QUERY_TASK}\nQuery:{text}"


class Qwen3EmbedError(BrainError):
    """Raised when the Ollama embed endpoint returns an error or is unreachable."""


class Qwen3Embedder:
    """Wraps the Ollama HTTP API to produce Qwen3-Embedding-8B vectors."""

    def __init__(
        self,
        *,
        host: str,
        model: str = DEFAULT_MODEL,
        client: httpx.Client | None = None,
        batch_size: int = DEFAULT_BATCH,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._host = host
        self._model = model
        self._batch_size = batch_size
        self._tokenizer = tiktoken.get_encoding("cl100k_base")
        if client is not None:
            self._client = client
        else:
            self._client = httpx.Client(
                base_url=host, timeout=httpx.Timeout(timeout)
            )

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        """Embed ``texts`` in batches of ``batch_size`` and return all vectors in order.

        With ``input_type="query"`` each text is wrapped with the Qwen3
        Instruct prefix; ``"document"`` (the default) sends the raw text.
        An empty input returns an empty list without making any HTTP calls.
        Raises :class:`Qwen3EmbedError` on any HTTP / decode failure or when
        the response shape is wrong (missing ``embeddings`` key, mismatched
        count).
        """
        if not texts:
            return []
        prepared = (
            [_format_query(t) for t in texts] if input_type == "query" else list(texts)
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
                json={"model": self._model, "input": batch},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as e:
            body = e.response.text if e.response is not None else "<no body>"
            raise Qwen3EmbedError(
                f"Ollama returned HTTP {e.response.status_code}: {body}"
            ) from e
        except httpx.HTTPError as e:
            raise Qwen3EmbedError(f"Ollama request failed: {e}") from e
        except ValueError as e:
            # json.JSONDecodeError is a ValueError — a 200 OK with non-JSON
            # body would otherwise leak as a raw decode error to callers
            # that contract for Qwen3EmbedError.
            raise Qwen3EmbedError(f"Ollama returned non-JSON response: {e}") from e
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list):
            raise Qwen3EmbedError(
                f"Ollama response missing 'embeddings' list: {payload!r}"
            )
        if len(embeddings) != len(batch):
            raise Qwen3EmbedError(
                f"Ollama returned {len(embeddings)} embeddings for {len(batch)} inputs"
            )
        return [list(v) for v in embeddings]

    def count_tokens(self, text: str) -> int:
        """Return the number of tokens in ``text`` per the local tiktoken tokenizer."""
        return len(self._tokenizer.encode(text))


def make_embedder(cfg: Config) -> Qwen3Embedder:
    """Build a :class:`Qwen3Embedder` from project config."""
    return Qwen3Embedder(host=cfg.ollama_host, model=cfg.qwen3_model)
