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
def test_inner_scan_is_index_shaped_and_outer_resort_is_total() -> None:
    """Inner: the HNSW-servable shape. Outer: a total re-sort no index can serve.

    ``relaxed_order`` may return the inner rows slightly out of order; the outer
    ``dist + 0, id`` restores exact, deterministic order.
    """
    sql, _ = vector_leg_sql(
        predicate=build_predicate(),
        query_embedding=[0.1, 0.2],
        vector_sim_floor=0.25,
        limit=CANDIDATE_LIMIT,
    )
    inner, outer = sql.split("FROM nn", 1)
    assert "AS MATERIALIZED" in inner
    inner_order = inner.split("ORDER BY", 1)[1].split("LIMIT", 1)[0].strip()
    assert inner_order == "c.embedding <=> %s::vector"
    assert outer.split("ORDER BY", 1)[1].strip() == "dist + 0, id"


def test_limit_is_the_argument() -> None:
    for limit in (1, 7, CANDIDATE_LIMIT):
        sql, _ = vector_leg_sql(
            predicate=build_predicate(),
            query_embedding=[0.1],
            vector_sim_floor=0.0,
            limit=limit,
        )
        assert sql.count(f"LIMIT {limit}\n") == 1


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
    assert params == [emb, emb, 0.3, emb]
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
    assert params == [emb, *predicate.where_params, emb, 0.3, emb]
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
    assert params[-2] == 0.31415


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


# ---------------------------------------------------------------------------
# Zero-norm embeddings: cosine is NaN and must never pass the floor
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("floor", [-1.0, 0.0, 0.25])
def test_zero_norm_chunk_never_enters_the_vector_leg(
    test_db: psycopg.Connection[Any], fake_embedder: FakeEmbedder, floor: float
) -> None:
    """A zero vector's cosine distance is NaN; it must be excluded, not ranked #1.

    Postgres orders NaN above every number and evaluates ``NaN >= x`` as TRUE,
    so a floor written as ``1 - (e <=> q) >= floor`` admits the row and
    ``score DESC`` puts it first. The HNSW path never returned it (pgvector does
    not index zero vectors for cosine), so the exact scan must exclude it
    explicitly. Prod held 0 such chunks on 2026-10-13; this guards the latent
    case: an embedder that emits all zeros, or a hand-edited row.
    """
    ids = []
    for i in range(3):
        result = ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=ExtractedDoc(
                title=f"Synthetic zero-norm probe {i}",
                content=f"synthetic zero norm probe body {i}",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:zero-norm-{i}",
            tags=[],
        )
        assert result.document_id is not None
        ids.append(result.document_id)
    zero_doc = ids[0]
    test_db.execute(
        "UPDATE chunks SET embedding = array_fill(0, ARRAY[%s])::vector "
        "WHERE document_id = %s",
        [fake_embedder.dim, zero_doc],
    )
    # Precondition: the row really is zero-norm (cosine distance NaN).
    nan_rows = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id = %s "
        "AND (embedding <=> embedding) = 'NaN'::float8",
        [zero_doc],
    ).fetchone()
    assert nan_rows is not None and nan_rows[0] >= 1

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query=_NO_FTS_QUERY,
        limit=10,
        vector_sim_floor=floor,
        explain=True,
    )
    assert zero_doc not in [r.document_id for r in results]
    assert all(
        r.explain is None
        or r.explain.vector_cosine is None
        or not math.isnan(r.explain.vector_cosine)
        for r in results
    )


# ---------------------------------------------------------------------------
# SearchDiagnostics.vector_ms — the leg's own time (the revisit trigger's input)
# ---------------------------------------------------------------------------
def test_vector_ms_reports_the_leg_and_is_part_of_sql_ms(
    test_db: psycopg.Connection[Any], fake_embedder: FakeEmbedder
) -> None:
    """``vector_ms`` is measured around the leg alone and is a sub-phase of ``sql_ms``."""
    from brain.search import SearchDiagnostics

    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Synthetic timing probe",
            content="synthetic timing probe body",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id="manual:timing-probe",
        tags=[],
    )
    diag = SearchDiagnostics()
    hybrid_search(
        test_db,
        embedder=fake_embedder,
        query=_NO_FTS_QUERY,
        limit=5,
        vector_sim_floor=-1.0,
        diagnostics=diag,
    )
    assert diag.vector_ms is not None and diag.vector_ms > 0.0
    assert diag.sql_ms is not None and diag.vector_ms <= diag.sql_ms


