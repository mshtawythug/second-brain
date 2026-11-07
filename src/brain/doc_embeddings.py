"""Whole-document vectors for the document-leg stage: what is embedded, and the backfill (RM-20).

Each document gets one vector in ``document_embeddings`` (migration 031), made
from its ``title + "\\n" + summary`` or, when it has no summary, its title plus
the first :data:`DOC_HEAD_TOKENS` tokens of its content (:func:`document_basis`).
:func:`backfill` writes the missing ones (``brain docembed backfill``) and, with
``stale=True``, refreshes those whose text changed or whose ``embedder`` is not
the active backend's identity (:func:`brain.search.embedder_identity`: class,
model and dim). :mod:`brain.search_doc_leg` reads them.

Two boundaries (plan ``docs/plans/2026-11-07-rm-20-document-embeddings.md``):

- **F6 egress.** Under a hosted embedder a confidential document's text is never
  sent: every selection statement carries the confidential exclusion, as
  ``brain reembed`` does, and each batch is re-checked just before the embed call,
  so a document marked confidential after its page was read is dropped. It stays
  vectorless, which the stage treats as neutral.
- **No content in logs or output.** Ids and counts only; a failure is reported
  by its class (:func:`brain.errors.describe_failure`).

An embed failure stops the run (D16): batches already written stay, so a re-run
resumes, and the report says what stopped it.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import psycopg

from .confidential_lookup import confidential_document_ids
from .embeddings import is_hosted_embedder
from .errors import BrainError, EmbedError, describe_failure
from .ingest import Embedder
from .ollama_budget import INGEST_CHUNK_OVERLAP_TOKENS, INGEST_CHUNK_TARGET_TOKENS
from .queries import count_confidential_documents
from .search import embedder_identity
from .sensitivity import not_confidential_sql
from .token_budget import truncate_to_token_budget

logger = logging.getLogger(__name__)

BASIS_TITLE_SUMMARY = "title_summary"
BASIS_TITLE_HEAD = "title_head"

#: How much of the content stands in for a missing summary (spec RM-20).
DOC_HEAD_TOKENS = 512
#: The head is read from this many leading characters only, so a long document
#: is never pulled whole; the basis is defined on that prefix, so it stays
#: deterministic (D13).
DOC_HEAD_CHAR_CAP = 8192
#: The title and summary are read from their leading characters only, too, so a
#: several-megabyte hand-edited value is never pulled or tokenized whole. Both
#: comfortably exceed what survives :data:`DOC_BASIS_MAX_TOKENS` (sec-127 L3).
DOC_TITLE_CHAR_CAP = 2048
DOC_SUMMARY_CHAR_CAP = 8192
#: The whole basis is cut to the longest chunk ingest produces, the size the
#: embed window is validated for (``MIN_OLLAMA_EMBED_NUM_CTX``), so a long title
#: cannot make one document overflow on every run (D13).
DOC_BASIS_MAX_TOKENS = INGEST_CHUNK_TARGET_TOKENS + INGEST_CHUNK_OVERLAP_TOKENS

DEFAULT_BATCH_SIZE = 32

#: Below every ``gen_random_uuid()``: the keyset walk's starting point.
_FIRST_ID = "00000000-0000-0000-0000-000000000000"

_TABLE_PRESENT_SQL = "SELECT to_regclass('public.document_embeddings') IS NOT NULL"

# One keyset page. ``{where}`` is built only from the frozen literals below,
# never from input. Drafts are included on purpose: their chunks are embedded
# too, and the stage only re-scores rows the search predicate already admitted.
# Under a hosted embedder the confidential exclusion is added (F6).
_PAGE_SQL = """
    SELECT d.id::text, left(d.title, %s), left(d.summary, %s),
           CASE WHEN d.summary IS NULL OR btrim(d.summary) = ''
                THEN left(d.content, %s) ELSE '' END,
           e.basis_hash, e.embedder
    FROM documents d
    LEFT JOIN document_embeddings e ON e.document_id = d.id
    WHERE d.id > %s::uuid{where}
    ORDER BY d.id
    LIMIT %s
