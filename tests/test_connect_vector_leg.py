"""``connect``'s embedding-affinity KNN against a real HNSW index: near-exact, scoped.

Regression tests for ``docs/specs/2026-10-15-related-connect-vector-leg-design.md``
§2.2. ``_embedding_affinity_knn`` argued its result was EXACT because
``strict_order`` returns the ``LIMIT k`` rows in exact distance order. Order is not
membership: HNSW is approximate at any ``ef_search``, and at pgvector's default of
40 the shipped loop matched the exhaustive query for only 106 of 200 live source
documents. The tests in ``tests/test_connect.py`` could not see that, because the
test database's 4096-dim column has no HNSW index, so every query there is exact.

These tests rebuild the production shape (1024 dims + the real HNSW index, see
:mod:`tests.hnsw_fixtures`) on high-entropy synthetic vectors, the hard case for
an HNSW graph, and compare with an oracle computed in Python. With 800 chunks the
leg's ``ef_search = 1000`` reaches the whole graph, so on THIS fixture the result
must equal the oracle exactly. On the live corpus it is near-exact (199 / 200).
"""
from __future__ import annotations

import json
import math
import random
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

import brain.connect as connect_mod
from brain.hnsw_scan import STRICT_SCAN_SETTINGS
from tests.conftest import FakeEmbedder
from tests.hnsw_fixtures import (
    HNSW_DIM,
    SettingsProbe,
    apply_planner_pressure,
    build_hnsw_index,
    current_scan_settings,
    insert_ineligible_twins,
    plan_text,
    use_hnsw_dimension,
    vector_literal,
)

_DOCS = 200
_CHUNKS_PER_DOC = 4
_SOURCES = 20
# The production default (``DEFAULT_CONNECT_CANDIDATE_LIMIT``), so the first KNN
# batch is the production k of 400.
_CANDIDATE_LIMIT = 50
# Gaussian vectors have cosines near 0, so the production floor of 0.25 would admit
# nothing. -1.0 admits every chunk and keeps the comparison about ranking alone.
_FLOOR = -1.0
_SEED = 1234


def _gaussian_literal(rng: random.Random) -> str:
    """A seeded high-entropy vector: the hard case for an HNSW graph."""
    return vector_literal([rng.gauss(0, 1) for _ in range(HNSW_DIM)])


def _seed(conn: psycopg.Connection[Any]) -> tuple[list[str], FakeEmbedder]:
    """``_DOCS`` vault documents × ``_CHUNKS_PER_DOC`` seeded Gaussian chunks."""
    embedder = use_hnsw_dimension(conn)
    rng = random.Random(_SEED)
    ids: list[str] = []
    for i in range(_DOCS):
        row = conn.execute(
            "INSERT INTO documents "
            "(title, content, content_hash, content_type, vault_path, draft, kind) "
            "VALUES (%s, %s, %s, 'note', %s, FALSE, 'vault') RETURNING id::text",
            (f"Synthetic affinity {i:03d}", f"body {i:03d}", f"hash-cv-{i}", f"cv/{i:03d}.md"),
        ).fetchone()
        assert row is not None
        ids.append(str(row[0]))
        chunks = [
            (row[0], j, f"chunk {i:03d}.{j}", _gaussian_literal(rng))
            for j in range(_CHUNKS_PER_DOC)
        ]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
                "VALUES (%s::uuid, %s, %s, %s::vector)",
                chunks,
            )
    build_hnsw_index(conn, embedder)
    return ids, embedder


def _oracle(conn: psycopg.Connection[Any], source: str) -> list[tuple[str, float]]:
    """Per-doc best cosine to the source's average embedding, computed in Python."""
    rows = conn.execute(
        "SELECT c.document_id::text, c.embedding::text FROM chunks c "
        "JOIN documents d ON d.id = c.document_id "
        "WHERE d.draft = FALSE AND d.vault_path IS NOT NULL"
    ).fetchall()
    by_doc: dict[str, list[list[float]]] = {}
    for doc_id, emb in rows:
        by_doc.setdefault(doc_id, []).append(json.loads(emb))
    # avg() in pgvector is float4; recompute it from the server so the query
    # vector is the one production uses, not a float64 re-derivation.
    avg_row = conn.execute(
        "SELECT avg(embedding)::text FROM chunks WHERE document_id = %s::uuid", (source,)
    ).fetchone()
    assert avg_row is not None
    q = json.loads(avg_row[0])
    q_norm = math.sqrt(sum(x * x for x in q))
    best: dict[str, float] = {}
    for doc_id, vectors in by_doc.items():
        if doc_id == source:
            continue
        best[doc_id] = max(
            sum(a * b for a, b in zip(q, v, strict=True))
            / (q_norm * math.sqrt(sum(x * x for x in v)))
            for v in vectors
        )
    ranked = sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))
    return [kv for kv in ranked if kv[1] >= _FLOOR][:_CANDIDATE_LIMIT]


