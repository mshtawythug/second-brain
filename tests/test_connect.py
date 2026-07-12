"""Tests for `brain connect` — auto-link suggestions (Plan 07).

Covers the pure scoring helpers, the DB-backed affinity legs + refresh pipeline,
the accept/reject status machine, the ``## See Also`` vault writeback, and the
CLI surface. All fixtures are synthetic (no PII).
"""
from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain import connect as connect_mod
from brain.cli import app
from brain.config import Config
from brain.errors import ConnectError
from brain.ingest import ExtractedDoc, ingest_document

from .conftest import FakeEmbedder

runner = CliRunner()


# --------------------------------------------------------------------------- #
# Seeding helpers.
# --------------------------------------------------------------------------- #


def _make_vault_doc(
    conn: psycopg.Connection,
    embedder: FakeEmbedder,
    *,
    title: str,
    content: str,
    vault_path: str,
) -> str:
    """Ingest a manual doc, give it a vault_path, return its id.

    Eligibility for ``connect`` requires a non-NULL ``vault_path`` + at least
    one embedded chunk; ``ingest_document`` populates chunks via the fake
    embedder, and we set ``vault_path`` explicitly afterward.
    """
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
        source_kind="manual",
        tags=[],
    )
    assert result.document_id is not None
    conn.execute(
        "UPDATE documents SET vault_path = %s WHERE id = %s",
        (vault_path, result.document_id),
    )
    return result.document_id


