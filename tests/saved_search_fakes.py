"""Shared fakes for the saved-search (RM-15) tests: a bag-of-words embedder and a seeder.

The conftest ``FakeEmbedder`` hashes text, so a query and a related document get unrelated
vectors; a semantic watch needs cosines that mean something. :class:`WordEmbedder` gives each
word its own dimension, so a query and a document sharing words have a real, predictable cosine.
All text here is synthetic.
"""
from __future__ import annotations

import hashlib
import math
import uuid
from typing import Any

import psycopg

from brain.embeddings import OllamaEmbedError
from brain.ingest import ExtractedDoc, ingest_document, update_document

#: The test schema's embedding width (conftest ``FakeEmbedder``'s default).
TEST_DIM = 4096


class WordEmbedder:
    """Bag-of-words vectors: one hashed dimension per lowercased word, L2-normalised."""

    def __init__(self, dim: int = TEST_DIM) -> None:
        self.dim = dim

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        return [self._vec(text) for text in texts]

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in text.lower().split():
            slot = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % self.dim
            vec[slot] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class DownEmbedder(WordEmbedder):
    """Ingests like :class:`WordEmbedder` but fails every QUERY embed, as a stopped Ollama does."""

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        if input_type == "query":
            raise OllamaEmbedError("synthetic outage", transient=True)
        return super().embed(texts, input_type)


def ingest(
    conn: psycopg.Connection[Any],
    embedder: Any,
    *,
    title: str,
    content: str,
    source_kind: str = "manual",
    sensitivity: str | None = None,
) -> str:
    """Ingest one synthetic document and return its id."""
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind=source_kind,
        source_external_id=str(uuid.uuid4()),
        tags=[],
    )
    assert result.document_id is not None
    if sensitivity is not None:
        conn.execute(
            "UPDATE documents SET sensitivity = %s WHERE id = %s",
            (sensitivity, result.document_id),
        )
    return result.document_id


def edit(conn: psycopg.Connection[Any], embedder: Any, document_id: str, content: str) -> None:
    """Edit a document's body in place, as ``brain edit`` does (bumps ``updated_at``)."""
    update_document(
        conn, document_id=document_id, embedder=embedder, new_content=content, enrich=False
    )


def age_document(conn: psycopg.Connection[Any], document_id: str, *, hours: int) -> None:
    """Move a document's ``updated_at`` (and ``ingested_at``) ``hours`` into the past."""
    conn.execute(
        "UPDATE documents SET updated_at = NOW() - make_interval(hours => %s), "
        "ingested_at = NOW() - make_interval(hours => %s) WHERE id = %s",
        (hours, hours, document_id),
    )


def age_watch(conn: psycopg.Connection[Any], name: str, *, hours: int) -> None:
    """Move a watch's ``last_checked_at`` ``hours`` into the past."""
    conn.execute(
        "UPDATE saved_searches SET last_checked_at = NOW() - make_interval(hours => %s) "
        "WHERE name = %s",
        (hours, name),
    )