@pytest.fixture
def affinity_corpus(
    test_db: psycopg.Connection[Any],
) -> Iterator[tuple[psycopg.Connection[Any], list[str]]]:
    """The seeded corpus, under the planner pressure that makes it plan like prod."""
    ids, _ = _seed(test_db)
    apply_planner_pressure(test_db)
    plan = plan_text(
        test_db,
        connect_mod._KNN_SQL,
        {"vec": vector_literal([0.1] * HNSW_DIM), "source": ids[0], "k": 400},
    )
    assert "chunks_embedding_idx" in plan, plan  # precondition: prod-like planning
    yield test_db, ids


def _affinity(conn: Any, source: str) -> dict[str, float]:
    return connect_mod.embedding_affinity(
        conn,
        source_doc_id=source,
        candidate_limit=_CANDIDATE_LIMIT,
        vector_sim_floor=_FLOOR,
    )


@pytest.mark.fresh_schema
def test_embedding_affinity_matches_the_oracle_on_an_hnsw_index(
    affinity_corpus: tuple[psycopg.Connection[Any], list[str]],
) -> None:
    """Same documents, same order, same best cosine, for every source.

    The shipped loop (default ``ef_search`` 40) disagreed for 18 of these 20.
    Scores compare to 1e-6: pgvector computes cosine in single precision.
    """
    conn, ids = affinity_corpus
    mismatched = []
    for source in ids[:_SOURCES]:
        expected = _oracle(conn, source)
        got = list(_affinity(conn, source).items())
        if [d for d, _ in got] != [d for d, _ in expected] or any(
            abs(g - e) > 1e-6 for (_, g), (_, e) in zip(got, expected, strict=True)
        ):
            mismatched.append(source)
    assert mismatched == []


def test_knn_query_runs_under_strict_design_d_settings(
    test_db: psycopg.Connection[Any], fake_embedder: FakeEmbedder
) -> None:
    """ef_search 1000, ``strict_order``, seqscan off: live at the KNN's own query.

    ``strict_order`` and not ``relaxed_order``: the loop's early stop ("the
    smallest returned cosine is already below the floor") and its per-doc-max
    argument both need the rows in distance order.
    """
    ids = [
        _make_doc(test_db, fake_embedder, i) for i in range(3)
    ]
    probe = SettingsProbe(test_db, marker="ORDER BY c.embedding <=> %(vec)s::vector")
    assert len(_affinity(probe, ids[0])) == 2
    assert probe.seen and set(probe.seen) == {("1000", "strict_order", "off")}
    assert dict(STRICT_SCAN_SETTINGS)["hnsw.iterative_scan"] == "strict_order"


def test_settings_do_not_leak_from_embedding_affinity(
    test_db: psycopg.Connection[Any], fake_embedder: FakeEmbedder
) -> None:
    ids = [_make_doc(test_db, fake_embedder, i) for i in range(3)]
    _affinity(test_db, ids[0])
    assert current_scan_settings(test_db) == ("40", "off", "on")
    with test_db.transaction():
        test_db.execute("SET LOCAL hnsw.ef_search = 77")
        _affinity(test_db, ids[0])
        assert current_scan_settings(test_db) == ("77", "off", "on")
    assert current_scan_settings(test_db) == ("40", "off", "on")


def _make_doc(conn: psycopg.Connection[Any], embedder: FakeEmbedder, i: int) -> str:
    """One vault document with one FakeEmbedder chunk (the default 4096-dim schema)."""
    row = conn.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, vault_path, draft, kind) "
        "VALUES (%s, %s, %s, 'note', %s, FALSE, 'vault') RETURNING id::text",
        (f"Synthetic probe {i}", f"body {i}", f"hash-cp-{i}", f"cp/{i}.md"),
    ).fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
        "VALUES (%s::uuid, 0, %s, %s::vector)",
        (row[0], f"chunk {i}", vector_literal(embedder.embed([f"synthetic probe {i}"])[0])),
    )
    return str(row[0])


