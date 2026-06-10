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
    sid = _insert_suggestion(test_db, source=a, target=b)
    patch_embedder(fake_embedder)
    accept = runner.invoke(app, ["connect", "accept", sid[:8]])
    assert accept.exit_code == 0
    assert "accepted" in accept.stdout
    status = test_db.execute(
        "SELECT status FROM link_suggestions WHERE id = %s::uuid", (sid,)
    ).fetchone()
    assert status is not None and status[0] == "accepted"

    sid2 = _insert_suggestion(test_db, source=b, target=a)
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