"""
_MISSING_ONLY = "e.document_id IS NULL"

# ``SELECT ... FROM documents WHERE id``: a document deleted since its page was
# read inserts nothing, instead of failing the run on the foreign key.
_UPSERT_SQL = """
    INSERT INTO document_embeddings (document_id, embedding, basis, basis_hash, embedder)
    SELECT d.id, %s::vector, %s, %s, %s FROM documents d WHERE d.id = %s::uuid
    ON CONFLICT (document_id) DO UPDATE SET
        embedding = EXCLUDED.embedding,
        basis = EXCLUDED.basis,
        basis_hash = EXCLUDED.basis_hash,
        embedder = EXCLUDED.embedder,
        created_at = NOW()
"""

# Documents whose one vector another embedder or model wrote: a missing-only run
# never replaces them, so the CLI points at ``--stale``. Same exclusion.
_FOREIGN_SQL = """
    SELECT count(*) FROM documents d
    JOIN document_embeddings e ON e.document_id = d.id
    WHERE e.embedder <> %s{where}
"""

# Documents with no vector from the active embedder. Same exclusion under a
# hosted embedder, so withheld documents are reported apart, not as "remaining".
_REMAINING_SQL = """
    SELECT count(*) FROM documents d
    WHERE NOT EXISTS (
        SELECT 1 FROM document_embeddings e
        WHERE e.document_id = d.id AND e.embedder = %s AND e.embedding IS NOT NULL
    ){where}
"""


class DocEmbeddingError(BrainError):
    """The backfill cannot run: no vectors from this embedder, or no table yet."""


@dataclass(frozen=True)
class Basis:
    """The text a document's vector is made from, which kind it is, and its SHA-256."""

    kind: str
    text: str
    digest: str


@dataclass(frozen=True)
class BackfillReport:
    """What one run did. ``stopped_by`` is a failure's class name, or ``None``.

    ``foreign`` counts the ``remaining`` documents whose vector another embedder or
    model wrote; only ``stale=True`` replaces those.
    """

    embedded: int
    skipped_confidential: int
    remaining: int
    foreign: int = 0
    stopped_by: str | None = None


def document_basis(
    title: str, summary: str | None, head: str, *, count_tokens: Callable[[str], int]
) -> Basis:
    """``title + "\\n" + summary``, or the title plus the head of the content.

    Only the first :data:`DOC_TITLE_CHAR_CAP`, :data:`DOC_SUMMARY_CHAR_CAP` and
    :data:`DOC_HEAD_CHAR_CAP` characters of the title, summary and ``head`` (the
    content's leading text) are read, as :data:`_PAGE_SQL` reads them, so the
    basis is the same whichever side cut them. ``count_tokens`` is
    ``Embedder.count_tokens``.
    """
    title = title[:DOC_TITLE_CHAR_CAP]
    summary = None if summary is None else summary[:DOC_SUMMARY_CHAR_CAP]
    if summary is not None and summary.strip():
        kind, body = BASIS_TITLE_SUMMARY, summary
    else:
        kind = BASIS_TITLE_HEAD
        body = truncate_to_token_budget(
            head[:DOC_HEAD_CHAR_CAP], cost=count_tokens, budget=DOC_HEAD_TOKENS
        )
    text = truncate_to_token_budget(
        f"{title}\n{body}", cost=count_tokens, budget=DOC_BASIS_MAX_TOKENS
    ).rstrip()
    return Basis(kind=kind, text=text, digest=hashlib.sha256(text.encode("utf-8")).hexdigest())


def refuse_without_vectors(embedder: Embedder) -> None:
    """Raise :class:`DocEmbeddingError` for a backend that produces no vectors."""
    if not getattr(embedder, "produces_embeddings", True):
        raise DocEmbeddingError(
            "BRAIN_EMBEDDER=none produces no vectors; "
            "set a real embedder to backfill document vectors"
        )


def _require_table(conn: psycopg.Connection[Any]) -> None:
    row = conn.execute(_TABLE_PRESENT_SQL).fetchone()
    if row is None or not row[0]:
        raise DocEmbeddingError("document_embeddings is missing; run `brain init` first")


def _where(*conditions: str) -> str:
    return "".join(f" AND {c}" for c in conditions)


