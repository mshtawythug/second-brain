"""Related-docs vector leg: a full, F6-gated candidate pool from a scoped HNSW scan.

Regression tests for ``docs/specs/2026-10-15-related-connect-vector-leg-design.md``
§2.1. The leg used to put every filter, the F6 confidential predicate and the
cosine floor in one ``WHERE`` under ``ORDER BY embedding <=> q LIMIT 50`` with
pgvector's default settings. The HNSW index serves that query, hands over at most
``hnsw.ef_search`` (40) rows, and the filters apply afterwards. Measured on the
live corpus: 25-39 candidates where 50 exist, recall 0.69. The leg now uses design
D, the same shape as :mod:`brain.search_vector_leg`.

The DB tests rebuild the production shape (1024 dims + the real HNSW index, see
:mod:`tests.hnsw_fixtures`) and assert, as a precondition, that the planner serves
the old query shape from the index. Expected sets come from an independent oracle:
cosine computed in Python over the stored embeddings.

There is no "the leg's own settings choose the index" test here, unlike the search
leg's: on 91 rows the planner serves this join through ``documents`` and an exact
sort even with ``enable_seqscan = off``, which is correct, just not the index. On
the live corpus the leg's plan uses the index for 20 of 20 sampled sources (spec
§2.1).
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from brain.related_candidates import vector_candidates, vector_candidates_sql
from brain.search import CANDIDATE_LIMIT
from brain.sensitivity import CONFIDENTIAL
from tests.conftest import FakeEmbedder
from tests.hnsw_fixtures import (
    SettingsProbe,
    apply_planner_pressure,
    build_hnsw_index,
    current_scan_settings,
    floor_hitting_distance_exactly,
    index_scan_nodes,
    plan_text,
    use_hnsw_dimension,
    vector_literal,
)

# One source plus enough candidates that the true pool (CANDIDATE_LIMIT, 50) is
# strictly larger than what an ef_search=40 index scan can return. At the leg's
# ef_search=1000 the scan reaches every row, so on THIS fixture the leg's pool
# equals the oracle's exact top 50.
_CANDIDATES = 90
# The old leg's SQL, verbatim from related.py before this change: the
# precondition proves the planner serves THIS shape from the index.
_OLD_SQL = """
    SELECT c.id::text, c.document_id::text, c.content
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE d.draft = FALSE
      AND d.vault_path IS NOT NULL
      AND d.id <> %s::uuid
      AND c.embedding IS NOT NULL
      AND 1 - (c.embedding <=> %s::vector) >= %s
    ORDER BY c.embedding <=> %s::vector
    LIMIT 50
