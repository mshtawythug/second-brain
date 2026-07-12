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
    BrainError,
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

# The ``finalize_embedding_index`` tests below apply NOT NULL / build indexes /
# resize the embedding column (schema mutation) — route the whole module to the
# full drop+migrate reset via the Wave 6.1 ``fresh_schema`` marker.
pytestmark = pytest.mark.fresh_schema


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


# --- G0b: generalized (table, column) finalize ------------------------------
# finalize_embedding_index is parameterized over (table, column) + create_hnsw.
# These tests prove (a) the chunks path is unchanged via the explicit-arg
# signature (NOT NULL + HNSW), (b) graph_entities with create_hnsw=False stays
# NULLABLE with no index even when a NULL embedding is present, and (c) a
# non-allowlisted pair is rejected.


def _graph_entities_index_exists(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'graph_entities_embedding_idx'"
    ).fetchone()
    return row is not None


def _graph_entities_embedding_nullable(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'graph_entities' AND column_name = 'embedding'"
    ).fetchone()
    assert row is not None
    return str(row[0]) == "YES"


def test_finalize_embedding_index_chunks_explicit_args_match_default(
    test_db: psycopg.Connection,
) -> None:
    """Regression: explicit ``("chunks", "embedding")`` + ``create_hnsw=True``
    is byte-equivalent to the default chunks path — NOT NULL is applied and the
    HNSW cosine index is created for a 1024-dim backend.
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

    finalize_embedding_index(
        test_db, _FixedDimEmbedder(dim=1024), "chunks", "embedding", create_hnsw=True
    )

    state = embedding_column_state(test_db)
    assert state.not_null
    assert "vector(1024)" in state.column_type
    assert state.has_index is True
    idx = test_db.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'chunks_embedding_idx'"
    ).fetchone()
    assert idx is not None


def test_finalize_embedding_index_graph_entities_skips_hnsw_and_not_null(
    test_db: psycopg.Connection,
) -> None:
    """graph_entities + ``create_hnsw=False`` → no HNSW, no NOT NULL.

    A NULL embedding is present (the normal post-migration state); the
    create_hnsw=False path must NOT raise (no NULL-completeness requirement),
    must NOT create the HNSW index, and must leave the column NULLABLE.
    """
    test_db.execute(
        "INSERT INTO graph_entities (entity_type, name, canonical_key) "
        "VALUES (%s, %s, %s)",
        ("topic", "Beta", "beta"),
    )
    assert _graph_entities_embedding_nullable(test_db)
    assert not _graph_entities_index_exists(test_db)

    finalize_embedding_index(
        test_db,
        _FixedDimEmbedder(dim=1024),
        "graph_entities",
        "embedding",
        create_hnsw=False,
    )

    # Column still NULLABLE (no NOT NULL forced) and no HNSW index created.
    assert _graph_entities_embedding_nullable(test_db)
    assert not _graph_entities_index_exists(test_db)


def test_finalize_embedding_index_rejects_non_allowlisted(
    test_db: psycopg.Connection,
) -> None:
    """A ``(table, column)`` pair off the allowlist raises before any DDL."""
    with pytest.raises(BrainError, match="allowlist"):
        finalize_embedding_index(
            test_db, _FixedDimEmbedder(dim=1024), "documents", "tsv"
        )


def test_embedding_column_state_pre_finalize(
    test_db: psycopg.Connection,
) -> None:
    """Fresh schema (post-migration, pre-finalize): nullable column, no index."""
    state = embedding_column_state(test_db)
    assert "vector(4096)" in state.column_type
    assert not state.not_null
    assert state.has_index is False


# ---------------------------------------------------------------------------
# Mirror drift helpers — used by ``brain doctor`` and
# ``brain vault prune-orphans``.
# ---------------------------------------------------------------------------


def _write_mirror_file(
    path: "Any", *, frontmatter: dict[str, Any], body: str
) -> None:
    """Write a vault mirror file with YAML frontmatter + body."""
    from brain.vault.frontmatter import dump_frontmatter

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(frontmatter, body), encoding="utf-8")


def test_iter_orphan_mirror_files(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: "Any"
) -> None:
    """The iterator yields exactly the file whose frontmatter id has no DB row.

    Setup: a vault folder with three mirror files —
      1. one whose frontmatter id matches a real ``documents.id`` (NOT orphan)
      2. one with a fresh UUID that has no DB row (ORPHAN — the only yield)
      3. one with no frontmatter at all (vault README — skipped)
    Plus a control file outside ``_ingested/`` that must never be considered.

    Exercise: iterate :func:`iter_orphan_mirror_files`.

    Verify: yields exactly the orphan path (in any order, but here only one).
    """
    from brain.queries import iter_orphan_mirror_files

    real_id = _seed(test_db, fake_embedder, title="real")
    vault = tmp_path / "vault"

    not_orphan = vault / "_ingested" / "manual" / "real.md"
    _write_mirror_file(
        not_orphan,
        frontmatter={"id": real_id, "title": "real"},
        body="real body\n",
    )

    orphan_id = "00000000-0000-4000-8000-000000000abc"
    orphan = vault / "_ingested" / "manual" / "orphan.md"
    _write_mirror_file(
        orphan,
        frontmatter={"id": orphan_id, "title": "ghost"},
        body="orphan body\n",
    )

    # No-frontmatter README — the iterator must skip it.
    readme = vault / "_ingested" / "README.md"
    readme.parent.mkdir(parents=True, exist_ok=True)
    readme.write_text("Just a plain README, no YAML.\n", encoding="utf-8")

    # Control file outside _ingested/ — must never be considered.
    elsewhere = vault / "notes" / "elsewhere.md"
    _write_mirror_file(
        elsewhere,
        frontmatter={"id": "ffffffff-ffff-4fff-bfff-ffffffffffff", "title": "x"},
        body="not in scope\n",
    )

    yielded = list(iter_orphan_mirror_files(test_db, vault_path=vault))
    assert yielded == [orphan]


def test_iter_stale_mirror_files(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: "Any"
) -> None:
    """The iterator yields files whose id resolves but path != row's vault_path.

    Setup: a vault with three mirror files all carrying the same frontmatter id —
      1. canonical: at the path stored in ``documents.vault_path`` (NOT stale)
      2. stale: same id, different path (YIELDED — leftover from a slug-shape change)
      3. another stale: same id, third path (YIELDED — multiple stales for one row)
    Plus a control file with no frontmatter (skipped) and a true orphan whose id
    doesn't resolve (NOT yielded — that's :func:`iter_orphan_mirror_files`'s job).
    """
    from brain.queries import iter_stale_mirror_files

    real_id = _seed(test_db, fake_embedder, title="real")
    canonical_relative = "_ingested/manual/real.md"
    test_db.execute(
        "UPDATE documents SET vault_path = %s WHERE id = %s",
        (canonical_relative, real_id),
    )

    vault = tmp_path / "vault"

    canonical_path = vault / canonical_relative
    _write_mirror_file(
        canonical_path,
        frontmatter={"id": real_id, "title": "real"},
        body="canonical body\n",
    )

    stale_a = vault / "_ingested" / "manual" / "real-old-shape.md"
    _write_mirror_file(
        stale_a,
        frontmatter={"id": real_id, "title": "real"},
        body="leftover from earlier slug shape\n",
    )

    stale_b = vault / "_ingested" / "manual" / "real-older-shape-deadbeef.md"
    _write_mirror_file(
        stale_b,
        frontmatter={"id": real_id, "title": "real"},
        body="even earlier leftover\n",
    )

    # True orphan — id has no DB row. iter_orphan_mirror_files would catch this,
    # but iter_stale_mirror_files must NOT yield it.
    pure_orphan = vault / "_ingested" / "manual" / "ghost.md"
    _write_mirror_file(
        pure_orphan,
        frontmatter={"id": "00000000-0000-4000-8000-000000000def", "title": "ghost"},
        body="ghost\n",
    )

    yielded = sorted(iter_stale_mirror_files(test_db, vault_path=vault))
    assert yielded == sorted([stale_a, stale_b])


def test_mirror_drift_summary(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: "Any"
) -> None:
    """Each of the four counters reflects independent DB/disk state.

    Setup pieces (each independent of the others):

    - One ingested doc + matching mirror file (healthy: contributes only to
      the total).
    - One ingested doc whose ``vault_path`` is NULL (after-ingest, pre-export
      state).
    - One ingested doc whose ``vault_path`` points at a file that doesn't
      exist (ghost row).
    - One on-disk file under ``_ingested/`` whose frontmatter id has no DB
      row (orphan).

    Exercise: :func:`mirror_drift_summary`.

    Verify: the dataclass returns ``(total=3, null=1, ghost=1, orphan=1)``.
    """
    from brain.ingest import ExtractedDoc, ingest_document
    from brain.queries import mirror_drift_summary
    from brain.vault.frontmatter import dump_frontmatter

    vault = tmp_path / "vault"

    def _ingest_unique(title: str, body: str) -> str:
        # Use distinct body bytes per row so ``documents_content_hash_stdin_idx``
        # (UNIQUE on content_hash WHERE kind='ingested' AND source_path IS NULL)
        # never silently dedupes the seeds.
        result = ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=ExtractedDoc(
                title=title,
                content=body,
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
        )
        assert result.document_id is not None
        return result.document_id

    # 1. Healthy ingest: row + matching file.
    healthy_id = _ingest_unique("healthy", "healthy unique body")
    healthy_rel = "_ingested/manual/healthy.md"
    test_db.execute(
        "UPDATE documents SET kind='ingested', vault_path=%s WHERE id=%s",
        (healthy_rel, healthy_id),
    )
    healthy_path = vault / healthy_rel
    healthy_path.parent.mkdir(parents=True, exist_ok=True)
    healthy_path.write_text(
        dump_frontmatter({"id": healthy_id, "title": "healthy"}, "x\n"),
        encoding="utf-8",
    )

    # 2. NULL-vault_path row: ingested but never exported.
    null_id = _ingest_unique("null-vp", "null-vp distinct body")
    test_db.execute(
        "UPDATE documents SET kind='ingested', vault_path=NULL WHERE id=%s",
        (null_id,),
    )

    # 3. Ghost row: vault_path set, file missing on disk.
    ghost_id = _ingest_unique("ghost", "ghost-row distinct body")
    test_db.execute(
        "UPDATE documents SET kind='ingested', vault_path=%s WHERE id=%s",
        ("_ingested/manual/ghost.md", ghost_id),
    )
    # Intentionally NO file on disk for ghost.

    # 4. Orphan file: on disk, no DB row.
    orphan_uuid = "11111111-1111-4111-8111-111111111111"
    orphan_path = vault / "_ingested" / "manual" / "orphan.md"
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.write_text(
        dump_frontmatter({"id": orphan_uuid, "title": "orphan"}, "y\n"),
        encoding="utf-8",
    )

    # 5. A vault-tier (non-ingested) row: excluded from every ingested counter,
    #    but its id still joins ``known_ids`` for orphan resolution.
    vault_id = _ingest_unique("vault-tier", "vault-tier distinct body")
    test_db.execute(
        "UPDATE documents SET kind='vault', vault_path=%s WHERE id=%s",
        ("some-note.md", vault_id),
    )

    summary = mirror_drift_summary(test_db, vault_path=vault)
    assert summary.total_ingested_rows == 3
    assert summary.rows_with_null_vault_path == 1
    assert summary.ghost_rows == 1
    assert summary.orphan_files == 1


def test_iter_orphan_mirror_files_frontmatter_edge_cases(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: "Any"
) -> None:
    """The scan reads only the *frontmatter* block and skips unparseable files.

    Regression guard for the drift scan reading just each file's frontmatter
    region (rather than the whole file) and loading it with libyaml. Four
    tricky mirror files exercise the boundaries:

    1. Healthy row whose *body* contains a ``---`` line and a decoy ``id:`` —
       the id must come from the top frontmatter (resolves ⇒ NOT orphan), never
       from the body. Proves the region read stops at the first closing fence.
    2. A true orphan (unresolved id) whose body also contains a ``---`` line —
       the single expected yield.
    3. Malformed YAML frontmatter — must be skipped (matches the previous
       ``except yaml.YAMLError`` behavior), not counted as orphan.
    4. A leading ``---`` fence with no closing fence — treated as bodyless /
       no-frontmatter and skipped.

    Verify: :func:`iter_orphan_mirror_files` yields exactly the orphan.
    """
    from brain.queries import iter_orphan_mirror_files

    real_id = _seed(test_db, fake_embedder, title="real")
    vault = tmp_path / "vault"

    # 1. Healthy: real id in frontmatter, decoy ``---`` + ``id:`` in the body.
    healthy = vault / "_ingested" / "manual" / "real.md"
    _write_mirror_file(
        healthy,
        frontmatter={"id": real_id, "title": "real"},
        body="intro line\n---\nid: 22222222-2222-4222-8222-222222222222\noutro\n",
    )

    # 2. True orphan: unresolved id, body also carries a ``---`` fence.
    orphan_id = "00000000-0000-4000-8000-0000000edcba"
    orphan = vault / "_ingested" / "manual" / "orphan.md"
    _write_mirror_file(
        orphan,
        frontmatter={"id": orphan_id, "title": "ghost"},
        body="body\n---\ntrailing\n",
    )

    # 3. Malformed YAML frontmatter — unterminated flow sequence.
    malformed = vault / "_ingested" / "manual" / "malformed.md"
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text(
        "---\nid: [unclosed\ntitle: broken\n---\nbody\n", encoding="utf-8"
    )

    # 4. Leading fence, no closing fence — no frontmatter, skipped.
    no_close = vault / "_ingested" / "manual" / "no-close.md"
    no_close.write_text(
        "---\nid: 33333333-3333-4333-8333-333333333333\nbody with no second fence\n",
        encoding="utf-8",
    )

    # 5. Empty frontmatter block — no id, skipped.
    empty_fm = vault / "_ingested" / "manual" / "empty-fm.md"
    empty_fm.write_text("---\n---\nbody\n", encoding="utf-8")

    # 6. Non-mapping frontmatter (a bare YAML list) — skipped, matching the
    #    previous ``parse_frontmatter`` ValueError path.
    non_mapping = vault / "_ingested" / "manual" / "non-mapping.md"
    non_mapping.write_text("---\n- one\n- two\n---\nbody\n", encoding="utf-8")

    # 7. Frontmatter with no ``id`` key — skipped.
    no_id = vault / "_ingested" / "manual" / "no-id.md"
    no_id.write_text("---\ntitle: has no id\n---\nbody\n", encoding="utf-8")

    # 8. A directory whose name ends in ``.md`` — rglob yields it, the scan
    #    skips it (not a regular file).
    (vault / "_ingested" / "manual" / "a-directory.md").mkdir(
        parents=True, exist_ok=True
    )

    yielded = list(iter_orphan_mirror_files(test_db, vault_path=vault))
    assert yielded == [orphan]


def test_read_mirror_frontmatter_id_on_directory_returns_none(
    tmp_path: "Any",
) -> None:
    """Opening a directory path yields ``OSError`` → ``None`` (skip), not a crash.

    Guards the ``except OSError`` branch of :func:`_read_mirror_frontmatter_id`:
    ``path.open()`` on a directory raises ``IsADirectoryError`` (an ``OSError``),
    which the scan swallows into a skip rather than propagating.
    """
    from brain.queries import _read_mirror_frontmatter_id

    a_dir = tmp_path / "not-a-file.md"
    a_dir.mkdir()
    assert _read_mirror_frontmatter_id(a_dir) is None


def test_mirror_scan_on_vault_without_ingested_tier(
    test_db: psycopg.Connection, tmp_path: "Any"
) -> None:
    """A vault with no ``_ingested/`` tier scans cleanly instead of crashing.

    Guards the ``if not ingested_dir.is_dir(): return`` short-circuits in
    :func:`iter_orphan_mirror_files` and :func:`iter_stale_mirror_files` (which
    also skip their SQL when there is nothing on disk to compare against) and
    the analogous empty-summary path of :func:`mirror_drift_summary`.
    """
    from brain.queries import (
        iter_orphan_mirror_files,
        iter_stale_mirror_files,
        mirror_drift_summary,
    )

    vault = tmp_path / "vault"
    vault.mkdir()  # exists, but no `_ingested/` subdirectory

    assert list(iter_orphan_mirror_files(test_db, vault_path=vault)) == []
    assert list(iter_stale_mirror_files(test_db, vault_path=vault)) == []

    summary = mirror_drift_summary(test_db, vault_path=vault)
    assert summary.total_ingested_rows == 0
    assert summary.rows_with_null_vault_path == 0
    assert summary.ghost_rows == 0
    assert summary.orphan_files == 0
