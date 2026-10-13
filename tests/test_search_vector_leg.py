"""Vector-leg candidate pool: exact scan, full pool, filters never starve it.

Regression tests for the defect in ``docs/specs/2026-10-13-search-vector-leg-design.md``:
the vector leg ordered by ``embedding <=> q``, so the planner served it from the
HNSW index, which returns at most ``hnsw.ef_search`` (default 40) rows and applies
every ``WHERE`` filter *after* the index scan. On the live corpus that meant ~40
candidates instead of ``CANDIDATE_LIMIT``, 14-33 on filtered queries, and a recall
of 0.757 against an exact scan.

The test database cannot reproduce that by default: its ``chunks.embedding`` is
``vector(4096)``, over pgvector's 2,000-dim HNSW cap, so it has no HNSW index and
every vector query is already exact. The real-DB tests below therefore rebuild
the production shape under ``fresh_schema`` — 1024 dims via
:func:`brain.db.ensure_embedding_column` and the HNSW index via
:func:`brain.queries.finalize_embedding_index`, the same calls ``brain init`` /
``brain reembed`` make — and assert, as a precondition, that the planner really
chooses the index for an index-shaped query. Without that precondition a pass
would prove nothing.

The expected candidate set comes from an independent oracle: cosine computed in
Python over the stored embeddings, never from SQL that mirrors the code under
test.
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from brain.db import ensure_embedding_column
from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import finalize_embedding_index
from brain.search import CANDIDATE_LIMIT, hybrid_search
from brain.search_predicate import SearchPredicate, build_predicate
from brain.search_vector_leg import vector_leg_sql
from tests.conftest import FakeEmbedder

# The production HNSW-capable dimensionality (arctic / voyage).
_DIM = 1024
# Enough one-chunk documents that the exact pool (CANDIDATE_LIMIT = 50) is
# strictly larger than what an ef_search=40 index scan can return.
_DOC_COUNT = 90
# Every third document carries this tag: 30 matches, fewer than CANDIDATE_LIMIT,
# so a correct filtered leg returns ALL of them.
_KEEP_TAG = "vector-leg-keep"
# Matches no chunk lexically, so the FTS leg is empty and every result's
# presence is attributable to the vector leg alone.
_NO_FTS_QUERY = "zzqxvectorlegprobe"


# ---------------------------------------------------------------------------
# SQL builder — unit tests (no database)
# ---------------------------------------------------------------------------
def _order_by_clause(sql: str) -> str:
    tail = sql.split("ORDER BY", 1)[1]
    return tail.split("LIMIT", 1)[0].strip()


def test_orders_by_score_never_by_the_index_operator() -> None:
    """The ORDER BY must be one an HNSW index cannot serve — that is the fix."""
    sql, _ = vector_leg_sql(
        predicate=build_predicate(),
        query_embedding=[0.1, 0.2],
        vector_sim_floor=0.25,
        limit=CANDIDATE_LIMIT,
    )
    order_by = _order_by_clause(sql)
    assert "<=>" not in order_by
    assert order_by == "score DESC, c.id"


def test_limit_is_the_argument() -> None:
    for limit in (1, 7, CANDIDATE_LIMIT):
        sql, _ = vector_leg_sql(
            predicate=build_predicate(),
            query_embedding=[0.1],
            vector_sim_floor=0.0,
            limit=limit,
        )
        assert sql.rstrip().endswith(f"LIMIT {limit}")


@pytest.mark.parametrize("bad", [0, -1, True, 2.5])
def test_rejects_a_limit_that_is_not_a_positive_int(bad: Any) -> None:
    with pytest.raises(ValueError, match="limit"):
        vector_leg_sql(
            predicate=build_predicate(),
            query_embedding=[0.1],
            vector_sim_floor=0.0,
            limit=bad,
        )


def test_params_without_filters() -> None:
    emb = [0.5, -0.5]
    sql, params = vector_leg_sql(
        predicate=build_predicate(),
        query_embedding=emb,
        vector_sim_floor=0.3,
        limit=10,
    )
    assert params == [emb, emb, 0.3]
    assert sql.count("%s") == len(params)
    assert "JOIN documents" not in sql


def test_params_with_filters_carry_the_shared_predicate() -> None:
    emb = [0.5, -0.5]
    predicate = build_predicate(tag="some-tag")
    sql, params = vector_leg_sql(
        predicate=predicate,
        query_embedding=emb,
        vector_sim_floor=0.3,
        limit=10,
    )
    assert params == [emb, *predicate.where_params, emb, 0.3]
    assert sql.count("%s") == len(params)
    assert predicate.join_clause in sql
    assert predicate.where_sql in sql


def test_floor_is_bound_never_interpolated() -> None:
    sql, params = vector_leg_sql(
        predicate=build_predicate(),
        query_embedding=[0.1],
        vector_sim_floor=0.31415,
        limit=10,
    )
    assert "0.31415" not in sql
    assert params[-1] == 0.31415


# ---------------------------------------------------------------------------
# Real database with a real HNSW index
# ---------------------------------------------------------------------------
@pytest.fixture
def hnsw_corpus(
    test_db: psycopg.Connection[Any],
) -> Iterator[tuple[psycopg.Connection[Any], FakeEmbedder]]:
    """``_DOC_COUNT`` one-chunk docs on a 1024-dim column with its HNSW index.

    ``enable_seqscan = off`` and ``enable_sort = off`` apply the planner pressure
    under which the live corpus picks the index. On 21k chunks the HNSW scan is
    simply cheapest; on 90 rows the planner would otherwise serve a filtered query
    with a nested loop over ``documents`` plus an exact sort, which is exact by
    accident and would let the old SQL pass. Disabling the sort leaves the index
    scan as the only path that delivers ordered rows. The new SQL orders by an
    expression no index provides, so it still sorts (the setting is a cost
    penalty, not a ban). The precondition below proves the pressure took effect
    for both query shapes.
    """
    embedder = FakeEmbedder(dim=_DIM)
    ensure_embedding_column(test_db, embedder)
    for i in range(_DOC_COUNT):
        result = ingest_document(
            test_db,
            embedder=embedder,
            doc=ExtractedDoc(
                title=f"Synthetic vector-leg doc {i:03d}",
                content=f"synthetic body number {i:03d} for the vector leg pool",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:vector-leg-{i:03d}",
            tags=[_KEEP_TAG] if i % 3 == 0 else [],
        )
        assert result.document_id is not None
    finalize_embedding_index(test_db, embedder)
    test_db.execute("SET enable_seqscan = off")
    test_db.execute("SET enable_sort = off")
    _assert_planner_uses_hnsw(test_db, embedder, build_predicate())
    _assert_planner_uses_hnsw(test_db, embedder, build_predicate(tag=_KEEP_TAG))
    yield test_db, embedder


def _assert_planner_uses_hnsw(
    conn: psycopg.Connection[Any], embedder: FakeEmbedder, predicate: SearchPredicate
) -> None:
    """Precondition: the pre-fix, index-shaped query is served by the HNSW index.

    Built with the same predicate machinery ``hybrid_search`` uses. If this fails
    the environment no longer reproduces production planning, and every
    assertion below would pass for the wrong reason.
    """
    q = embedder.embed([_NO_FTS_QUERY], input_type="query")[0]
    where = f"WHERE {predicate.where_sql}" if predicate.has_filters else ""
    plan = "\n".join(
        row[0]
        for row in conn.execute(
            f"EXPLAIN SELECT c.id FROM chunks c {predicate.join_clause} {where} "
            "ORDER BY c.embedding <=> %s::vector LIMIT 50",
            [*predicate.where_params, q],
        ).fetchall()
    )
    assert "chunks_embedding_idx" in plan, plan


def _oracle_ranking(
    conn: psycopg.Connection[Any],
    embedder: FakeEmbedder,
    *,
    tag: str | None = None,
) -> list[tuple[float, str]]:
    """``(cosine, document_id)`` for every chunk, best first, computed in Python."""
    q = embedder.embed([_NO_FTS_QUERY], input_type="query")[0]
    q_norm = math.sqrt(sum(x * x for x in q))
    sql = (
        "SELECT c.id::text, c.document_id::text, c.embedding::text "
        "FROM chunks c JOIN documents d ON d.id = c.document_id"
    )
    params: list[Any] = []
    if tag is not None:
        sql += " WHERE %s = ANY(d.tags)"
        params.append(tag)
    ranked: list[tuple[float, str, str]] = []
    for chunk_id, doc_id, emb_text in conn.execute(sql, params).fetchall():
        vec = json.loads(emb_text)
        dot = sum(a * b for a, b in zip(q, vec, strict=True))
        cos = dot / (q_norm * math.sqrt(sum(x * x for x in vec)))
        ranked.append((cos, chunk_id, doc_id))
    ranked.sort(key=lambda r: (-r[0], r[1]))
    return [(cos, doc_id) for cos, _, doc_id in ranked]


def _vector_leg_docs(
    conn: psycopg.Connection[Any],
    embedder: FakeEmbedder,
    *,
    vector_sim_floor: float,
    tag: str | None = None,
) -> list[str]:
    """Document ids the vector leg contributed, in vector-rank order."""
    results = hybrid_search(
        conn,
        embedder=embedder,
        query=_NO_FTS_QUERY,
        limit=_DOC_COUNT * 2,
        vector_sim_floor=vector_sim_floor,
        tag=tag,
        explain=True,
    )
    assert all(r.explain is not None and r.explain.fts_rank is None for r in results)
    vec = [r for r in results if r.explain is not None and r.explain.vector_rank is not None]
    vec.sort(key=lambda r: r.explain.vector_rank if r.explain else 0)
    return [r.document_id for r in vec]


@pytest.mark.fresh_schema
def test_vector_leg_returns_the_exact_full_pool(
    hnsw_corpus: tuple[psycopg.Connection[Any], FakeEmbedder],
) -> None:
    """All ``CANDIDATE_LIMIT`` nearest chunks, in exact order — not ef_search's 40."""
    conn, embedder = hnsw_corpus
    expected = [doc for _, doc in _oracle_ranking(conn, embedder)][:CANDIDATE_LIMIT]
    assert len(expected) == CANDIDATE_LIMIT  # the pool is genuinely larger than 40

    assert _vector_leg_docs(conn, embedder, vector_sim_floor=-1.0) == expected