"""


# ---------------------------------------------------------------------------
# SQL builder: unit tests (no database)
# ---------------------------------------------------------------------------
def _sql(*, exclude_confidential: bool = True, floor: float = 0.25) -> tuple[str, list[Any]]:
    return vector_candidates_sql(
        src_embedding="[0.1,0.2]",
        exclude_doc_id="00000000-0000-0000-0000-000000000000",
        vector_sim_floor=floor,
        exclude_confidential=exclude_confidential,
    )


def test_sql_shape() -> None:
    """Inner: index-shaped, every filter. Outer: the floor and a total re-sort."""
    sql, _ = _sql()
    inner, outer = sql.split("FROM nn", 1)
    assert "AS MATERIALIZED" in inner
    inner_order = inner.split("ORDER BY", 1)[1].split("LIMIT", 1)[0].strip()
    assert inner_order == "c.embedding <=> %s::vector"
    assert f"LIMIT {CANDIDATE_LIMIT}\n" in inner
    # The F6 gate is INSIDE, so a confidential chunk never uses up a slot.
    assert "sensitivity" in inner and "sensitivity" not in outer
    # The floor is OUTSIDE: inside, an off-corpus source walks the whole index.
    assert "1 - %s" not in inner
    assert outer.split("ORDER BY", 1)[0].strip() == "WHERE dist <= 1 - %s"
    assert outer.split("ORDER BY", 1)[1].strip() == "dist + 0, id"


def test_sql_without_the_gate_has_no_sensitivity_predicate() -> None:
    sql, _ = _sql(exclude_confidential=False)
    assert "sensitivity" not in sql


def test_every_value_is_bound_never_interpolated() -> None:
    sql, params = _sql(floor=0.31415)
    assert "0.31415" not in sql and "[0.1,0.2]" not in sql
    assert params == [
        "[0.1,0.2]",
        "00000000-0000-0000-0000-000000000000",
        "[0.1,0.2]",
        0.31415,
    ]
    assert sql.count("%s") == len(params)


def test_no_source_embedding_means_no_candidates(test_db: psycopg.Connection[Any]) -> None:
    assert (
        vector_candidates(
            test_db,
            src_embedding=None,
            exclude_doc_id="00000000-0000-0000-0000-000000000000",
            vector_sim_floor=0.25,
            exclude_confidential=True,
        )
        == []
    )


# ---------------------------------------------------------------------------
# Real database with a real HNSW index
# ---------------------------------------------------------------------------
def _insert_doc(
    conn: psycopg.Connection[Any], embedder: FakeEmbedder, i: int, *, draft: bool = False
) -> str:
    row = conn.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, vault_path, draft, kind) "
        "VALUES (%s, %s, %s, 'note', %s, %s, 'vault') RETURNING id::text",
        (f"Synthetic related {i:03d}", f"body {i:03d}", f"hash-rv-{i}", f"rv/{i:03d}.md", draft),
    ).fetchone()
    assert row is not None
    vec = embedder.embed([f"synthetic related chunk {i:03d}"])[0]
    conn.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
        "VALUES (%s::uuid, 0, %s, %s::vector)",
        (row[0], f"synthetic related chunk {i:03d}", vector_literal(vec)),
    )
    return str(row[0])


def _seed(conn: psycopg.Connection[Any]) -> tuple[str, list[str], FakeEmbedder]:
    """A source document plus ``_CANDIDATES`` one-chunk candidates, indexed."""
    embedder = use_hnsw_dimension(conn)
    source = _insert_doc(conn, embedder, 999)
    others = [_insert_doc(conn, embedder, i) for i in range(_CANDIDATES)]
    build_hnsw_index(conn, embedder)
    return source, others, embedder


def _src_embedding(conn: psycopg.Connection[Any], source: str) -> str:
    row = conn.execute(
        "SELECT avg(embedding)::text FROM chunks WHERE document_id = %s::uuid", (source,)
    ).fetchone()
    assert row is not None
    return str(row[0])


@pytest.fixture
def related_corpus(
    test_db: psycopg.Connection[Any],
) -> Iterator[tuple[psycopg.Connection[Any], str, list[str]]]:
    """The seeded corpus, under the planner pressure that makes it plan like prod."""
    source, others, _ = _seed(test_db)
    apply_planner_pressure(test_db)
    src = _src_embedding(test_db, source)
    plan = plan_text(test_db, _OLD_SQL, (source, src, -1.0, src))
    assert "chunks_embedding_idx" in plan, plan  # precondition: prod-like planning
    yield test_db, source, others


def _mark_confidential(conn: psycopg.Connection[Any], doc_ids: list[str]) -> None:
    conn.execute(
        "UPDATE documents SET sensitivity = %s WHERE id = ANY(%s::uuid[])",
        (CONFIDENTIAL, doc_ids),
    )


def _oracle(
    conn: psycopg.Connection[Any], source: str, *, exclude_confidential: bool
) -> list[tuple[float, str]]:
    """``(cosine, chunk_id)`` for every eligible chunk, best first, computed in Python."""
    q = json.loads(_src_embedding(conn, source))
    q_norm = math.sqrt(sum(x * x for x in q))
    rows = conn.execute(
        "SELECT c.id::text, c.embedding::text, d.sensitivity FROM chunks c "
        "JOIN documents d ON d.id = c.document_id "
        "WHERE d.id <> %s::uuid AND d.draft = FALSE AND d.vault_path IS NOT NULL",
        (source,),
    ).fetchall()
    ranked: list[tuple[float, str]] = []
    for chunk_id, emb_text, sensitivity in rows:
        if exclude_confidential and sensitivity == CONFIDENTIAL:
            continue
        vec = json.loads(emb_text)
        dot = sum(a * b for a, b in zip(q, vec, strict=True))
        ranked.append((dot / (q_norm * math.sqrt(sum(x * x for x in vec))), chunk_id))
    ranked.sort(key=lambda r: (-r[0], r[1]))
    return ranked


def _leg(
    conn: Any, source: str, *, floor: float = -1.0, exclude_confidential: bool = True
) -> list[str]:
    rows = vector_candidates(
        conn,
        src_embedding=_src_embedding(conn, source),
        exclude_doc_id=source,
        vector_sim_floor=floor,
        exclude_confidential=exclude_confidential,
    )
    return [chunk_id for chunk_id, _, _ in rows]


@pytest.mark.fresh_schema
def test_vector_leg_fills_the_pool_in_oracle_order(
    related_corpus: tuple[psycopg.Connection[Any], str, list[str]],
) -> None:
    """A full ``CANDIDATE_LIMIT`` pool, not ef_search's 40, in the oracle's order."""
    conn, source, _ = related_corpus
    expected = [cid for _, cid in _oracle(conn, source, exclude_confidential=True)]
    expected = expected[:CANDIDATE_LIMIT]
    assert len(expected) == CANDIDATE_LIMIT  # the pool is genuinely larger than 40
    assert _leg(conn, source) == expected


@pytest.mark.fresh_schema
def test_confidential_chunks_never_use_up_a_slot(
    related_corpus: tuple[psycopg.Connection[Any], str, list[str]],
) -> None:
    """The F6 gate filters BEFORE the LIMIT counts.

    Every other candidate is confidential, leaving 45 eligible, fewer than the
    pool. A correct leg returns all 45 and no confidential chunk. Gating after a
    40-row index scan keeps about 20.
    """
    conn, source, others = related_corpus
    confidential = others[::2]
    _mark_confidential(conn, confidential)
    expected = [cid for _, cid in _oracle(conn, source, exclude_confidential=True)]
    assert len(expected) == _CANDIDATES // 2

    got = _leg(conn, source)
    assert got == expected
    confidential_chunks = {
        str(r[0])
        for r in conn.execute(
            "SELECT id FROM chunks WHERE document_id = ANY(%s::uuid[])", (confidential,)
        ).fetchall()
    }
    assert not confidential_chunks & set(got)


@pytest.mark.fresh_schema
def test_include_confidential_returns_the_ungated_pool(
    related_corpus: tuple[psycopg.Connection[Any], str, list[str]],
) -> None:
    """The permissive direction: the same fixture with the gate opened.

    This is what keeps the gating test honest: if the gate were always on, the
    test above would still pass, and this one would not.
    """
    conn, source, others = related_corpus
    _mark_confidential(conn, others[::2])
    expected = [cid for _, cid in _oracle(conn, source, exclude_confidential=False)]
    assert _leg(conn, source, exclude_confidential=False) == expected[:CANDIDATE_LIMIT]


@pytest.mark.fresh_schema
def test_iterative_scan_fills_a_gated_pool_beyond_ef_search(
    related_corpus: tuple[psycopg.Connection[Any], str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``ef_search`` at the default 40, the iterative scan still fills it.

    On 90 rows ``ef_search = 1000`` alone covers every row, so the other tests
    cannot tell whether ``iterative_scan`` is on. On the live corpus (21k chunks)
    it is what stops the F6 gate from starving the pool.
    """
    import brain.related_candidates as candidates

    monkeypatch.setattr(
        candidates,
        "_SCAN_SETTINGS",
        (("hnsw.ef_search", "40"), *candidates._SCAN_SETTINGS[1:]),
    )
    conn, source, others = related_corpus
    _mark_confidential(conn, others[::2])
    expected = [cid for _, cid in _oracle(conn, source, exclude_confidential=True)]
    assert _leg(conn, source) == expected