def _add_entity(
    conn: psycopg.Connection, *, name: str, key: str, etype: str = "topic"
) -> str:
    """Insert a synthetic graph entity (tenant defaults to 'default')."""
    row = conn.execute(
        "INSERT INTO graph_entities (entity_type, name, canonical_key) "
        "VALUES (%s, %s, %s) RETURNING id::text",
        (etype, name, key),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _mention(conn: psycopg.Connection, *, entity_id: str, doc_id: str) -> None:
    """Record that ``doc_id`` mentions ``entity_id``."""
    conn.execute(
        "INSERT INTO graph_entity_mentions (entity_id, document_id, source) "
        "VALUES (%s::uuid, %s::uuid, %s)",
        (entity_id, doc_id, "concepts"),
    )


def _insert_suggestion(
    conn: psycopg.Connection,
    *,
    source: str,
    target: str,
    score: float = 0.70,
    graph: float | None = 0.80,
    embed: float | None = 0.60,
    status: str = "pending",
) -> str:
    """Insert a link_suggestions row directly (deterministic, no scoring)."""
    row = conn.execute(
        "INSERT INTO link_suggestions "
        "(source_doc_id, target_doc_id, score, graph_score, embed_score, status) "
        "VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s) RETURNING id::text",
        (source, target, score, graph, embed, status),
    ).fetchone()
    assert row is not None
    return str(row[0])


@pytest.fixture(autouse=True)
def _pin_connect_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the confidence floor these mechanics fixtures were authored against.

    The shipped default is 0.60 (tuned on a live corpus, 2026-06-10 — see
    config.py); the fixtures here score pairs in the 0.30–0.60 band on
    purpose to exercise upsert/retire/cap mechanics. The default's value
    itself is asserted in test_config.py.
    """
    monkeypatch.setenv("BRAIN_CONNECT_MIN_SCORE", "0.30")


def _cfg() -> Config:
    """Load config (DATABASE_URL forced to the test DB by the session fixture)."""
    return Config.load()


# --------------------------------------------------------------------------- #
# Pure scoring helpers.
# --------------------------------------------------------------------------- #


def test_normalized_overlap_disjoint_is_zero() -> None:
    # 0 shared entities → 0.0 regardless of set sizes.
    assert connect_mod.normalized_overlap(0, 5, 4) == 0.0


def test_normalized_overlap_full_subset_is_one() -> None:
    # One doc's entities ⊂ the other's: shared == min-side count → 1.0.
    assert connect_mod.normalized_overlap(2, 2, 5) == 1.0


def test_normalized_overlap_partial() -> None:
    # 3 shared of a 5-entity min side → 0.6.
    assert connect_mod.normalized_overlap(3, 5, 5) == pytest.approx(0.6)


def test_normalized_overlap_zero_denominator_is_zero() -> None:
    # A side with no entities can never overlap.
    assert connect_mod.normalized_overlap(0, 0, 0) == 0.0


def test_score_doc_pair_both_legs_is_mean() -> None:
    assert connect_mod.score_doc_pair(0.81, 0.63) == pytest.approx(0.72)


def test_score_doc_pair_graph_only_halves() -> None:
    # Embedding leg absent → graph signal counts as half (absent leg = 0).
    assert connect_mod.score_doc_pair(0.6, None) == pytest.approx(0.30)


def test_score_doc_pair_embed_only_halves() -> None:
    assert connect_mod.score_doc_pair(None, 0.5) == pytest.approx(0.25)


def test_score_doc_pair_both_outranks_single_leg() -> None:
    # Corroboration: a both-leg pair outscores an otherwise-equal single-leg one.
    both = connect_mod.score_doc_pair(0.6, 0.6)
    single = connect_mod.score_doc_pair(0.6, None)
    assert both > single


def test_score_doc_pair_neither_leg_is_zero() -> None:
    assert connect_mod.score_doc_pair(None, None) == 0.0


# --------------------------------------------------------------------------- #
# graph_affinity (DB-backed).
# --------------------------------------------------------------------------- #


def test_graph_affinity_no_shared_entities(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="alpha", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="beta", vault_path="n/b.md"
    )
    e1 = _add_entity(test_db, name="Entity One", key="entity-one")
    e2 = _add_entity(test_db, name="Entity Two", key="entity-two")
    _mention(test_db, entity_id=e1, doc_id=a)
    _mention(test_db, entity_id=e2, doc_id=b)

    scores = connect_mod.graph_affinity(
        test_db, source_doc_id=a, tenant_id="default", candidate_limit=50
    )
    assert scores == {}


def test_graph_affinity_full_subset(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="alpha", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="beta", vault_path="n/b.md"
    )
    # A has 1 entity; B has 3 incl. A's → shared 1 / min(1,3) = 1.0.
    shared = _add_entity(test_db, name="Shared", key="shared")
    extra1 = _add_entity(test_db, name="Extra One", key="extra-one")
    extra2 = _add_entity(test_db, name="Extra Two", key="extra-two")
    _mention(test_db, entity_id=shared, doc_id=a)
    for ent in (shared, extra1, extra2):
        _mention(test_db, entity_id=ent, doc_id=b)

    scores = connect_mod.graph_affinity(
        test_db, source_doc_id=a, tenant_id="default", candidate_limit=50
    )
    assert scores == {b: pytest.approx(1.0)}


def test_graph_affinity_partial(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="alpha", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="beta", vault_path="n/b.md"
    )
    ents = [
        _add_entity(test_db, name=f"E{i}", key=f"ent-{i}") for i in range(7)
    ]
    # A: e0..e4 (5). B: e0,e1,e2,e5,e6 (5). Shared 3 of min(5,5) → 0.6.
    for ent in ents[:5]:
        _mention(test_db, entity_id=ent, doc_id=a)
    for ent in (ents[0], ents[1], ents[2], ents[5], ents[6]):
        _mention(test_db, entity_id=ent, doc_id=b)

    scores = connect_mod.graph_affinity(
        test_db, source_doc_id=a, tenant_id="default", candidate_limit=50
    )
    assert scores == {b: pytest.approx(0.6)}


def test_graph_affinity_rejects_bad_limit(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="alpha", vault_path="n/a.md"
    )
    with pytest.raises(ConnectError):
        connect_mod.graph_affinity(
            test_db, source_doc_id=a, tenant_id="default", candidate_limit=0
        )


def test_graph_affinity_tenant_isolation(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # Mentions under a different tenant must not surface for the default tenant.
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="alpha", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="beta", vault_path="n/b.md"
    )
    other = test_db.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
        "VALUES (%s, %s, %s, %s) RETURNING id::text",
        ("other", "topic", "Shared", "shared"),
    ).fetchone()
    assert other is not None
    other_id = str(other[0])
    for doc in (a, b):
        test_db.execute(
            "INSERT INTO graph_entity_mentions (tenant_id, entity_id, document_id, source) "
            "VALUES (%s, %s::uuid, %s::uuid, %s)",
            ("other", other_id, doc, "concepts"),
        )
    scores = connect_mod.graph_affinity(
        test_db, source_doc_id=a, tenant_id="default", candidate_limit=50
    )
    assert scores == {}


# --------------------------------------------------------------------------- #
# embedding_affinity (DB-backed).
# --------------------------------------------------------------------------- #


def test_embedding_affinity_returns_cosine_scores(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # Distinct content (distinct content_hash so the docs don't dedup), then
    # force B's chunk embeddings equal to A's so cosine is a deterministic 1.0.
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="alpha body", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="beta body", vault_path="n/b.md"
    )
    test_db.execute(
        "UPDATE chunks SET embedding = "
        "(SELECT embedding FROM chunks WHERE document_id = %s::uuid LIMIT 1) "
        "WHERE document_id IN (%s::uuid, %s::uuid)",
        (a, a, b),
    )
    scores = connect_mod.embedding_affinity(
        test_db, source_doc_id=a, candidate_limit=50, vector_sim_floor=0.25
    )
    assert b in scores
    assert scores[b] == pytest.approx(1.0, abs=1e-6)


def test_embedding_affinity_empty_when_no_source_embedding(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="alpha", vault_path="n/a.md"
    )
    # Null out the source doc's chunk embeddings → no query vector.
    test_db.execute(
        "UPDATE chunks SET embedding = NULL WHERE document_id = %s", (a,)
    )
    scores = connect_mod.embedding_affinity(
        test_db, source_doc_id=a, candidate_limit=50, vector_sim_floor=0.25
    )
    assert scores == {}


# --------------------------------------------------------------------------- #
# embedding_affinity — Task 4.1 HNSW-KNN rewrite: equivalence + adaptive re-query.
#
# The rewrite must return byte-identical results to the pre-4.1 exhaustive
# ``MAX(...) GROUP BY`` query. We reproduce that OLD query INLINE (below) as an
# independent oracle — deliberately NOT calling the production
# ``_embedding_affinity_exhaustive`` fallback, which would make the check
# circular.
# --------------------------------------------------------------------------- #

_OLD_EMBEDDING_AFFINITY_SQL = """
    SELECT d.id::text AS doc_id,
           MAX(1 - (c.embedding <=> %(vec)s::vector)) AS cosine
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE d.draft = FALSE
      AND d.vault_path IS NOT NULL
      AND d.id <> %(source)s::uuid
      AND c.embedding IS NOT NULL
      AND 1 - (c.embedding <=> %(vec)s::vector) >= %(floor)s
    GROUP BY d.id
    ORDER BY cosine DESC, doc_id
    LIMIT %(limit)s
"""


def _old_embedding_affinity(
    conn: psycopg.Connection,
    *,
    source_doc_id: str,
    candidate_limit: int,
    vector_sim_floor: float,
) -> dict[str, float]:
    """Pre-4.1 exhaustive embedding-affinity semantics, reproduced inline.

    The equivalence oracle for the KNN rewrite: same avg-embedding query vector,
    same eligibility filters, same per-doc ``MAX`` + floor + ``ORDER BY cosine
    DESC, doc_id LIMIT``. Insertion order is the SQL row order.
    """
    from brain.wiki.build_related import _avg_embedding

    src = _avg_embedding(conn, source_doc_id)
    if src is None:
        return {}
    rows = conn.execute(
        _OLD_EMBEDDING_AFFINITY_SQL,
        {
            "vec": src,
            "source": source_doc_id,
            "floor": vector_sim_floor,
            "limit": candidate_limit,
        },
    ).fetchall()
    return {str(r[0]): float(r[1]) for r in rows}


def _add_chunk(
    conn: psycopg.Connection,
    *,
    doc_id: str,
    chunk_index: int,
    copy_embedding_from_doc: str | None = None,
    null_embedding: bool = False,
) -> None:
    """Insert an extra chunk row so a doc can carry several controlled chunks.

    - ``copy_embedding_from_doc``: copy that doc's ``chunk_index=0`` embedding
      (used to plant a high-cosine chunk equal to the source's reference vector).
    - ``null_embedding``: insert with a NULL embedding (exercises the
      ``embedding IS NOT NULL`` filter + per-doc MAX skipping the NULL chunk).
    - neither: copy this doc's OWN ``chunk_index=0`` embedding (a second natural,
      low-cosine chunk).
    """
    if null_embedding:
        conn.execute(
            "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
            "VALUES (%s::uuid, %s, %s, NULL)",
            (doc_id, chunk_index, f"extra chunk {chunk_index}"),
        )
        return
    src_doc = copy_embedding_from_doc if copy_embedding_from_doc is not None else doc_id
    conn.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
        "VALUES (%s::uuid, %s, %s, "
        "  (SELECT embedding FROM chunks "
        "   WHERE document_id = %s::uuid AND chunk_index = 0 LIMIT 1))",
        (doc_id, chunk_index, f"extra chunk {chunk_index}", src_doc),
    )


def _copy_chunk0_embedding(
    conn: psycopg.Connection, *, into_doc: str, from_doc: str
) -> None:
    """Point ``into_doc``'s chunk 0 embedding at ``from_doc``'s chunk 0 (cosine 1.0)."""
    conn.execute(
        "UPDATE chunks SET embedding = "
        "  (SELECT embedding FROM chunks "
        "   WHERE document_id = %s::uuid AND chunk_index = 0 LIMIT 1) "
        "WHERE document_id = %s::uuid AND chunk_index = 0",
        (from_doc, into_doc),
    )


def _seed_equivalence_corpus(
    conn: psycopg.Connection, embedder: FakeEmbedder
) -> dict[str, str]:
    """Seed ~30 chunks across 12 docs spanning every branch the rewrite touches.

    Returns a role→doc-id map. ``source`` is the query doc; four docs carry a
    chunk copied from the source (cosine ≈ 1.0); the rest keep their natural
    (low-cosine) fake-embedder vectors. Categories exercised: multi-chunk
    per-doc MAX, a NULL-embedding chunk, drafts, non-vault docs, self-exclusion,
    a tie at the top (four docs at cosine ≈ 1.0), and low sub-floor docs.
    """
    ids: dict[str, str] = {}
    ids["source"] = _make_vault_doc(
        conn, embedder, title="Source", content="source body text", vault_path="eq/source.md"
    )
    src = ids["source"]

    # Four docs whose best chunk equals the source vector → cosine ≈ 1.0 (tie).
    ids["hi_a"] = _make_vault_doc(
        conn, embedder, title="Hi A", content="hi a body", vault_path="eq/hi_a.md"
    )
    _copy_chunk0_embedding(conn, into_doc=ids["hi_a"], from_doc=src)
    ids["hi_b"] = _make_vault_doc(
        conn, embedder, title="Hi B", content="hi b body", vault_path="eq/hi_b.md"
    )
    _copy_chunk0_embedding(conn, into_doc=ids["hi_b"], from_doc=src)

    # Multi-chunk doc: chunk0 natural (low) + chunk99 = source vector → MAX ≈ 1.0.
    ids["multi_max"] = _make_vault_doc(
        conn, embedder, title="Multi Max", content="multi max body", vault_path="eq/multi.md"
    )
    _add_chunk(conn, doc_id=ids["multi_max"], chunk_index=99, copy_embedding_from_doc=src)

    # NULL-embedding chunk0 + chunk99 = source vector → still included at ≈ 1.0.
    ids["null_chunk"] = _make_vault_doc(
        conn, embedder, title="Null Chunk", content="null chunk body", vault_path="eq/null.md"
    )
    conn.execute(
        "UPDATE chunks SET embedding = NULL WHERE document_id = %s::uuid AND chunk_index = 0",
        (ids["null_chunk"],),
    )
    _add_chunk(conn, doc_id=ids["null_chunk"], chunk_index=99, copy_embedding_from_doc=src)

    # Natural low-cosine docs (candidate sub-floor exclusions), some multi-chunk.
    ids["low1"] = _make_vault_doc(
        conn, embedder, title="Low One", content="low one body", vault_path="eq/low1.md"
    )
    ids["low2"] = _make_vault_doc(
        conn, embedder, title="Low Two", content="low two body", vault_path="eq/low2.md"
    )
    ids["mid_multi"] = _make_vault_doc(
        conn, embedder, title="Mid Multi", content="mid multi body", vault_path="eq/mid.md"
    )
    _add_chunk(conn, doc_id=ids["mid_multi"], chunk_index=1)
    for i, role in enumerate(("filler1", "filler2", "filler3")):
        ids[role] = _make_vault_doc(
            conn,
            embedder,
            title=f"Filler {i}",
            content=f"filler body {i}",
            vault_path=f"eq/filler{i}.md",
        )
        _add_chunk(conn, doc_id=ids[role], chunk_index=1)
        _add_chunk(conn, doc_id=ids[role], chunk_index=2)

    # Excluded-category docs, each planted with the high (≈1.0) source vector so
    # ONLY the draft / vault filters can keep them out of the result.
    ids["draft_hi"] = _make_vault_doc(
        conn, embedder, title="Draft Hi", content="draft hi body", vault_path="eq/draft.md"
    )
    _copy_chunk0_embedding(conn, into_doc=ids["draft_hi"], from_doc=src)
    conn.execute(
        "UPDATE documents SET draft = TRUE WHERE id = %s::uuid", (ids["draft_hi"],)
    )
    ids["novault_hi"] = _make_vault_doc(
        conn, embedder, title="Novault Hi", content="novault hi body", vault_path="eq/nv.md"
    )
    _copy_chunk0_embedding(conn, into_doc=ids["novault_hi"], from_doc=src)
    conn.execute(
        "UPDATE documents SET vault_path = NULL WHERE id = %s::uuid",
        (ids["novault_hi"],),
    )
    return ids


@pytest.mark.parametrize("candidate_limit", [3, 50])
@pytest.mark.parametrize("floor", [0.0, 0.25, 0.5, 0.99])
def test_embedding_affinity_matches_old_exhaustive_semantics(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    candidate_limit: int,
    floor: float,
) -> None:
    # THE EQUIVALENCE GUARANTEE: the KNN rewrite returns byte-identical results
    # (keys, values, AND insertion order) to the pre-4.1 exhaustive query across
    # a spread of floors and candidate limits.
    ids = _seed_equivalence_corpus(test_db, fake_embedder)
    src = ids["source"]
    expected = _old_embedding_affinity(
        test_db, source_doc_id=src, candidate_limit=candidate_limit, vector_sim_floor=floor
    )
    actual = connect_mod.embedding_affinity(
        test_db, source_doc_id=src, candidate_limit=candidate_limit, vector_sim_floor=floor
    )
    # Order-preserving equivalence (dict == ignores order; items() compares it).
    assert list(actual.items()) == list(expected.items())
    # Excluded categories never appear, regardless of floor.
    for excluded in (ids["source"], ids["draft_hi"], ids["novault_hi"]):
        assert excluded not in actual


def test_embedding_affinity_high_floor_keeps_only_reference_docs(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # Deterministic slice: at floor 0.99 only the four docs whose best chunk
    # equals the source vector survive (cosine ≈ 1.0). This nails multi-chunk
    # MAX (multi_max), the NULL-embedding skip (null_chunk), and the top-tie
    # ordering by doc id — all in one shot.
    ids = _seed_equivalence_corpus(test_db, fake_embedder)
    actual = connect_mod.embedding_affinity(
        test_db, source_doc_id=ids["source"], candidate_limit=50, vector_sim_floor=0.99
    )
    reference_docs = {ids["hi_a"], ids["hi_b"], ids["multi_max"], ids["null_chunk"]}
    assert set(actual) == reference_docs
    # Tie at the top → ordered by doc id ascending.
    assert list(actual) == sorted(reference_docs)
    for cosine in actual.values():
        assert cosine == pytest.approx(1.0, abs=1e-6)


def test_embedding_affinity_data_driven_floor_excludes_lowest(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # A floor picked strictly between the two lowest per-doc cosines must exclude
    # exactly the lowest doc — and the rewrite still matches the old query.
    ids = _seed_equivalence_corpus(test_db, fake_embedder)
    src = ids["source"]
    all_scores = _old_embedding_affinity(
        test_db, source_doc_id=src, candidate_limit=50, vector_sim_floor=-1.0
    )
    ranked = sorted(all_scores.items(), key=lambda kv: kv[1])
    assert len(ranked) >= 2
    lowest_doc, lowest_cos = ranked[0]
    floor = (lowest_cos + ranked[1][1]) / 2.0
    expected = _old_embedding_affinity(
        test_db, source_doc_id=src, candidate_limit=50, vector_sim_floor=floor
    )
    actual = connect_mod.embedding_affinity(
        test_db, source_doc_id=src, candidate_limit=50, vector_sim_floor=floor
    )
    assert list(actual.items()) == list(expected.items())
    assert lowest_doc not in actual


def _vec_literal(ones: int, dim: int) -> str:
    """pgvector text literal: ``ones`` leading 1.0s, the rest 0.0 (dim total).

    Cosine similarity to an all-ones vector of the same dim is
    ``ones/(sqrt(ones)*sqrt(dim)) = sqrt(ones/dim)`` — strictly increasing in
    ``ones``, so distinct ``ones`` give distinct, ORDERED cosines. Lets us plant
    a deterministic cosine gradient the fake embedder can't produce on its own.
    """
    values = [1.0] * ones + [0.0] * (dim - ones)
    return "[" + ",".join(str(v) for v in values) + "]"


def _set_chunk0_vector(conn: psycopg.Connection, *, doc_id: str, literal: str) -> None:
    conn.execute(
        "UPDATE chunks SET embedding = %s::vector "
        "WHERE document_id = %s::uuid AND chunk_index = 0",
        (literal, doc_id),
    )


def test_embedding_affinity_forced_truncation_converges_to_exhaustive(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # FORCED TRUNCATION: with a small starting K the first KNN batch is
    # dominated by one multi-chunk doc, so the adaptive re-query MUST fire to
    # surface the remaining docs. The result must converge to the exhaustive
    # query and to the default-K result. A broken (no re-query) impl would
    # return only the dominant doc and fail here.
    dim = fake_embedder.dim
    src = _make_vault_doc(
        test_db, fake_embedder, title="Src", content="src body", vault_path="tr/src.md"
    )
    # Source = all-ones; targets = leading-ones prefixes → cosine sqrt(m/dim).
    _set_chunk0_vector(test_db, doc_id=src, literal=_vec_literal(dim, dim))

    # t0 dominates the top: THREE chunks all at cosine 1.0 (equal to source).
    t0 = _make_vault_doc(
        test_db, fake_embedder, title="T0", content="t0 body", vault_path="tr/t0.md"
    )
    _set_chunk0_vector(test_db, doc_id=t0, literal=_vec_literal(dim, dim))
    _add_chunk(test_db, doc_id=t0, chunk_index=1, copy_embedding_from_doc=t0)
    _add_chunk(test_db, doc_id=t0, chunk_index=2, copy_embedding_from_doc=t0)

    # Descending distinct cosines, all comfortably above the 0.25 floor.
    gradient = {"t1": 3600, "t2": 2500, "t3": 1600, "t4": 900}
    tids: dict[str, str] = {"t0": t0}
    for role, ones in gradient.items():
        doc = _make_vault_doc(
            test_db,
            fake_embedder,
            title=role.upper(),
            content=f"{role} body",
            vault_path=f"tr/{role}.md",
        )
        _set_chunk0_vector(test_db, doc_id=doc, literal=_vec_literal(ones, dim))
        tids[role] = doc

    candidate_limit = 3
    floor = 0.25
    expected = _old_embedding_affinity(
        test_db, source_doc_id=src, candidate_limit=candidate_limit, vector_sim_floor=floor
    )
    # knn_multiplier=1 → start K = candidate_limit (=3); t0's three top chunks
    # fill it, forcing at least one doubling before 3 distinct docs are seen.
    forced = connect_mod.embedding_affinity(
        test_db,
        source_doc_id=src,
        candidate_limit=candidate_limit,
        vector_sim_floor=floor,
        knn_multiplier=1,
    )
    default = connect_mod.embedding_affinity(
        test_db,
        source_doc_id=src,
        candidate_limit=candidate_limit,
        vector_sim_floor=floor,
    )
    # Truncated start converges to the exhaustive answer AND the default-K path.
    assert list(forced.items()) == list(expected.items())
    assert list(default.items()) == list(expected.items())
    # Top-3 by the planted gradient: t0 (1.0) > t1 > t2; t3/t4 truncated away.
    assert list(forced) == [tids["t0"], tids["t1"], tids["t2"]]
    assert forced[tids["t0"]] == pytest.approx(1.0, abs=1e-6)


def test_embedding_affinity_rejects_bad_knn_multiplier(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="alpha", vault_path="n/a.md"
    )
    with pytest.raises(ConnectError):
        connect_mod.embedding_affinity(
            test_db,
            source_doc_id=a,
            candidate_limit=50,
            vector_sim_floor=0.25,
            knn_multiplier=0,
        )


# --------------------------------------------------------------------------- #
# refresh_suggestions (DB read + write).
# --------------------------------------------------------------------------- #


def _seed_overlapping_pair(
    conn: psycopg.Connection, embedder: FakeEmbedder
) -> tuple[str, str]:
    """Two vault docs sharing two entities (graph score 1.0 each direction)."""
    a = _make_vault_doc(
        conn, embedder, title="Doc A", content="alpha body", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        conn, embedder, title="Doc B", content="beta body", vault_path="n/b.md"
    )
    e1 = _add_entity(conn, name="Shared One", key="shared-one")
    e2 = _add_entity(conn, name="Shared Two", key="shared-two")
    for ent in (e1, e2):
        _mention(conn, entity_id=ent, doc_id=a)
        _mention(conn, entity_id=ent, doc_id=b)
    return a, b


def test_refresh_creates_suggestion(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a, b = _seed_overlapping_pair(test_db, fake_embedder)
    result = connect_mod.refresh_suggestions(test_db, _cfg())
    assert result.written >= 1
    rows = test_db.execute(
        "SELECT source_doc_id::text, target_doc_id::text, status "
        "FROM link_suggestions"
    ).fetchall()
    pairs = {(str(r[0]), str(r[1])) for r in rows}
    assert (a, b) in pairs or (b, a) in pairs
    assert all(r[2] == "pending" for r in rows)


def test_refresh_dedup_links_table(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a, b = _seed_overlapping_pair(test_db, fake_embedder)
    test_db.execute(
        "INSERT INTO links (src_document_id, dst_document_id, link_text, link_kind) "
        "VALUES (%s::uuid, %s::uuid, %s, %s)",
        (a, b, "Doc B", "wiki"),
    )
    connect_mod.refresh_suggestions(test_db, _cfg())
    ab = test_db.execute(
        "SELECT 1 FROM link_suggestions WHERE source_doc_id = %s::uuid "
        "AND target_doc_id = %s::uuid",
        (a, b),
    ).fetchone()
    assert ab is None  # A→B already linked → not re-suggested


def test_refresh_dedup_derived_links_table(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a, b = _seed_overlapping_pair(test_db, fake_embedder)
    test_db.execute(
        "INSERT INTO derived_links (src_document_id, dst_document_id, rule, weight) "
        "VALUES (%s::uuid, %s::uuid, %s, %s)",
        (a, b, "shared_thread", 1.0),
    )
    connect_mod.refresh_suggestions(test_db, _cfg())
    ab = test_db.execute(
        "SELECT 1 FROM link_suggestions WHERE source_doc_id = %s::uuid "
        "AND target_doc_id = %s::uuid",
        (a, b),
    ).fetchone()
    assert ab is None


def test_refresh_dedup_derived_links_reverse_orientation(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # Codex R3 #1: derived_links are canonical/undirected. A derived edge stored
    # as (b, a) must still suppress the a→b suggestion (and b→a).
    a, b = _seed_overlapping_pair(test_db, fake_embedder)
    test_db.execute(
        "INSERT INTO derived_links (src_document_id, dst_document_id, rule, weight) "
        "VALUES (%s::uuid, %s::uuid, %s, %s)",
        (b, a, "shared_thread", 1.0),  # OPPOSITE orientation to the a→b pair
    )
    connect_mod.refresh_suggestions(test_db, _cfg())
    rows = test_db.execute(
        "SELECT 1 FROM link_suggestions "
        "WHERE (source_doc_id = %s::uuid AND target_doc_id = %s::uuid) "
        "   OR (source_doc_id = %s::uuid AND target_doc_id = %s::uuid)",
        (a, b, b, a),
    ).fetchall()
    assert rows == []  # undirected derived edge covers both directions


def test_refresh_retires_now_linked_derived_reverse_orientation(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # Codex R3 #1: a pending a→b row is retired when a derived edge is later
    # added in the (b, a) orientation.
    a, b = _seed_overlapping_pair(test_db, fake_embedder)
    connect_mod.refresh_suggestions(test_db, _cfg())
    count = test_db.execute("SELECT count(*) FROM link_suggestions").fetchone()
    assert count is not None and int(count[0]) > 0
    test_db.execute(
        "INSERT INTO derived_links (src_document_id, dst_document_id, rule, weight) "
        "VALUES (%s::uuid, %s::uuid, %s, %s)",
        (b, a, "shared_thread", 1.0),
    )
    connect_mod.refresh_suggestions(test_db, _cfg())
    remaining = test_db.execute(
        "SELECT 1 FROM link_suggestions "
        "WHERE (source_doc_id = %s::uuid AND target_doc_id = %s::uuid) "
        "   OR (source_doc_id = %s::uuid AND target_doc_id = %s::uuid)",
        (a, b, b, a),
    ).fetchall()
    assert remaining == []


def test_refresh_retires_now_linked_pending(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # Codex R2 #1: a pending suggestion whose pair gets linked AFTER it was
    # queued must be retired on the next refresh (not linger in the queue).
    a, b = _seed_overlapping_pair(test_db, fake_embedder)
    connect_mod.refresh_suggestions(test_db, _cfg())
    row = test_db.execute(
        "SELECT source_doc_id::text, target_doc_id::text FROM link_suggestions "
        "WHERE status = 'pending' LIMIT 1"
    ).fetchone()
    assert row is not None
    src, tgt = str(row[0]), str(row[1])
    # The user draws the wikilink manually after the suggestion was queued.
    test_db.execute(
        "INSERT INTO links (src_document_id, dst_document_id, link_text, link_kind) "
        "VALUES (%s::uuid, %s::uuid, %s, %s)",
        (src, tgt, "manual", "wiki"),
    )
    connect_mod.refresh_suggestions(test_db, _cfg())
    still_there = test_db.execute(
        "SELECT 1 FROM link_suggestions WHERE source_doc_id = %s::uuid "
        "AND target_doc_id = %s::uuid",
        (src, tgt),
    ).fetchone()
    assert still_there is None  # retired — the pair is now linked


def test_refresh_retires_now_linked_via_derived_links(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a, b = _seed_overlapping_pair(test_db, fake_embedder)
    connect_mod.refresh_suggestions(test_db, _cfg())
    row = test_db.execute(
        "SELECT source_doc_id::text, target_doc_id::text FROM link_suggestions "
        "WHERE status = 'pending' LIMIT 1"
    ).fetchone()
    assert row is not None
    src, tgt = str(row[0]), str(row[1])
    test_db.execute(
        "INSERT INTO derived_links (src_document_id, dst_document_id, rule, weight) "
        "VALUES (%s::uuid, %s::uuid, %s, %s)",
        (src, tgt, "shared_thread", 1.0),
    )
    connect_mod.refresh_suggestions(test_db, _cfg())
    still_there = test_db.execute(
        "SELECT 1 FROM link_suggestions WHERE source_doc_id = %s::uuid "
        "AND target_doc_id = %s::uuid",
        (src, tgt),
    ).fetchone()
    assert still_there is None


def test_refresh_retire_does_not_touch_accepted(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # The retire cleanup only removes pending rows — an accepted row for a now
    # -linked pair stays (it is the historical record of the accept).
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md"
    )
    sid = _insert_suggestion(test_db, source=a, target=b, status="accepted")
    test_db.execute(
        "INSERT INTO links (src_document_id, dst_document_id, link_text, link_kind) "
        "VALUES (%s::uuid, %s::uuid, %s, %s)",
        (a, b, "Doc B", "wiki"),
    )
    connect_mod.refresh_suggestions(test_db, _cfg())
    row = test_db.execute(
        "SELECT status FROM link_suggestions WHERE id = %s::uuid", (sid,)
    ).fetchone()
    assert row is not None and row[0] == "accepted"


def test_refresh_idempotent(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    _seed_overlapping_pair(test_db, fake_embedder)
    connect_mod.refresh_suggestions(test_db, _cfg())
    first = test_db.execute("SELECT count(*) FROM link_suggestions").fetchone()
    assert first is not None
    connect_mod.refresh_suggestions(test_db, _cfg())
    second = test_db.execute("SELECT count(*) FROM link_suggestions").fetchone()
    assert second is not None
    assert int(first[0]) == int(second[0])  # no duplicate rows


def test_refresh_confidence_gate_excludes_low_scores(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # Partial graph overlap 0.2 → graph-only score 0.1 < min 0.30 → no write.
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="alpha", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="beta", vault_path="n/b.md"
    )
    ents = [_add_entity(test_db, name=f"E{i}", key=f"ent-{i}") for i in range(10)]
    # A: e0..e4 (5); B: e0, e5..e8 (5) → shared 1 / 5 = 0.2 → score 0.1.
    for ent in ents[:5]:
        _mention(test_db, entity_id=ent, doc_id=a)
    for ent in (ents[0], ents[5], ents[6], ents[7], ents[8]):
        _mention(test_db, entity_id=ent, doc_id=b)
    # Suppress the embedding leg with a near-1 cosine floor so the graph-only
    # score (0.1) governs the gate; docs stay eligible (chunks keep embeddings).
    cfg = dataclasses.replace(_cfg(), vector_sim_floor=0.999)
    connect_mod.refresh_suggestions(test_db, cfg)
    rows = test_db.execute(
        "SELECT 1 FROM link_suggestions WHERE source_doc_id = %s::uuid "
        "AND target_doc_id = %s::uuid",
        (a, b),
    ).fetchall()
    assert rows == []


def test_refresh_respects_max_per_doc(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # One source sharing entities with 3 targets, cap = 1 → only 1 row for it.
    src = _make_vault_doc(
        test_db, fake_embedder, title="Src", content="s", vault_path="n/src.md"
    )
    shared = _add_entity(test_db, name="Shared", key="shared")
    _mention(test_db, entity_id=shared, doc_id=src)
    for i in range(3):
        tgt = _make_vault_doc(
            test_db,
            fake_embedder,
            title=f"T{i}",
            content=f"target body {i}",
            vault_path=f"n/t{i}.md",
        )
        _mention(test_db, entity_id=shared, doc_id=tgt)
    # Graph-only (near-1 floor suppresses the embedding leg): all 3 targets tie
    # at graph 1.0 → score 0.5; max_per_doc=1 keeps just one for the source.
    cfg = dataclasses.replace(_cfg(), connect_max_per_doc=1, vector_sim_floor=0.999)
    connect_mod.refresh_suggestions(test_db, cfg)
    count = test_db.execute(
        "SELECT count(*) FROM link_suggestions WHERE source_doc_id = %s::uuid",
        (src,),
    ).fetchone()
    assert count is not None
    assert int(count[0]) == 1


def test_refresh_dry_run_writes_nothing(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    _seed_overlapping_pair(test_db, fake_embedder)
    result = connect_mod.refresh_suggestions(test_db, _cfg(), dry_run=True)
    assert result.dry_run is True
    assert result.candidates >= 1
    count = test_db.execute("SELECT count(*) FROM link_suggestions").fetchone()
    assert count is not None
    assert int(count[0]) == 0


def test_refresh_doc_prefix_filters_sources(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a, b = _seed_overlapping_pair(test_db, fake_embedder)
    connect_mod.refresh_suggestions(test_db, _cfg(), doc_prefix=a[:8])
    rows = test_db.execute(
        "SELECT DISTINCT source_doc_id::text FROM link_suggestions"
    ).fetchall()
    # Only A was scored as a source.
    assert {str(r[0]) for r in rows} == {a}


def test_refresh_rejects_bad_config(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    cfg = dataclasses.replace(_cfg(), connect_max_per_doc=0)
    with pytest.raises(ConnectError):
        connect_mod.refresh_suggestions(test_db, cfg)


# --------------------------------------------------------------------------- #
# Upsert freezing of accepted/rejected rows.
# --------------------------------------------------------------------------- #


def test_upsert_freezes_accepted_row(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    _seed_overlapping_pair(test_db, fake_embedder)
    connect_mod.refresh_suggestions(test_db, _cfg())
    row = test_db.execute(
        "SELECT id::text FROM link_suggestions LIMIT 1"
    ).fetchone()
    assert row is not None
    connect_mod.set_suggestion_status(test_db, str(row[0]), "accepted")
    connect_mod.refresh_suggestions(test_db, _cfg())
    after = test_db.execute(
        "SELECT status FROM link_suggestions WHERE id = %s::uuid", (str(row[0]),)
    ).fetchone()
    assert after is not None
    assert after[0] == "accepted"  # frozen — not flipped back to pending


def test_upsert_freezes_rejected_row(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    _seed_overlapping_pair(test_db, fake_embedder)
    connect_mod.refresh_suggestions(test_db, _cfg())
    row = test_db.execute(
        "SELECT id::text FROM link_suggestions LIMIT 1"
    ).fetchone()
    assert row is not None
    connect_mod.set_suggestion_status(test_db, str(row[0]), "rejected")
    connect_mod.refresh_suggestions(test_db, _cfg())
    after = test_db.execute(
        "SELECT status FROM link_suggestions WHERE id = %s::uuid", (str(row[0]),)
    ).fetchone()
    assert after is not None
    assert after[0] == "rejected"


# --------------------------------------------------------------------------- #
# Accept / reject status machine + queue reads.
# --------------------------------------------------------------------------- #


def test_accept_no_write_status_only(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder, tmp_path: Path
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md"
    )
    sid = _insert_suggestion(test_db, source=a, target=b)
    source_file = tmp_path / "n" / "a.md"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("# Doc A\n\nbody\n", encoding="utf-8")
    before = source_file.read_text(encoding="utf-8")

    action = connect_mod.set_suggestion_status(test_db, sid, "accepted")
    assert action.status == "accepted"
    row = test_db.execute(
        "SELECT status, actioned_at FROM link_suggestions WHERE id = %s::uuid", (sid,)
    ).fetchone()
    assert row is not None
    assert row[0] == "accepted"
    assert row[1] is not None  # actioned_at stamped
    # No file write happened (status-only).
    assert source_file.read_text(encoding="utf-8") == before


def test_reject_status_flip(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md"
    )
    sid = _insert_suggestion(test_db, source=a, target=b)
    action = connect_mod.set_suggestion_status(test_db, sid, "rejected")
    assert action.status == "rejected"
    row = test_db.execute(
        "SELECT status FROM link_suggestions WHERE id = %s::uuid", (sid,)
    ).fetchone()
    assert row is not None
    assert row[0] == "rejected"


def test_set_status_rejects_unknown(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md"
    )
    sid = _insert_suggestion(test_db, source=a, target=b)
    with pytest.raises(ConnectError):
        connect_mod.set_suggestion_status(test_db, sid, "bogus")


def test_iter_suggestions_pending_default_and_all(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md"
    )
    c = _make_vault_doc(
        test_db, fake_embedder, title="Doc C", content="c", vault_path="n/c.md"
    )
    _insert_suggestion(test_db, source=a, target=b, score=0.9, status="pending")
    _insert_suggestion(test_db, source=a, target=c, score=0.5, status="rejected")

    pending = connect_mod.iter_suggestions(test_db, status="pending", limit=20)
    assert [r.target_doc_id for r in pending] == [b]
    assert pending[0].source_title == "Doc A"
    assert pending[0].target_title == "Doc B"

    allrows = connect_mod.iter_suggestions(test_db, status=None, limit=20)
    assert len(allrows) == 2  # both statuses
    # Ordered by score desc.
    assert allrows[0].score >= allrows[1].score


def test_suggestion_counts_zero_filled(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md"
    )
    _insert_suggestion(test_db, source=a, target=b, status="accepted")
    counts = connect_mod.suggestion_counts(test_db)
    assert counts == {"pending": 0, "accepted": 1, "rejected": 0}


def test_resolve_suggestion_prefix_errors(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    with pytest.raises(ConnectError):
        connect_mod.resolve_suggestion_prefix(test_db, "abc")  # too short
    with pytest.raises(ConnectError):
        connect_mod.resolve_suggestion_prefix(test_db, "zzzzzz")  # non-hex
    with pytest.raises(ConnectError):
        connect_mod.resolve_suggestion_prefix(test_db, "abcdef")  # not found


# --------------------------------------------------------------------------- #
# Vault writeback.
# --------------------------------------------------------------------------- #


def test_build_see_also_wikilink_is_path_alias() -> None:
    link = connect_mod.build_see_also_wikilink(
        "_ingested/krisp/2026-01-15-abcd1234-meeting.md", "Synthetic Target Title"
    )
    assert link == (
        "[[_ingested/krisp/2026-01-15-abcd1234-meeting|Synthetic Target Title]]"
    )


def test_build_see_also_wikilink_sanitizes_title() -> None:
    link = connect_mod.build_see_also_wikilink("n/t.md", "Re: [External] a|b")
    # Brackets → parens (Quartz alias rule); pipe → hyphen (wikilink delimiter).
    assert link == "[[n/t|Re: (External) a-b]]"


def test_append_see_also_creates_section(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text("# Doc A\n\nbody\n", encoding="utf-8")
    written = connect_mod.append_see_also_link(f, "[[n/t|Target]]")
    assert written is True
    text = f.read_text(encoding="utf-8")
    assert "## See Also" in text
    assert "- [[n/t|Target]]" in text


def test_append_see_also_idempotent(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text("# Doc A\n", encoding="utf-8")
    assert connect_mod.append_see_also_link(f, "[[n/t|Target]]") is True
    assert connect_mod.append_see_also_link(f, "[[n/t|Target]]") is False
    assert f.read_text(encoding="utf-8").count("[[n/t|Target]]") == 1


def test_append_see_also_appends_to_existing_section(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text("# Doc A\n\n## See Also\n\n- [[n/x|X]]\n", encoding="utf-8")
    assert connect_mod.append_see_also_link(f, "[[n/y|Y]]") is True
    text = f.read_text(encoding="utf-8")
    assert text.count("## See Also") == 1  # no duplicate heading
    assert "- [[n/x|X]]" in text
    assert "- [[n/y|Y]]" in text


def test_append_see_also_inserts_into_section_not_trailing(tmp_path: Path) -> None:
    # Codex R1 #2: ## See Also exists but is NOT the trailing section — the new
    # bullet must land under See Also, not under the later ## Notes section.
    f = tmp_path / "a.md"
    f.write_text(
        "# Doc A\n\n## See Also\n\n- [[n/x|X]]\n\n## Notes\n\nlater content\n",
        encoding="utf-8",
    )
    assert connect_mod.append_see_also_link(f, "[[n/y|Y]]") is True
    text = f.read_text(encoding="utf-8")
    see_also_idx = text.index("## See Also")
    notes_idx = text.index("## Notes")
    y_idx = text.index("- [[n/y|Y]]")
    # The new bullet sits between the See Also heading and the Notes heading.
    assert see_also_idx < y_idx < notes_idx
    # Later content is untouched and stays last.
    assert text.index("later content") > notes_idx
    assert text.count("## See Also") == 1


def test_accept_write_inserts_path_alias_wikilink(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder, tmp_path: Path
) -> None:
    # Source + target are real vault files; accept --write inserts the alias.
    src = _make_vault_doc(
        test_db, fake_embedder, title="Source Doc", content="s", vault_path="notes/src.md"
    )
    tgt = _make_vault_doc(
        test_db,
        fake_embedder,
        title="Synthetic Target Title",
        content="t",
        vault_path="notes/tgt.md",
    )
    sid = _insert_suggestion(test_db, source=src, target=tgt)
    source_file = tmp_path / "notes" / "src.md"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("# Source Doc\n\nbody\n", encoding="utf-8")

    action = connect_mod.set_suggestion_status(test_db, sid, "accepted")
    wikilink = connect_mod.build_see_also_wikilink(
        action.target_vault_path or "", action.target_title
    )
    written = connect_mod.append_see_also_link(source_file, wikilink)
    assert written is True
    text = source_file.read_text(encoding="utf-8")
    assert "## See Also" in text
    # Path-form alias, NOT a bare [[Synthetic Target Title]].
    assert "[[notes/tgt|Synthetic Target Title]]" in text
    assert "[[Synthetic Target Title]]" not in text


# --------------------------------------------------------------------------- #
# CLI surface.
# --------------------------------------------------------------------------- #


def test_connect_list_empty(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    patch_embedder(fake_embedder)
    result = runner.invoke(app, ["connect", "list"])
    assert result.exit_code == 0
    assert "no pending suggestions" in result.stdout


def test_connect_list_json(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md"
    )
    _insert_suggestion(test_db, source=a, target=b, score=0.7, graph=0.8, embed=0.6)
    patch_embedder(fake_embedder)
    result = runner.invoke(app, ["connect", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 1
    row = payload[0]
    for key in (
        "id",
        "source_title",
        "target_title",
        "score",
        "graph_score",
        "embed_score",
    ):
        assert key in row
    assert row["source_title"] == "Doc A"
    assert row["target_title"] == "Doc B"


def test_connect_list_default_is_list(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    # Bare `brain connect` aliases `brain connect list`.
    patch_embedder(fake_embedder)
    result = runner.invoke(app, ["connect"])
    assert result.exit_code == 0
    assert "no pending suggestions" in result.stdout


def test_connect_stats_counts(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md"
    )
    c = _make_vault_doc(
        test_db, fake_embedder, title="Doc C", content="c", vault_path="n/c.md"
    )
    _insert_suggestion(test_db, source=a, target=b, status="pending")
    _insert_suggestion(test_db, source=a, target=c, status="accepted")
    patch_embedder(fake_embedder)
    result = runner.invoke(app, ["connect", "stats"])
    assert result.exit_code == 0
    assert "pending 1" in result.stdout
    assert "accepted 1" in result.stdout
    assert "rejected 0" in result.stdout


def test_connect_refresh_dry_run(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    _seed_overlapping_pair(test_db, fake_embedder)
    patch_embedder(fake_embedder)
    result = runner.invoke(app, ["connect", "refresh", "--dry-run"])
    assert result.exit_code == 0
    assert "candidate pair" in result.stdout
    count = test_db.execute("SELECT count(*) FROM link_suggestions").fetchone()
    assert count is not None
    assert int(count[0]) == 0  # dry run wrote nothing


def test_connect_refresh_persists(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    _seed_overlapping_pair(test_db, fake_embedder)
    patch_embedder(fake_embedder)
    result = runner.invoke(app, ["connect", "refresh"])
    assert result.exit_code == 0
    count = test_db.execute("SELECT count(*) FROM link_suggestions").fetchone()
    assert count is not None
    assert int(count[0]) >= 1


def test_connect_accept_reject_cli(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md"
    )
    c = _make_vault_doc(
        test_db, fake_embedder, title="Doc C", content="c", vault_path="n/c.md"
    )
    sid = _insert_suggestion(test_db, source=a, target=b)
    patch_embedder(fake_embedder)
    accept = runner.invoke(app, ["connect", "accept", sid[:8]])
    assert accept.exit_code == 0
    assert "accepted" in accept.stdout
    status = test_db.execute(
        "SELECT status FROM link_suggestions WHERE id = %s::uuid", (sid,)
    ).fetchone()
    assert status is not None and status[0] == "accepted"

    # Reject a DISTINCT pair — suggestions are undirected (migration 022), so a
    # (b, a) mirror of the accepted (a, b) pair can no longer exist as its own
    # row. Use a fresh {a, c} pair to exercise the reject path.
    sid2 = _insert_suggestion(test_db, source=a, target=c)
    reject = runner.invoke(app, ["connect", "reject", sid2[:8]])
    assert reject.exit_code == 0
    assert "rejected" in reject.stdout


def test_connect_accept_unknown_id_errors(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
) -> None:
    patch_embedder(fake_embedder)
    result = runner.invoke(app, ["connect", "accept", "abcdef"])
    assert result.exit_code == 1


def test_connect_accept_write_cli(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    src = _make_vault_doc(
        test_db, fake_embedder, title="Source Doc", content="s", vault_path="notes/src.md"
    )
    tgt = _make_vault_doc(
        test_db, fake_embedder, title="Target Doc", content="t", vault_path="notes/tgt.md"
    )
    sid = _insert_suggestion(test_db, source=src, target=tgt)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    source_file = tmp_path / "notes" / "src.md"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("# Source Doc\n\nbody\n", encoding="utf-8")
    patch_embedder(fake_embedder)

    result = runner.invoke(app, ["connect", "accept", sid[:8], "--write"])
    assert result.exit_code == 0
    text = source_file.read_text(encoding="utf-8")
    assert "## See Also" in text
    assert "[[notes/tgt|Target Doc]]" in text

    # Second accept --write is idempotent (link already present).
    again = runner.invoke(app, ["connect", "accept", sid[:8], "--write"])
    assert again.exit_code == 0
    assert source_file.read_text(encoding="utf-8").count("[[notes/tgt|Target Doc]]") == 1


def test_connect_accept_write_missing_file_leaves_pending(
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Codex R1 #1: accept --write must NOT freeze the row accepted if the vault
    # write fails (here: the source file does not exist on disk).
    src = _make_vault_doc(
        test_db, fake_embedder, title="Source", content="s", vault_path="notes/src.md"
    )
    tgt = _make_vault_doc(
        test_db, fake_embedder, title="Target", content="t", vault_path="notes/tgt.md"
    )
    sid = _insert_suggestion(test_db, source=src, target=tgt)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))  # no src file created
    patch_embedder(fake_embedder)

    result = runner.invoke(app, ["connect", "accept", sid[:8], "--write"])
    assert result.exit_code == 1
    # Status stayed pending — the failed write did not freeze the suggestion.
    row = test_db.execute(
        "SELECT status FROM link_suggestions WHERE id = %s::uuid", (sid,)
    ).fetchone()
    assert row is not None
    assert row[0] == "pending"


def test_load_action_context_does_not_mutate(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="a", vault_path="notes/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="b", vault_path="notes/b.md"
    )
    sid = _insert_suggestion(test_db, source=a, target=b)
    ctx = connect_mod.load_action_context(test_db, sid)
    assert ctx.status == "pending"  # current status, unchanged
    assert ctx.source_vault_path == "notes/a.md"
    assert ctx.target_vault_path == "notes/b.md"
    assert ctx.target_title == "Doc B"
    # Reading context did not flip the row.
    row = test_db.execute(
        "SELECT status FROM link_suggestions WHERE id = %s::uuid", (sid,)
    ).fetchone()
    assert row is not None and row[0] == "pending"


# --------------------------------------------------------------------------- #
# Undirected-pair behavior (migration 022 — mirror-pair redundancy fix).
# --------------------------------------------------------------------------- #


def _pair_rows(
    conn: psycopg.Connection, a: str, b: str
) -> list[tuple[str, str, str]]:
    """Return ``(source, target, status)`` for every row touching the {a,b} pair."""
    rows = conn.execute(
        "SELECT source_doc_id::text, target_doc_id::text, status "
        "FROM link_suggestions "
        "WHERE (source_doc_id = %s::uuid AND target_doc_id = %s::uuid) "
        "   OR (source_doc_id = %s::uuid AND target_doc_id = %s::uuid)",
        (a, b, b, a),
    ).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]


def test_refresh_writes_single_row_per_unordered_pair(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # THE HEADLINE BUG: an overlapping A/B pair is eligible from BOTH source
    # docs, but only ONE undirected row may persist (not A→B AND B→A).
    a, b = _seed_overlapping_pair(test_db, fake_embedder)
    connect_mod.refresh_suggestions(test_db, _cfg())
    rows = _pair_rows(test_db, a, b)
    assert len(rows) == 1  # exactly one row for the unordered pair {a, b}
    assert rows[0][2] == "pending"


def test_unordered_unique_index_blocks_direct_mirror_insert(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # DB-level enforcement: with A→B stored, a direct B→A insert must violate the
    # unordered-pair unique index (migration 022).
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md"
    )
    _insert_suggestion(test_db, source=a, target=b)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_suggestion(test_db, source=b, target=a)
    test_db.rollback()  # clear the aborted-transaction state


def test_upsert_keeps_better_scoring_orientation(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # Upserting the reverse orientation with a STRICTLY higher score flips the
    # stored orientation + leg scores; a worse score leaves it untouched.
    a = _make_vault_doc(
        test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md"
    )
    b = _make_vault_doc(
        test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md"
    )
    connect_mod._upsert_suggestion(
        test_db, source_doc_id=a, target_doc_id=b, score=0.40,
        graph_score=0.40, embed_score=None,
    )
    connect_mod._upsert_suggestion(
        test_db, source_doc_id=b, target_doc_id=a, score=0.60,
        graph_score=0.55, embed_score=0.65,
    )
    rows = _pair_rows(test_db, a, b)
    assert len(rows) == 1
    assert rows[0][:2] == (b, a)  # flipped to the better-scoring orientation
    stored = test_db.execute(
        "SELECT score, graph_score, embed_score FROM link_suggestions "
        "WHERE source_doc_id = %s::uuid AND target_doc_id = %s::uuid",
        (b, a),
    ).fetchone()
    assert stored is not None
    assert stored[0] == pytest.approx(0.60)
    assert stored[1] == pytest.approx(0.55)
    assert stored[2] == pytest.approx(0.65)

    # A worse score in the original orientation does NOT flip it back.
    connect_mod._upsert_suggestion(
        test_db, source_doc_id=a, target_doc_id=b, score=0.50,
        graph_score=0.50, embed_score=None,
    )
    rows_after = _pair_rows(test_db, a, b)
    assert len(rows_after) == 1
    assert rows_after[0][:2] == (b, a)  # unchanged — 0.50 < stored 0.60


def test_accept_then_refresh_does_not_resuggest_mirror(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # Accepting one orientation decides the pair; refresh must NOT resurrect the
    # reverse orientation as a new pending row.
    a, b = _seed_overlapping_pair(test_db, fake_embedder)
    connect_mod.refresh_suggestions(test_db, _cfg())
    row = test_db.execute(
        "SELECT id::text FROM link_suggestions LIMIT 1"
    ).fetchone()
    assert row is not None
    connect_mod.set_suggestion_status(test_db, str(row[0]), "accepted")
    connect_mod.refresh_suggestions(test_db, _cfg())
    rows = _pair_rows(test_db, a, b)
    assert len(rows) == 1  # still one row...
    assert rows[0][2] == "accepted"  # ...and it stayed decided (no pending mirror)


def test_reject_then_refresh_does_not_resuggest_mirror(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    a, b = _seed_overlapping_pair(test_db, fake_embedder)
    connect_mod.refresh_suggestions(test_db, _cfg())
    row = test_db.execute(
        "SELECT id::text FROM link_suggestions LIMIT 1"
    ).fetchone()
    assert row is not None
    connect_mod.set_suggestion_status(test_db, str(row[0]), "rejected")
    connect_mod.refresh_suggestions(test_db, _cfg())
    rows = _pair_rows(test_db, a, b)
    assert len(rows) == 1
    assert rows[0][2] == "rejected"  # no pending mirror appeared


def test_refresh_dedup_links_undirected(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # A wikilink in `links` (directed A→B) must suppress the suggestion in BOTH
    # orientations — the pair is undirected for review purposes.
    a, b = _seed_overlapping_pair(test_db, fake_embedder)
    test_db.execute(
        "INSERT INTO links (src_document_id, dst_document_id, link_text, link_kind) "
        "VALUES (%s::uuid, %s::uuid, %s, %s)",
        (a, b, "Doc B", "wiki"),
    )
    connect_mod.refresh_suggestions(test_db, _cfg())
    assert _pair_rows(test_db, a, b) == []  # neither A→B nor B→A suggested


def test_refresh_retires_pending_when_linked_reverse_orientation(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # A pending suggestion stored as A→B is retired when the user later draws the
    # wikilink in the REVERSE orientation (B→A) — `links` dedup is undirected.
    a, b = _seed_overlapping_pair(test_db, fake_embedder)
    connect_mod.refresh_suggestions(test_db, _cfg())
    row = test_db.execute(
        "SELECT source_doc_id::text, target_doc_id::text FROM link_suggestions "
        "WHERE status = 'pending' LIMIT 1"
    ).fetchone()
    assert row is not None
    src, tgt = str(row[0]), str(row[1])
    # Draw the wikilink in the opposite orientation to the stored suggestion.
    test_db.execute(
        "INSERT INTO links (src_document_id, dst_document_id, link_text, link_kind) "
        "VALUES (%s::uuid, %s::uuid, %s, %s)",
        (tgt, src, "manual", "wiki"),  # reverse of the stored (src, tgt)
    )
    connect_mod.refresh_suggestions(test_db, _cfg())
    assert _pair_rows(test_db, src, tgt) == []  # retired despite reverse linkage


def test_partial_refresh_retires_pending_scoped_by_target_endpoint(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> None:
    # Codex review: _retire_linked_pending must scope by EITHER endpoint. A
    # pending row stored as src->tgt, linked in reverse (tgt->src), must be
    # retired by a PARTIAL `refresh --doc <tgt>` even though tgt is the row's
    # TARGET (not its source) — otherwise the stale row lingers because scoring
    # the linked source suppresses any new upsert.
    a, b = _seed_overlapping_pair(test_db, fake_embedder)
    connect_mod.refresh_suggestions(test_db, _cfg())
    row = test_db.execute(
        "SELECT source_doc_id::text, target_doc_id::text FROM link_suggestions "
        "WHERE status = 'pending' LIMIT 1"
    ).fetchone()
    assert row is not None
    src, tgt = str(row[0]), str(row[1])
    # Link the reverse orientation; the pair is now connected.
    test_db.execute(
        "INSERT INTO links (src_document_id, dst_document_id, link_text, link_kind) "
        "VALUES (%s::uuid, %s::uuid, %s, %s)",
        (tgt, src, "manual", "wiki"),
    )
    # Refresh ONLY the stored target doc — the stored source is out of scope.
    connect_mod.refresh_suggestions(test_db, _cfg(), doc_prefix=tgt[:8])
    assert _pair_rows(test_db, src, tgt) == []  # retired by target-endpoint scope


def test_migration_022_dedup_keeps_better_and_preserves_decided(
    test_db: psycopg.Connection,
) -> None:
    # Lock the migration-022 cleanup ranking on a constructed legacy snapshot
    # (mirror rows can't be inserted into the live table once the unique index
    # exists, so simulate the pre-022 shape in an unconstrained temp table and
    # run the exact dedup SQL).
    # No ON COMMIT DROP: the test_db connection runs autocommit, so it would
    # drop the table immediately after creation. A plain session TEMP table is
    # auto-cleaned when the per-test connection closes at fixture teardown.
    test_db.execute(
        "CREATE TEMP TABLE _legacy_ls ("
        "  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
        "  source_doc_id uuid, target_doc_id uuid, score float, status text"
        ")"
    )
    a = "aaaaaaaa-0000-0000-0000-000000000000"
    b = "bbbbbbbb-0000-0000-0000-000000000000"
    c = "cccccccc-0000-0000-0000-000000000000"
    d = "dddddddd-0000-0000-0000-000000000000"
    # Pair {a,b}: two pending mirrors — keep the better-scoring (b→a, 0.80).
    test_db.execute(
        "INSERT INTO _legacy_ls (source_doc_id, target_doc_id, score, status) VALUES "
        "(%s,%s,0.50,'pending'),(%s,%s,0.80,'pending')",
        (a, b, b, a),
    )
    # Pair {c,d}: accepted c→d mirrored by a stale pending d→c — keep accepted.
    test_db.execute(
        "INSERT INTO _legacy_ls (source_doc_id, target_doc_id, score, status) VALUES "
        "(%s,%s,0.40,'accepted'),(%s,%s,0.90,'pending')",
        (c, d, d, c),
    )
    test_db.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY LEAST(source_doc_id, target_doc_id),
                                    GREATEST(source_doc_id, target_doc_id)
                       ORDER BY CASE status
                                    WHEN 'accepted' THEN 3
                                    WHEN 'rejected' THEN 2
                                    ELSE 1
                                END DESC,
                                score DESC,
                                id ASC
                   ) AS rn
            FROM _legacy_ls
        )
        DELETE FROM _legacy_ls WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """
    )
    survivors = {
        (str(r[0]), str(r[1]), str(r[2]))
        for r in test_db.execute(
            "SELECT source_doc_id::text, target_doc_id::text, status FROM _legacy_ls"
        ).fetchall()
    }
    assert survivors == {
        (b, a, "pending"),   # better-scoring pending orientation kept
        (c, d, "accepted"),  # decided row preserved over its stale pending mirror
    }
