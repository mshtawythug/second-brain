"""Direct unit tests for ``brain.queries`` shared helpers.

The CLI and MCP server both call into these — they're covered indirectly by
the existing test suites, but these tests exercise the module's exception
hierarchy and the defensive ``None`` branch in :func:`fetch_document`
directly so the contract stays pinned.
"""
from typing import Any

import psycopg
import pytest

from brain.errors import (
    IdPrefixAmbiguous,
    IdPrefixNotFound,
    IdPrefixNotHex,
    IdPrefixTooShort,
)
from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import (
    count_chunks_missing_embedding,
    embedding_column_state,
    fetch_document,
    finalize_embedding_index,
    iter_chunks_missing_embedding,
    list_documents,
    resolve_document_prefix,
    summary_counts,
)


def _seed_doc_for_chunks(conn: psycopg.Connection) -> str:
    """Insert a parent ``documents`` row and return its id (no chunks)."""
    row = conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, %s) RETURNING id::text",
        ("t", "body", "h-" + str(hash(("doc", id(conn)))), "note"),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _insert_chunk(
    conn: psycopg.Connection,
    *,
    document_id: str,
    chunk_index: int,
    content: str,
    embedding: list[float] | None,
) -> str:
    """Insert one chunks row directly via SQL (bypassing ingest)."""
    row = conn.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
        "VALUES (%s, %s, %s, %s) RETURNING id::text",
        (document_id, chunk_index, content, embedding),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _seed(
    conn: psycopg.Connection, fake_embedder: Any, *, title: str = "t"
) -> str:
    result = ingest_document(
        conn,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title=title,
            content="alpha bravo body",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=[],
    )
    assert result.document_id is not None
    return result.document_id