@pytest.mark.fresh_schema
def test_filtered_vector_leg_keeps_every_matching_chunk(
    hnsw_corpus: tuple[psycopg.Connection[Any], FakeEmbedder],
) -> None:
    """A filter removes non-matching rows; it never starves the pool.

    The 30 tagged chunks all fit in the pool, so a correct leg returns all 30.
    Post-filtering an ef_search=40 index scan keeps only the tagged fraction of
    40 — about 13.
    """
    conn, embedder = hnsw_corpus
    expected = [doc for _, doc in _oracle_ranking(conn, embedder, tag=_KEEP_TAG)]
    assert len(expected) == _DOC_COUNT // 3

    got = _vector_leg_docs(conn, embedder, vector_sim_floor=-1.0, tag=_KEEP_TAG)
    assert got == expected


@pytest.mark.fresh_schema
def test_vector_leg_still_honours_the_cosine_floor(
    hnsw_corpus: tuple[psycopg.Connection[Any], FakeEmbedder],
) -> None:
    """Exactly the chunks at or above the floor, when fewer than the pool size."""
    conn, embedder = hnsw_corpus
    ranking = _oracle_ranking(conn, embedder)
    keep = 20
    # A floor midway between the 20th and 21st cosines admits exactly 20 chunks,
    # with no dependence on float equality at the boundary.
    floor = (ranking[keep - 1][0] + ranking[keep][0]) / 2
    expected = [doc for _, doc in ranking[:keep]]

    assert _vector_leg_docs(conn, embedder, vector_sim_floor=floor) == expected