@pytest.mark.fresh_schema
def test_an_under_filled_scan_falls_back_to_the_exhaustive_query(
    affinity_corpus: tuple[psycopg.Connection[Any], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fewer than k rows is NOT proof that every eligible chunk was seen.

    The iterative scan also stops at ``hnsw.max_scan_tuples``. Here
    ``ef_search`` and the cap are both lowered to 1, so the first batch of
    k = 400 comes back short (91 rows on this fixture) while 800 eligible chunks
    exist. Treating that under-fill as "complete" ranks only the chunks the
    capped walk happened to reach. The loop must concede to the exhaustive
    query instead, which matches the oracle exactly.

    The cap is approximate: at ``ef_search`` 40 with a cap of 100, pgvector
    still returned all 400 rows, so a red there measured ef-40 approximation,
    not an under-fill. That is why the precondition is asserted first.
    """
    monkeypatch.setattr(
        connect_mod,
        "STRICT_SCAN_SETTINGS",
        (
            ("hnsw.ef_search", "1"),
            ("hnsw.iterative_scan", "strict_order"),
            ("enable_seqscan", "off"),
            ("hnsw.max_scan_tuples", "1"),
        ),
    )
    conn, ids = affinity_corpus
    sources = ids[:3]
    k = _CANDIDATE_LIMIT * connect_mod._KNN_CANDIDATE_MULTIPLIER
    for source in sources:  # precondition: the capped KNN really comes back short
        assert 0 < _capped_knn_row_count(conn, source, k) < k
    probe = SettingsProbe(conn, marker="ORDER BY c.embedding <=> %(vec)s::vector")
    for source in sources:
        got = list(_affinity(probe, source).items())
        expected = _oracle(conn, source)
        assert [d for d, _ in got] == [d for d, _ in expected]
        assert all(abs(g - e) <= 1e-6 for (_, g), (_, e) in zip(got, expected, strict=True))
    assert probe.seen and set(probe.seen) == {("1", "strict_order", "off")}


def _capped_knn_row_count(conn: psycopg.Connection[Any], source: str, k: int) -> int:
    """Rows the KNN query returns for ``source`` under the patched, capped settings."""
    from brain.hnsw_scan import scoped_settings

    src = conn.execute(
        "SELECT avg(embedding)::text FROM chunks WHERE document_id = %s::uuid", (source,)
    ).fetchone()
    assert src is not None
    with scoped_settings(conn, connect_mod.STRICT_SCAN_SETTINGS):
        rows = conn.execute(
            connect_mod._KNN_SQL,
            {"vec": src[0], "source": source, "k": k},
        ).fetchall()
    return len(rows)


@pytest.mark.fresh_schema
def test_ineligible_twins_of_the_source_never_score(
    affinity_corpus: tuple[psycopg.Connection[Any], list[str]],
) -> None:
    """A draft doc and a vault-less doc whose chunk EQUALS the source's average.

    Each would score cosine 1.0 and rank first, so dropping either eligibility
    predicate from the KNN query surfaces it deterministically. The KNN path
    really runs here (800 eligible chunks fill k = 400; no under-fill), and the
    result must still be the oracle's top 50 over eligible documents only.
    """
    conn, ids = affinity_corpus
    source = ids[0]
    avg = conn.execute(
        "SELECT avg(embedding)::text FROM chunks WHERE document_id = %s::uuid", (source,)
    ).fetchone()
    assert avg is not None
    twins = insert_ineligible_twins(conn, avg[0])

    got = _affinity(conn, source)
    assert twins["draft"] not in got
    assert twins["vaultless"] not in got
    assert len(got) == _CANDIDATE_LIMIT
    assert list(got) == [doc for doc, _ in _oracle(conn, source)]


@pytest.mark.parametrize("floor", [-1.0, 0.0, 0.25])
def test_a_zero_norm_chunk_never_scores_on_the_exhaustive_path(
    test_db: psycopg.Connection[Any], fake_embedder: FakeEmbedder, floor: float
) -> None:
    """A zero vector's cosine is NaN; it must never take a candidate slot.

    Five documents on the default 4096-dim schema: k = 400 can never fill, so
    every call concedes to the exhaustive query (the path every corpus under 400
    eligible chunks takes). That query floored with ``1 - dist >= floor``, and
    ``1 - NaN >= x`` is TRUE in Postgres, so the zero-norm doc came back with
    cosine NaN and sorted FIRST under ``ORDER BY cosine DESC``. ``dist <= 1 -
    floor`` is FALSE for NaN.
    """
    ids = [_make_doc(test_db, fake_embedder, i) for i in range(5)]
    zero = ids[4]
    test_db.execute(
        "UPDATE chunks SET embedding = array_fill(0, ARRAY[%s])::vector "
        "WHERE document_id = %s::uuid",
        [fake_embedder.dim, zero],
    )
    got = _affinity_with_floor(test_db, ids[0], floor)
    assert zero not in got
    assert not any(math.isnan(score) for score in got.values())
    if floor == -1.0:
        assert set(got) == set(ids[1:4])  # every real cosine passes -1


def _affinity_with_floor(
    conn: psycopg.Connection[Any], source: str, floor: float
) -> dict[str, float]:
    return connect_mod.embedding_affinity(
        conn, source_doc_id=source, candidate_limit=_CANDIDATE_LIMIT, vector_sim_floor=floor
    )