def test_vector_ms_stays_none_when_the_leg_does_not_run(
    test_db: psycopg.Connection[Any], fake_embedder: FakeEmbedder
) -> None:
    """Under ``fts_only`` there is no vector leg; ``None`` is not ``0`` (no fake free leg)."""
    from brain.search import SearchDiagnostics

    diag = SearchDiagnostics()
    hybrid_search(
        test_db, embedder=fake_embedder, query="anything", limit=5, fts_only=True,
        diagnostics=diag,
    )
    assert diag.vector_ms is None


# ---------------------------------------------------------------------------
# The floor's boundary is inclusive: a chunk exactly AT the floor is a candidate
# ---------------------------------------------------------------------------
def _floor_hitting_distance_exactly(distance: float) -> float | None:
    """A float ``f`` with ``1.0 - f == distance`` exactly, or ``None`` if none exists.

    The leg evaluates ``(e <=> q) <= 1 - %s`` in float8, and Python floats are
    the same IEEE-754 doubles, so the search is over the representable
    neighbours of ``1 - distance``. The caller re-verifies the equality in
    Postgres; that is the check that counts.
    """
    candidate = 1.0 - distance
    for _ in range(64):
        if 1.0 - candidate == distance:
            return candidate
        candidate = math.nextafter(
            candidate, math.inf if 1.0 - candidate > distance else -math.inf
        )
    return None


def test_floor_boundary_is_inclusive(
    test_db: psycopg.Connection[Any], fake_embedder: FakeEmbedder
) -> None:
    """The k-th chunk sits EXACTLY on the floor and is in; the (k+1)-th is out.

    ``<=`` versus ``<`` is otherwise only visible in the golden SQL pin, which a
    later author would re-capture. The threshold is read from Postgres using the
    leg's own expression (``c.embedding <=> q``), so no Python cosine
    approximation is involved.
    """
    for i in range(30):
        ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=ExtractedDoc(
                title=f"Synthetic floor-boundary doc {i:02d}",
                content=f"synthetic floor boundary body {i:02d}",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:floor-boundary-{i:02d}",
            tags=[],
        )
    q = fake_embedder.embed([_NO_FTS_QUERY], input_type="query")[0]
    ranked = test_db.execute(
        "SELECT c.document_id::text, c.embedding <=> %s::vector AS dist "
        "FROM chunks c ORDER BY dist, c.id",
        [q],
    ).fetchall()

    # The first k >= 20 whose distance has an exact float floor, and whose
    # successor is strictly farther (so "the (k+1)-th is out" is well defined).
    chosen: tuple[int, float] | None = None
    for k in range(20, len(ranked)):
        dist_k, dist_next = ranked[k - 1][1], ranked[k][1]
        floor = _floor_hitting_distance_exactly(dist_k)
        if floor is not None and dist_next > dist_k:
            chosen = (k, floor)
            break
    assert chosen is not None, "no rank in 20..29 has an exactly representable floor"
    k, floor = chosen

    # The equality the test depends on, checked where it is evaluated: in float8.
    exact = test_db.execute(
        "SELECT (1 - %s::float8) = %s::float8, (1 - %s::float8) < %s::float8",
        [floor, ranked[k - 1][1], floor, ranked[k][1]],
    ).fetchone()
    assert exact == (True, True), exact

    got = _vector_leg_docs(test_db, fake_embedder, vector_sim_floor=floor)
    assert ranked[k - 1][0] in got  # exactly on the floor: included
    assert ranked[k][0] not in got  # just past it: excluded
    assert got == [doc for doc, _ in ranked[:k]]