@pytest.mark.fresh_schema
def test_floor_admits_exactly_the_rows_above_it(
    related_corpus: tuple[psycopg.Connection[Any], str, list[str]],
) -> None:
    conn, source, _ = related_corpus
    ranking = _oracle(conn, source, exclude_confidential=True)
    keep = 20
    # Midway between the 20th and 21st cosines: no float-equality dependence.
    floor = (ranking[keep - 1][0] + ranking[keep][0]) / 2
    assert _leg(conn, source, floor=floor) == [cid for _, cid in ranking[:keep]]


@pytest.mark.fresh_schema
def test_off_corpus_source_reads_at_most_limit_index_rows(
    related_corpus: tuple[psycopg.Connection[Any], str, list[str]],
) -> None:
    """Nothing above the floor: no candidates, and the index walk stays short.

    With the floor inside the CTE the iterative scan treats it as a filter to
    satisfy and walks the whole index (up to ``hnsw.max_scan_tuples``). Outside,
    the index yields ``limit`` rows and the outer query drops them.
    """
    import brain.related_candidates as candidates

    conn, source, _ = related_corpus
    off_corpus_floor = 0.999  # FakeEmbedder vectors never get this close
    assert _leg(conn, source, floor=off_corpus_floor) == []

    sql, params = vector_candidates_sql(
        src_embedding=_src_embedding(conn, source),
        exclude_doc_id=source,
        vector_sim_floor=off_corpus_floor,
        exclude_confidential=True,
    )
    with conn.transaction():
        for name, value in candidates._SCAN_SETTINGS:
            conn.execute("SELECT set_config(%s, %s, true)", [name, value])
        row = conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params).fetchone()
    assert row is not None
    nodes = index_scan_nodes(row[0][0]["Plan"])
    assert len(nodes) == 1, nodes
    assert nodes[0].get("Rows Removed by Filter", 0) == 0, nodes[0]
    # +1: the source's own chunk is its nearest neighbour. The index yields it and
    # the join's ``d.id <> source`` drops it, after the scan. With the floor
    # inside, the walk would read every one of the corpus's chunks instead.
    assert nodes[0]["Actual Rows"] <= CANDIDATE_LIMIT + 1, nodes[0]


