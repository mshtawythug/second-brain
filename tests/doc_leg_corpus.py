"""Real-DB fixtures for the document-leg (RM-20) tests: embedders and a seeded corpus.

:class:`~tests.saved_search_fakes.WordEmbedder` gives each word its own
dimension, so a query and a document sharing words have a real, predictable
cosine. The corpus makes one document (:data:`FIT_TITLE`) whose SUMMARY fits
:data:`QUERY` while its chunk matches one word of it, plus :data:`OTHERS`
documents whose chunks match both words (so plain hybrid search ranks the fit
document last). Every chunk clears the default 0.25 vector floor (the fit one
at about 0.35), so it is a candidate on every surface. The fit document's basis
cosine (about 0.71) tops the others' (about 0.61). All text is synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.db import ensure_relational_embedding_columns
from tests.saved_search_fakes import WordEmbedder, ingest

QUERY = "harbour lighthouse"
FIT_TITLE = "Quarterly notes"
FIT_SUMMARY = "harbour lighthouse"
#: One query word among gardening words: a vector match only, no text match.
FIT_BODY = "lighthouse compost soil mulch"
OTHERS = 4
#: A document whose chunk IS the query: a text hit and the top vector match, so plain
#: hybrid search ranks it above the fit document (never last). Seeded only on request.
LEAD_TITLE = "Beacon register"
LEAD_BODY = "harbour lighthouse"


class HostedWordEmbedder(WordEmbedder):
    """A :class:`WordEmbedder` that declares hosted egress and records what it handled.

    ``sent`` is every text passed to ``embed``; ``measured`` is every text passed
    to ``count_tokens``, which the backfill calls on each document it READ while
    building its basis, before anything is sent.
    """

    hosted_egress = True

    def __init__(self, dim: int = 4096) -> None:
        super().__init__(dim)
        self.sent: list[str] = []
        self.measured: list[str] = []

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        self.sent.extend(texts)
        return super().embed(texts, input_type)

    def count_tokens(self, text: str) -> int:
        self.measured.append(text)
        return super().count_tokens(text)


class ModelWordEmbedder(WordEmbedder):
    """A :class:`WordEmbedder` naming a model, so two instances differ by model alone."""

    def __init__(self, model: str, dim: int = 4096) -> None:
        super().__init__(dim)
        self._model = model


def reconcile(conn: psycopg.Connection[Any], embedder: Any) -> None:
    """Size ``document_embeddings.embedding`` for ``embedder`` (the test schema starts at 1024)."""
    ensure_relational_embedding_columns(conn, embedder)


def seed(
    conn: psycopg.Connection[Any],
    embedder: Any,
    *,
    fit_source: str = "manual",
    fit_sensitivity: str | None = None,
    lead_sensitivity: str | None = None,
) -> dict[str, str]:
    """Ingest the corpus; return ``{"fit": id, "other-0": id, ...}``.

    ``lead_sensitivity`` also seeds the :data:`LEAD_TITLE` document (as ``"lead"``)
    at that sensitivity.
    """
    ids = {
        "fit": ingest(
            conn,
            embedder,
            title=FIT_TITLE,
            content=FIT_BODY,
            source_kind=fit_source,
            sensitivity=fit_sensitivity,
        )
    }
    conn.execute("UPDATE documents SET summary = %s WHERE id = %s", (FIT_SUMMARY, ids["fit"]))
    for i in range(OTHERS):
        ids[f"other-{i}"] = ingest(
            conn,
            embedder,
            title=f"Harbour log {i}",
            content=f"harbour lighthouse rota shift {i}",
        )
    if lead_sensitivity is not None:
        ids["lead"] = ingest(
            conn, embedder, title=LEAD_TITLE, content=LEAD_BODY, sensitivity=lead_sensitivity
        )
    return ids
