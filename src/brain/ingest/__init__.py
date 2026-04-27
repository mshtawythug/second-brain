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


@dataclass
class UpdateResult:
    """Outcome of :func:`update_document`.

    ``fields_changed`` lists the document columns that were actually mutated
    (subset of ``{"title", "content", "content_type", "metadata", "tags"}``).
    ``rechunked`` is ``True`` iff the body was replaced and chunks were
    re-embedded.
    """

    document_id: str
    fields_changed: list[str]
    rechunked: bool


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


def update_document(
    conn: psycopg.Connection,
    *,
    document_id: str,
    embedder: Embedder | None = None,
    new_title: str | None = None,
    new_content_type: str | None = None,
    new_content: str | None = None,
    metadata_patch: dict[str, Any] | None = None,
    replace_metadata: bool = False,
    new_tags: list[str] | None = None,
) -> UpdateResult:
    """Update one document in place.

    Body changes (``new_content``) re-chunk + re-embed atomically: the prior
    chunks are deleted and new ones are inserted in the same transaction.
    Metadata defaults to a shallow merge — top-level keys overwrite, nested
    objects are not deep-merged. Set ``replace_metadata=True`` to swap the
    blob entirely.

    Raises :class:`ValueError` if ``new_content`` is empty/whitespace-only or
    if its SHA-256 collides with another document. ``embedder`` is required
    when ``new_content`` is provided. Empty/no-op edits are not an error and
    return an :class:`UpdateResult` with ``fields_changed=[]``.
    """
    with conn.transaction():
        row = conn.execute(
            "SELECT title, content, content_type, metadata, tags "
            "FROM documents WHERE id=%s",
            (document_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"document not found: {document_id}")
        cur_title, cur_content, cur_type, cur_meta, cur_tags = row
        cur_meta = dict(cur_meta or {})
        cur_tags = list(cur_tags or [])

        fields_changed: list[str] = []
        sets: list[str] = []
        params: list[Any] = []

        rechunked = False
        new_hash: str | None = None
        if new_content is not None:
            if embedder is None:
                raise ValueError("embedder is required when new_content is provided")
            stripped = new_content.strip()
            if not stripped:
                raise ValueError("content is empty")
            if new_content != cur_content:
                new_hash = _content_hash(new_content)
                clash = conn.execute(
                    "SELECT id FROM documents WHERE content_hash=%s AND id<>%s",
                    (new_hash, document_id),
                ).fetchone()
                if clash:
                    raise ValueError(
                        f"content collides with existing document {clash[0]}"
                    )
                rechunked = True

        if new_title is not None and new_title != cur_title:
            sets.append("title=%s")
            params.append(new_title)
            fields_changed.append("title")

        if new_content_type is not None and new_content_type != cur_type:
            sets.append("content_type=%s")
            params.append(new_content_type)
            fields_changed.append("content_type")

        if metadata_patch is not None:
            if replace_metadata:
                if metadata_patch != cur_meta:
                    sets.append("metadata=%s::jsonb")
                    params.append(json.dumps(metadata_patch))
                    fields_changed.append("metadata")
            else:
                merged = {**cur_meta, **metadata_patch}
                if merged != cur_meta:
                    sets.append("metadata=%s::jsonb")
                    params.append(json.dumps(merged))
                    fields_changed.append("metadata")

        if new_tags is not None and sorted(new_tags) != sorted(cur_tags):
            sets.append("tags=%s")
            params.append(list(new_tags))
            fields_changed.append("tags")

        if rechunked:
            assert new_hash is not None  # set above when rechunked is True
            assert embedder is not None  # checked above
            sets.append("content=%s")
            params.append(new_content)
            sets.append("content_hash=%s")
            params.append(new_hash)
            fields_changed.append("content")

            conn.execute(
                "DELETE FROM chunks WHERE document_id=%s", (document_id,)
            )
            assert new_content is not None  # gated by the empty-check above
            chunks = chunk_text(new_content, count_tokens=embedder.count_tokens)
            if chunks:
                embeddings = embedder.embed(
                    [c.content for c in chunks], input_type="document"
                )
                for c, emb in zip(chunks, embeddings, strict=True):
                    conn.execute(
                        "INSERT INTO chunks (document_id, chunk_index, content, "
                        "embedding) VALUES (%s, %s, %s, %s)",
                        (document_id, c.index, c.content, emb),
                    )

        if sets:
            params.append(document_id)
            conn.execute(
                f"UPDATE documents SET {', '.join(sets)} WHERE id=%s",
                params,
            )

    return UpdateResult(
        document_id=document_id,
        fields_changed=fields_changed,
        rechunked=rechunked,
    )


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