def test_leg_query_runs_under_relaxed_design_d_settings(
    test_db: psycopg.Connection[Any], fake_embedder: FakeEmbedder
) -> None:
    """ef_search 1000, relaxed_order, seqscan off: live at the leg's own query."""
    source = _insert_doc(test_db, fake_embedder, 1)
    _insert_doc(test_db, fake_embedder, 2)
    probe = SettingsProbe(test_db, marker="WITH nn AS MATERIALIZED")
    assert len(_leg(probe, source)) == 1
    assert probe.seen == [("1000", "relaxed_order", "off")]


def test_settings_do_not_leak_from_the_leg(
    test_db: psycopg.Connection[Any], fake_embedder: FakeEmbedder
) -> None:
    source = _insert_doc(test_db, fake_embedder, 1)
    _insert_doc(test_db, fake_embedder, 2)
    _leg(test_db, source)
    assert current_scan_settings(test_db) == ("40", "off", "on")


# ---------------------------------------------------------------------------
# Floor semantics: inclusive at the boundary, and a NaN distance never passes
# ---------------------------------------------------------------------------
def test_floor_boundary_is_inclusive(
    test_db: psycopg.Connection[Any], fake_embedder: FakeEmbedder
) -> None:
    """The k-th chunk sits EXACTLY on the floor and is in; the (k+1)-th is out.

    The default 4096-dim schema has no HNSW index, so the plan is exact and the
    boundary is the floor alone. The threshold comes from Postgres, using the
    leg's own expression and the same bound query vector, so no Python cosine
    approximation is involved.
    """
    source = _insert_doc(test_db, fake_embedder, 999)
    for i in range(30):
        _insert_doc(test_db, fake_embedder, i)
    src = _src_embedding(test_db, source)
    ranked = test_db.execute(
        "SELECT c.id::text, c.embedding <=> %s::vector AS dist FROM chunks c "
        "WHERE c.document_id <> %s::uuid ORDER BY dist, c.id",
        (src, source),
    ).fetchall()
    chosen: tuple[int, float] | None = None
    for k in range(10, len(ranked)):
        floor = floor_hitting_distance_exactly(ranked[k - 1][1])
        if floor is not None and ranked[k][1] > ranked[k - 1][1]:
            chosen = (k, floor)
            break
    assert chosen is not None, "no rank >= 10 has an exactly representable floor"
    k, floor = chosen
    exact = test_db.execute(
        "SELECT (1 - %s::float8) = %s::float8, (1 - %s::float8) < %s::float8",
        [floor, ranked[k - 1][1], floor, ranked[k][1]],
    ).fetchone()
    assert exact == (True, True), exact  # precondition, evaluated in float8

    got = _leg(test_db, source, floor=floor)
    assert ranked[k - 1][0] in got  # exactly on the floor: included
    assert ranked[k][0] not in got  # just past it: excluded
    assert got == [cid for cid, _ in ranked[:k]]


@pytest.mark.parametrize("floor", [-1.0, 0.0, 0.25])
def test_zero_norm_chunk_never_passes_the_floor(
    test_db: psycopg.Connection[Any], fake_embedder: FakeEmbedder, floor: float
) -> None:
    """A zero vector's cosine distance is NaN; the floor must exclude it.

    Postgres orders NaN above every number and evaluates ``1 - NaN >= x`` as
    TRUE, so the old floor (``1 - dist >= floor``) admitted such a chunk on any
    plan that reads it. The default 4096-dim schema has no HNSW index, so the
    plan here is exact and does read it. ``NaN <= x`` is FALSE. pgvector does
    not index zero vectors for cosine, so on the HNSW path the row never
    appears at all; this guards the exact path (an embedder emitting zeros, or
    a hand-edited row).
    """
    source = _insert_doc(test_db, fake_embedder, 999)
    normal = _insert_doc(test_db, fake_embedder, 1)
    zero = _insert_doc(test_db, fake_embedder, 2)
    test_db.execute(
        "UPDATE chunks SET embedding = array_fill(0, ARRAY[%s])::vector "
        "WHERE document_id = %s::uuid",
        [fake_embedder.dim, zero],
    )
    nan_rows = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id = %s::uuid "
        "AND (embedding <=> %s::vector) = 'NaN'::float8",
        [zero, _src_embedding(test_db, source)],
    ).fetchone()
    assert nan_rows is not None and nan_rows[0] == 1  # precondition: NaN distance

    rows = vector_candidates(
        test_db,
        src_embedding=_src_embedding(test_db, source),
        exclude_doc_id=source,
        vector_sim_floor=floor,
        exclude_confidential=True,
    )
    doc_ids = [doc_id for _, doc_id, _ in rows]
    assert zero not in doc_ids
    if floor == -1.0:
        assert normal in doc_ids  # every real cosine passes -1: the leg is not empty
