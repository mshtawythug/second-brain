"""Deterministic, Ollama-free embedder for the `brain demo` sandbox.

:class:`DemoEmbedder` hashes each text into a stable unit-norm vector via a
sha256-seeded PRNG. It satisfies the :class:`brain.ingest.Embedder` Protocol
(``dim`` / ``embed`` / ``count_tokens``) so the demo reuses the real ingest +
search pipeline unchanged — but produces vectors instantly and offline, with no
model download and no network I/O. Token counting uses the same offline
``tiktoken`` ``cl100k_base`` tokenizer every production backend uses for chunk
budgeting.
"""
from __future__ import annotations

import hashlib
import math
import random

import tiktoken

# Default output dimension. 1024 matches the arctic/voyage schema so the demo
# exercises the ≤2000-dim (HNSW-indexable) code path in
# ``queries.finalize_embedding_index``.
DEFAULT_DEMO_DIM = 1024

# Bytes of the sha256 digest folded into the PRNG seed. 8 bytes (64 bits) is
# ample entropy to make distinct texts map to distinct vectors.
_SEED_BYTES = 8


class DemoEmbedder:
    """Deterministic hash-vector embedder (no Ollama, no network).

    ``produces_embeddings`` advertises that this backend yields real vectors
    (as opposed to an FTS-only null backend), so callers that branch on
    embedding availability can treat the demo as vector-capable.
    """

    produces_embeddings = True

    def __init__(self, dim: int = DEFAULT_DEMO_DIM) -> None:
        if dim < 1:
            raise ValueError(f"DemoEmbedder dim must be >= 1 (got {dim})")
        self.dim = dim
        self._tokenizer = tiktoken.get_encoding("cl100k_base")

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        """Return one stable unit-norm vector per input text.

        ``input_type`` is folded into the hash so a text embedded as a query
        differs from the same text embedded as a document — mirroring the
        prompt-side asymmetry of the real backends — while staying fully
        deterministic. An empty input list returns an empty list.
        """
        return [self._vector(text, input_type) for text in texts]

    def count_tokens(self, text: str) -> int:
        """Return the token count per the offline ``cl100k_base`` tokenizer."""
        return len(self._tokenizer.encode(text))

    def _vector(self, text: str, input_type: str) -> list[float]:
        """Build the deterministic unit-norm vector for ``(input_type, text)``."""
        digest = hashlib.sha256(f"{input_type}:{text}".encode()).digest()
        seed = int.from_bytes(digest[:_SEED_BYTES], "big")
        rng = random.Random(seed)
        raw = [rng.gauss(0.0, 1.0) for _ in range(self.dim)]
        norm = math.sqrt(sum(component * component for component in raw))
        if norm == 0.0:
            # Astronomically unlikely (all-zero gaussians); return a valid unit
            # vector along the first axis rather than divide by zero.
            unit = [0.0] * self.dim
            unit[0] = 1.0
            return unit
        return [component / norm for component in raw]