def test_resolve_document_prefix_returns_full_id(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    doc_id = _seed(test_db, fake_embedder)
    assert resolve_document_prefix(test_db, doc_id[:8]) == doc_id


def test_resolve_document_prefix_too_short(
    test_db: psycopg.Connection,
) -> None:
    with pytest.raises(IdPrefixTooShort):
        resolve_document_prefix(test_db, "abc")


def test_resolve_document_prefix_non_hex(
    test_db: psycopg.Connection,
) -> None:
    with pytest.raises(IdPrefixNotHex):
        resolve_document_prefix(test_db, "abc_de%")


def test_resolve_document_prefix_not_found(
    test_db: psycopg.Connection,
) -> None:
    with pytest.raises(IdPrefixNotFound):
        resolve_document_prefix(test_db, "ffffff")


def test_resolve_document_prefix_ambiguous(
    test_db: psycopg.Connection,
) -> None:
    for new_id in (
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "aaaaaabb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ):
        test_db.execute(
            "INSERT INTO documents (id, title, content, content_hash, "
            "content_type) VALUES (%s, %s, %s, %s, %s)",
            (new_id, "t", "body", new_id + "_h", "note"),
        )
    with pytest.raises(IdPrefixAmbiguous):
        resolve_document_prefix(test_db, "aaaaaa")


def test_fetch_document_returns_none_for_missing_id(
    test_db: psycopg.Connection,
) -> None:
    """Defensive: caller may have raced; fetch returns ``None`` rather than crashing."""
    assert (
        fetch_document(test_db, "00000000-0000-0000-0000-000000000000") is None
    )


def test_list_documents_filters_round_trip(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Smoke test that the projection populates the expected DocumentRow fields."""
    doc_id = _seed(test_db, fake_embedder, title="Doc")
    rows = list_documents(test_db, limit=5)
    assert len(rows) == 1
    only = rows[0]
    assert only.id == doc_id
    assert only.title == "Doc"
    assert only.content_type == "note"
    assert only.tags == []
    # list projection omits the body + source_path.
    assert only.content is None
    assert only.source_path is None


def test_summary_counts_on_empty_db(test_db: psycopg.Connection) -> None:
    """Empty brain → zero counts and ``last_ingest`` is ``None``."""
    counts = summary_counts(test_db)
    assert counts.documents == 0
    assert counts.chunks == 0
    assert counts.sources == 0
    assert counts.last_ingest is None
    assert counts.by_kind == []


def test_summary_counts_reflects_db_state(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Seed a mix of source kinds and verify every field of ``StatusCounts``."""
    # One manual doc (no source row).
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="manual one",
            content="manual body alpha",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=[],
    )
    # Two krisp docs (each gets its own sources row via external_id).
    for n in (1, 2):
        ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=ExtractedDoc(
                title=f"krisp {n}",
                content=f"krisp body {n} unique",
                content_type="transcript",
                source_path=None,
                metadata={},
            ),
            source_kind="krisp",
            source_external_id=f"krisp:{n}",
            tags=[],
        )

    counts = summary_counts(test_db)
    assert counts.documents == 3
    assert counts.chunks >= 3  # one chunk per short doc, possibly more
    assert counts.sources == 2  # only the two krisp docs created sources rows
    assert counts.last_ingest is not None
    by_kind = dict(counts.by_kind)
    # Both kinds present; krisp first by count desc, manual still listed.
    assert by_kind == {"krisp": 2, "manual": 1}
    # by_kind is a stable list of (str, int) tuples.
    for kind, count in counts.by_kind:
        assert isinstance(kind, str)
        assert isinstance(count, int)


# --- Phase 3 helpers: reembed / finalize ------------------------------------


def _all_zero_vec(dim: int = 4096) -> list[float]:
    """A non-NULL placeholder embedding for chunks that already have one."""
    return [0.0] * dim


def test_iter_chunks_missing_embedding_yields_only_null(
    test_db: psycopg.Connection,
) -> None:
    """One chunk has an embedding, two are NULL — iterator yields the two NULL."""
    doc_id = _seed_doc_for_chunks(test_db)
    _insert_chunk(
        test_db,
        document_id=doc_id,
        chunk_index=0,
        content="already embedded",
        embedding=_all_zero_vec(),
    )
    null_ids = {
        _insert_chunk(
            test_db,
            document_id=doc_id,
            chunk_index=i,
            content=f"needs embed {i}",
            embedding=None,
        )
        for i in (1, 2)
    }

    yielded = [c for batch in iter_chunks_missing_embedding(test_db) for c in batch]

    assert {c.id for c in yielded} == null_ids
    assert all("needs embed" in c.content for c in yielded)


def test_iter_chunks_missing_embedding_batches(
    test_db: psycopg.Connection,
) -> None:
    """5 NULL chunks with batch_size=2 → batches of (2, 2, 1)."""
    doc_id = _seed_doc_for_chunks(test_db)
    for i in range(5):
        _insert_chunk(
            test_db,
            document_id=doc_id,
            chunk_index=i,
            content=f"chunk {i}",
            embedding=None,
        )

    sizes = [
        len(batch) for batch in iter_chunks_missing_embedding(test_db, batch_size=2)
    ]

    assert sizes == [2, 2, 1]


def test_count_chunks_missing_embedding(test_db: psycopg.Connection) -> None:
    """Counter reflects only NULL-embedding chunks."""
    doc_id = _seed_doc_for_chunks(test_db)
    assert count_chunks_missing_embedding(test_db) == 0
    _insert_chunk(
        test_db,
        document_id=doc_id,
        chunk_index=0,
        content="filled",
        embedding=_all_zero_vec(),
    )
    _insert_chunk(
        test_db,
        document_id=doc_id,
        chunk_index=1,
        content="empty",
        embedding=None,
    )
    _insert_chunk(
        test_db,
        document_id=doc_id,
        chunk_index=2,
        content="empty too",
        embedding=None,
    )

    assert count_chunks_missing_embedding(test_db) == 2


class _FixedDimEmbedder:
    """Tiny test double that satisfies the Embedder Protocol's ``dim`` only.

    The finalize path doesn't actually call ``embed`` / ``count_tokens``
    so a stub with just ``dim`` is sufficient and keeps the parametrized
    finalize tests focused on the schema effect.
    """

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed(  # pragma: no cover - never called from finalize tests
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]

    def count_tokens(self, text: str) -> int:  # pragma: no cover - same
        return len(text)


def test_finalize_embedding_index_applies_not_null_for_qwen3(
    test_db: psycopg.Connection,
) -> None:
    """qwen3 (dim=4096): finalize applies NOT NULL but creates no HNSW index.

    pgvector 0.8.x caps HNSW at 2000 dims for ``vector``; the qwen3 backend
    intentionally rides on sequential scan instead.
    """
    doc_id = _seed_doc_for_chunks(test_db)
    _insert_chunk(
        test_db,
        document_id=doc_id,
        chunk_index=0,
        content="filled",
        embedding=_all_zero_vec(),
    )

    finalize_embedding_index(test_db, _FixedDimEmbedder(dim=4096))

    state = embedding_column_state(test_db)
    assert state.not_null
    assert "vector(4096)" in state.column_type
    # qwen3 path: index intentionally skipped.
    idx = test_db.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'chunks_embedding_idx'"
    ).fetchone()
    assert idx is None
    assert state.has_index is False