# ---------------------------------------------------------------------------
# Scan settings are scoped to the leg's query and never leak to the caller
# ---------------------------------------------------------------------------
_SETTING_NAMES = ("hnsw.ef_search", "hnsw.iterative_scan", "enable_seqscan")


def _settings(conn: psycopg.Connection[Any]) -> tuple[str | None, ...]:
    row = conn.execute(
        "SELECT current_setting('hnsw.ef_search', true), "
        "current_setting('hnsw.iterative_scan', true), "
        "current_setting('enable_seqscan')"
    ).fetchone()
    assert row is not None
    return tuple(row)


def _one_doc(conn: psycopg.Connection[Any], embedder: FakeEmbedder) -> None:
    ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title="Synthetic settings probe",
            content="synthetic settings probe body",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id="manual:settings-probe",
        tags=[],
    )


def test_settings_do_not_leak_from_an_autocommit_search(
    test_db: psycopg.Connection[Any], fake_embedder: FakeEmbedder
) -> None:
    _one_doc(test_db, fake_embedder)
    hybrid_search(test_db, embedder=fake_embedder, query=_NO_FTS_QUERY, limit=5)
    before = _settings(test_db)
    hybrid_search(test_db, embedder=fake_embedder, query=_NO_FTS_QUERY, limit=5)
    assert _settings(test_db) == before
    assert before[1] == "off"  # pgvector's default; the leg's relaxed_order is gone


def test_settings_do_not_leak_inside_the_callers_transaction(
    test_db: psycopg.Connection[Any], fake_embedder: FakeEmbedder
) -> None:
    """Inside a caller's transaction the leg runs in a savepoint, and a released
    savepoint keeps local settings, so the explicit restore is what matters.
    The caller's own local values must survive untouched."""
    _one_doc(test_db, fake_embedder)
    with test_db.transaction():
        test_db.execute("SET LOCAL hnsw.ef_search = 77")
        test_db.execute("SET LOCAL hnsw.iterative_scan = strict_order")
        before = _settings(test_db)
        hybrid_search(test_db, embedder=fake_embedder, query=_NO_FTS_QUERY, limit=5)
        assert _settings(test_db) == before
        assert before[:2] == ("77", "strict_order")


def test_settings_restore_on_a_fresh_session_before_pgvector_is_loaded(
    fake_embedder: FakeEmbedder, test_db: psycopg.Connection[Any]
) -> None:
    """A brand-new session has no ``hnsw.*`` variables until pgvector loads; the
    restore must then fall back to the session defaults, not to NULL."""
    _one_doc(test_db, fake_embedder)
    from tests.conftest import TEST_DATABASE_URL

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as fresh, fresh.transaction():
        hybrid_search(fresh, embedder=fake_embedder, query=_NO_FTS_QUERY, limit=5)
        assert _settings(fresh) == ("40", "off", "on")


@pytest.mark.fresh_schema
def test_iterative_scan_fills_a_filtered_pool_beyond_ef_search(
    hnsw_corpus: tuple[psycopg.Connection[Any], FakeEmbedder],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``ef_search`` at pgvector's default 40, the iterative scan still fills it.

    On a 90-row table ``ef_search = 1000`` alone already covers every row, so the
    other tests cannot tell whether ``iterative_scan`` is on. On the live corpus
    (21k chunks) it is what stops a filter from starving the pool. Lowering
    ``ef_search`` here recreates that regime: without the iterative scan only the
    tagged fraction of 40 candidates (about 13) survives the filter.
    """
    import brain.search_vector_leg as leg

    monkeypatch.setattr(
        leg,
        "_SCAN_SETTINGS",
        (("hnsw.ef_search", "40"), *leg._SCAN_SETTINGS[1:]),
    )
    conn, embedder = hnsw_corpus
    expected = [doc for _, doc in _oracle_ranking(conn, embedder, tag=_KEEP_TAG)]

    got = _vector_leg_docs(conn, embedder, vector_sim_floor=-1.0, tag=_KEEP_TAG)
    assert got == expected
