"""`ProgressEmbedder` — the ingest job's embedder: progress, cancel points, commit gate.

Ingest spec §5.3. It wraps the job's real embedder and conforms to
:class:`brain.ingest.Embedder`, so ``ingest_document`` needs no change: the
embedder seam alone gives progress and cooperative cancel.

**Why cancel here is safe (C3).** ``ingest_document`` embeds every chunk BEFORE
its write transaction opens, so a :class:`~brain.ui.jobs.JobCancelled` raised from
inside :meth:`ProgressEmbedder.embed` aborts before anything is written. Should a
future change move the embed inside the transaction, a mid-embed cancel would
roll back instead, which is still safe but no longer what this says.

**Why the window closes here.** :meth:`~brain.ui.jobs.JobHandle.enter_commit` is
called after the LAST batch returns, and only here. A runner that called it before
``ingest_document`` would close the cancel window before the embed and make the
only long cancellable phase uncancellable.
"""
from __future__ import annotations

from ..ingest import Embedder
from .ingest_limits import EMBED_BATCH_SIZE
from .jobs import JobHandle


class ProgressEmbedder:
    """Embed in slices, reporting progress and honouring cancel between slices."""

    def __init__(
        self, inner: Embedder, handle: JobHandle, *, batch_size: int = EMBED_BATCH_SIZE
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._inner = inner
        self._handle = handle
        self._batch_size = batch_size
        self.dim = inner.dim
        # C5, REQUIRED. Both capability flags are duck-typed and live outside the
        # Embedder Protocol. Each is read with the SAME default its one reader
        # uses: `embeddings.is_hosted_embedder` defaults `hosted_egress` to False,
        # and `ingest._embed_chunks` defaults `produces_embeddings` to True.
        # Forwarding only dim/embed/count_tokens would silently disable the F6
        # egress veto: a confidential paste under Voyage would be POSTed
        # off-machine. Pinned by tests/test_ui_ingest_progress.py and
        # tests/test_ui_ingest_service.py, both mutation-proven.
        self.hosted_egress: bool = getattr(inner, "hosted_egress", False)
        self.produces_embeddings: bool = getattr(inner, "produces_embeddings", True)

    def count_tokens(self, text: str) -> int:
        return self._inner.count_tokens(text)

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        """Embed ``texts`` in slices; then the atomic check-and-close.

        Slicing changes no vector: every backend embeds each text independently.
        An empty ``texts`` still closes the window, because this is reached only
        immediately before a write. After the gate it reports ``finishing``.
        """
        total = len(texts)
        self._handle.progress("embedding", done=0, total=total)
        vectors: list[list[float]] = []
        for start in range(0, total, self._batch_size):
            self._handle.check_cancelled()
            chunk = texts[start : start + self._batch_size]
            vectors.extend(self._inner.embed(chunk, input_type=input_type))
            self._handle.progress("embedding", done=len(vectors), total=total)
        self._handle.enter_commit()
        # Past the gate: the transaction, the post-commit summary and the mirror
        # follow, none of them interruptible (an indeterminate bar in the UI).
        self._handle.progress("finishing")
        return vectors