def test_finalize_embedding_index_creates_hnsw_for_arctic(
    test_db: psycopg.Connection,
) -> None:
    """arctic / voyage (dim=1024): finalize creates the HNSW cosine index.

    Resizes the column to 1024 first so the index can actually be built —
    the session-scoped fixture leaves it at 4096 (qwen3 default). This
    mirrors what ``ensure_embedding_column`` does at ``brain init`` time
    when the active backend is arctic or voyage.
    """
    test_db.execute("ALTER TABLE chunks DROP COLUMN embedding")
    test_db.execute("ALTER TABLE chunks ADD COLUMN embedding vector(1024)")

    doc_id = _seed_doc_for_chunks(test_db)
    _insert_chunk(
        test_db,
        document_id=doc_id,
        chunk_index=0,
        content="filled",
        embedding=[0.0] * 1024,
    )

    finalize_embedding_index(test_db, _FixedDimEmbedder(dim=1024))

    state = embedding_column_state(test_db)
    assert state.not_null
    assert "vector(1024)" in state.column_type
    assert state.has_index is True
    idx = test_db.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'chunks_embedding_idx'"
    ).fetchone()
    assert idx is not None


def test_finalize_embedding_index_rejects_when_nulls_remain(
    test_db: psycopg.Connection,
) -> None:
    """A NULL embedding still present → ValueError, schema untouched."""
    doc_id = _seed_doc_for_chunks(test_db)
    _insert_chunk(
        test_db,
        document_id=doc_id,
        chunk_index=0,
        content="empty",
        embedding=None,
    )

    with pytest.raises(ValueError, match="cannot finalize"):
        finalize_embedding_index(test_db, _FixedDimEmbedder(dim=4096))

    state = embedding_column_state(test_db)
    assert not state.not_null  # schema unchanged


def test_finalize_embedding_index_idempotent(
    test_db: psycopg.Connection,
) -> None:
    """Calling finalize twice in a row is a no-op the second time."""
    doc_id = _seed_doc_for_chunks(test_db)
    _insert_chunk(
        test_db,
        document_id=doc_id,
        chunk_index=0,
        content="filled",
        embedding=_all_zero_vec(),
    )

    embedder = _FixedDimEmbedder(dim=4096)
    finalize_embedding_index(test_db, embedder)
    finalize_embedding_index(test_db, embedder)  # must not raise

    state = embedding_column_state(test_db)
    assert state.not_null


def test_embedding_column_state_pre_finalize(
    test_db: psycopg.Connection,
) -> None:
    """Fresh schema (post-migration, pre-finalize): nullable column, no index."""
    state = embedding_column_state(test_db)
    assert "vector(4096)" in state.column_type
    assert not state.not_null
    assert state.has_index is False
