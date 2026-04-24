"""Ingest pipeline: extract → chunk → embed → store."""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import psycopg
from pgvector.psycopg import register_vector  # noqa: F401  (ensures adapter loaded)

from .chunker import chunk_text


class Embedder(Protocol):
    """Narrow interface for embedding clients used by the ingest pipeline."""

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]: ...

    def count_tokens(self, text: str) -> int: ...


@dataclass
class ExtractedDoc:
    """A document produced by an extractor and ready to be ingested."""

    title: str
    content: str
    content_type: str
    source_path: str | None
    metadata: dict[str, Any]


@dataclass
class IngestResult:
    """Outcome of :func:`ingest_document`."""

    document_id: str | None
    created: bool


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _upsert_source(
    conn: psycopg.Connection,
    *,
    kind: str,
    external_id: str | None,
    metadata: dict[str, Any],
) -> str | None:
    """Return an existing source row id, or insert a new one. Returns None for
    purely manual ingests with no external id and no metadata."""
    if external_id is None and not metadata:
        return None
    row = conn.execute(
        "SELECT id FROM sources WHERE kind=%s AND external_id IS NOT DISTINCT FROM %s",
        (kind, external_id),
    ).fetchone()
    if row:
        return str(row[0])
    new = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) VALUES (%s, %s, %s) RETURNING id",
        (kind, external_id, json.dumps(metadata)),
    ).fetchone()
    assert new is not None  # RETURNING id always yields a row
    return str(new[0])


def ingest_document(
    conn: psycopg.Connection,
    *,
    embedder: Embedder,
    doc: ExtractedDoc,
    source_kind: str,
    source_external_id: str | None = None,
    source_metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    force: bool = False,
) -> IngestResult:
    """Ingest a single extracted document.

    Dedup rules:
    - Documents are deduped by ``content_hash`` (SHA-256 of ``doc.content``).
      A repeat ingest is a no-op unless ``force=True``, in which case the prior
      row (and its chunks via ON DELETE CASCADE) is removed and re-inserted.
    - Sources are deduped by ``(kind, external_id)``. A repeat ingest pointing
      at the same external id reuses the existing source row.
    """
    h = _content_hash(doc.content)
    tags = tags or []
    source_metadata = source_metadata or {}

    with conn.transaction():
        existing = conn.execute(
            "SELECT id FROM documents WHERE content_hash=%s", (h,)
        ).fetchone()
        if existing:
            if not force:
                return IngestResult(document_id=str(existing[0]), created=False)
            conn.execute("DELETE FROM documents WHERE id=%s", (existing[0],))

        source_id = _upsert_source(
            conn,
            kind=source_kind,
            external_id=source_external_id,
            metadata=source_metadata,
        )

        chunks = chunk_text(doc.content, count_tokens=embedder.count_tokens)
        if not chunks:
            return IngestResult(document_id=None, created=False)

        embeddings = embedder.embed(
            [c.content for c in chunks], input_type="document"
        )

        doc_row = conn.execute(
            """
            INSERT INTO documents (source_id, title, content, content_hash, content_type,
                                   source_path, tags, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                source_id,
                doc.title,
                doc.content,
                h,
                doc.content_type,
                doc.source_path,
                tags,
                json.dumps(doc.metadata),
            ),
        ).fetchone()
        assert doc_row is not None  # RETURNING id always yields a row
        document_id = str(doc_row[0])

        for c, emb in zip(chunks, embeddings, strict=True):
            conn.execute(
                "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
                "VALUES (%s, %s, %s, %s)",
                (document_id, c.index, c.content, emb),
            )

        return IngestResult(document_id=document_id, created=True)


_EXTRACTORS = {
    ".txt": "text",
    ".md": "markdown",
    ".markdown": "markdown",
    ".pdf": "pdf",
    ".docx": "docx",
}


def extract_path(path: Path) -> ExtractedDoc:
    """Dispatch to the correct extractor based on file extension.

    Raises ``ValueError`` for unsupported extensions or malformed input files;
    ``OSError`` for unreadable files. Backend-specific parser errors (e.g.
    :class:`pypdf.errors.PyPdfError`) are wrapped as ``ValueError`` so callers
    only need to handle a narrow set of exception types.
    """
    ext = Path(path).suffix.lower()
    name = _EXTRACTORS.get(ext)
    if name is None:
        raise ValueError(f"unsupported file type: {ext}")
    try:
        if name == "text":
            from .text import extract_text
            return extract_text(path)
        if name == "markdown":
            from .markdown import extract_markdown
            return extract_markdown(path)
        if name == "pdf":
            from pypdf.errors import PyPdfError

            from .pdf import extract_pdf
            try:
                return extract_pdf(path)
            except PyPdfError as e:
                raise ValueError(f"malformed PDF: {e}") from e
        if name == "docx":
            from docx.opc.exceptions import PackageNotFoundError

            from .docx import extract_docx
            try:
                return extract_docx(path)
            except PackageNotFoundError as e:
                raise ValueError(f"malformed DOCX: {e}") from e
    except UnicodeDecodeError as e:  # pragma: no cover - extractors use errors="replace"
        raise ValueError(f"could not decode file as UTF-8: {e}") from e
    raise AssertionError("unreachable")  # pragma: no cover


def supported_extensions() -> list[str]:
    """Return the list of file extensions that :func:`extract_path` can handle."""
    return list(_EXTRACTORS.keys())