def _due(
    page: list[tuple[Any, ...]], embedder: Embedder, identity: str
) -> list[tuple[str, Basis]]:
    """The page's documents whose stored vector is missing, outdated, or another embedder's."""
    due: list[tuple[str, Basis]] = []
    for document_id, title, summary, head, stored_hash, stored_embedder in page:
        basis = document_basis(title, summary, head, count_tokens=embedder.count_tokens)
        if stored_hash != basis.digest or stored_embedder != identity:
            due.append((str(document_id), basis))
    return due


def _pages(
    conn: psycopg.Connection[Any], conditions: tuple[str, ...], batch_size: int
) -> Iterator[list[tuple[Any, ...]]]:
    """Yield keyset pages of ``(id, title, summary, head, basis_hash, embedder)``."""
    query = _PAGE_SQL.format(where=_where(*conditions))
    last_id = _FIRST_ID
    while True:
        rows = conn.execute(
            query,
            (DOC_TITLE_CHAR_CAP, DOC_SUMMARY_CHAR_CAP, DOC_HEAD_CHAR_CAP, last_id, batch_size),
        ).fetchall()
        if not rows:
            return
        last_id = str(rows[-1][0])
        yield rows


def _withhold_confidential(
    conn: psycopg.Connection[Any], items: list[tuple[str, Basis]]
) -> list[tuple[str, Basis]]:
    """Drop the batch's documents marked confidential since their page was read.

    Run just before a hosted embed call: the page read and the call are moments
    apart, and a concurrent ``brain edit`` could mark a document confidential in
    between (sec-127 L1). One round trip.
    """
    withheld = confidential_document_ids(conn, [document_id for document_id, _ in items])
    return [item for item in items if item[0] not in withheld]


def _write(
    conn: psycopg.Connection[Any],
    items: list[tuple[str, Basis]],
    vectors: list[list[float]],
    identity: str,
) -> None:
    with conn.transaction():
        for (document_id, basis), vector in zip(items, vectors, strict=True):
            conn.execute(
                _UPSERT_SQL, (vector, basis.kind, basis.digest, identity, document_id)
            )


def backfill(
    conn: psycopg.Connection[Any],
    embedder: Embedder,
    *,
    stale: bool = False,
    limit: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> BackfillReport:
    """Embed documents with no vector (``stale=True``: or an outdated one).

    ``limit`` caps the documents embedded in this run. Raises
    :class:`DocEmbeddingError` before reading anything when the embedder makes
    no vectors or the table is missing. An :class:`~brain.errors.EmbedError`
    stops the run; the report names its class in ``stopped_by``.
    """
    refuse_without_vectors(embedder)
    _require_table(conn)
    identity = embedder_identity(embedder)
    hosted = is_hosted_embedder(embedder)
    gate = (not_confidential_sql("d"),) if hosted else ()
    conditions = gate if stale else (_MISSING_ONLY, *gate)
    embedded = 0
    stopped_by: str | None = None
    for page in _pages(conn, conditions, batch_size):
        if limit is not None and embedded >= limit:
            break
        items = _due(page, embedder, identity)
        if limit is not None:
            items = items[: limit - embedded]
        if hosted:
            items = _withhold_confidential(conn, items)
        if not items:
            continue
        try:
            vectors = embedder.embed([b.text for _, b in items], input_type="document")
        except EmbedError as exc:
            stopped_by = describe_failure(exc)
            logger.info("docembed stopped after %d document(s): %s", embedded, stopped_by)
            break
        _write(conn, items, vectors, identity)
        embedded += len(items)
        logger.info("docembed: %d document(s) embedded so far", embedded)
    return BackfillReport(
        embedded=embedded,
        skipped_confidential=count_confidential_documents(conn) if hosted else 0,
        remaining=_count(conn, _REMAINING_SQL.format(where=_where(*gate)), identity),
        foreign=_count(conn, _FOREIGN_SQL.format(where=_where(*gate)), identity),
        stopped_by=stopped_by,
    )


def _count(conn: psycopg.Connection[Any], query: str, identity: str) -> int:
    row = conn.execute(query, (identity,)).fetchone()
    return int(row[0]) if row is not None else 0
