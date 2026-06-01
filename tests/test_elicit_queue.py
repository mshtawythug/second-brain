"""Tests for brain.elicit.queue — _rank_gaps RRF + build_queue upsert/read-back."""
from __future__ import annotations

import uuid

import psycopg
import pytest

from brain.elicit.queue import _rank_gaps, build_queue
from brain.elicit.schema import Gap


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _g(kind: str, tid: str, score: float, n_ev: int = 3) -> Gap:
    return Gap(
        gap_id=str(uuid.uuid4()),
        signal_kind=kind,  # type: ignore[arg-type]
        target_type="topic",
        target_id=tid,
        score=score,
        evidence_ids=[f"d{i}" for i in range(n_ev)],
        evidence_texts=[],
        rationale="r",
    )


# ---------------------------------------------------------------------------
# _rank_gaps: cross-signal RRF
# ---------------------------------------------------------------------------


def test_rank_merges_signals_by_rrf() -> None:
    """A target_id appearing in multiple signal lists is boosted to the top."""
    delta = [_g("delta", "a", 10.0), _g("delta", "b", 5.0)]
    orphan = [_g("orphan", "a", 2.0)]  # 'a' under two signals -> boosted
    ranked = _rank_gaps({"delta": delta, "orphan": orphan})
    assert ranked[0].target_id == "a"
    assert all(0.0 < g.score <= 1.0 for g in ranked)


def test_rank_single_signal_descending() -> None:
    """With one signal, gaps are returned in descending score order."""
    gaps = [_g("delta", "c", 1.0), _g("delta", "d", 3.0), _g("delta", "e", 2.0)]
    ranked = _rank_gaps({"delta": gaps})
    assert ranked[0].target_id == "d"  # highest raw score → rank 0 → best RRF
    assert ranked[-1].target_id == "c"


def test_rank_empty_returns_empty() -> None:
    assert _rank_gaps({}) == []
    assert _rank_gaps({"delta": []}) == []


def test_rank_scores_normalized_to_one() -> None:
    """Top gap always has score exactly 1.0 after normalization."""
    gaps = [_g("delta", "x", 5.0), _g("delta", "y", 2.0)]
    ranked = _rank_gaps({"delta": gaps})
    assert abs(ranked[0].score - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# build_queue: upsert + guarded read-back
# ---------------------------------------------------------------------------


def test_build_queue_excludes_resolved(test_db: psycopg.Connection) -> None:
    """Resolved gaps are not returned by build_queue."""
    test_db.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, status) "
        "VALUES ('default','delta','topic','x', 0.9, ARRAY['d1','d2','d3'], 'resolved')"
    )
    from brain.config import Config

    cfg = Config.load()
    gaps = build_queue(test_db, cfg=cfg, tenant_id="default", detectors=[], limit=10)
    assert all(g.target_id != "x" for g in gaps)


def test_build_queue_empty_detectors_returns_empty(test_db: psycopg.Connection) -> None:
    """With no detectors and an empty queue, build_queue returns []."""
    from brain.config import Config

    cfg = Config.load()
    gaps = build_queue(test_db, cfg=cfg, tenant_id="default", detectors=[], limit=10)
    assert gaps == []


def test_build_queue_upserts_and_returns_gaps(test_db: psycopg.Connection) -> None:
    """build_queue upserts detected gaps and returns the top-scored open ones."""
    from brain.config import Config
    from brain.elicit.detectors import DeltaDetector

    # Seed 3 ingested-only docs for entity "Widget" so DeltaDetector finds it
    eid = test_db.execute(
        "INSERT INTO graph_entities "
        "(tenant_id, entity_type, name, canonical_key, description, doc_count) "
        "VALUES ('default', 'org', 'Widget', 'widget', 'desc', 3) RETURNING id"
    ).fetchone()[0]  # type: ignore[index]
    for i in range(3):
        did = test_db.execute(
            "INSERT INTO documents (title, content, content_hash, content_type, kind) "
            "VALUES (%s, 'body', %s, 'note', 'ingested') RETURNING id",
            (f"Widget doc {i}", f"widget-{i}-bq-hash"),
        ).fetchone()[0]  # type: ignore[index]
        test_db.execute(
            "INSERT INTO graph_entity_mentions "
            "(tenant_id, entity_id, document_id, source) "
            "VALUES ('default', %s, %s, 'people')",
            (eid, did),
        )

    cfg = Config.load()
    gaps = build_queue(
        test_db,
        cfg=cfg,
        tenant_id="default",
        detectors=[DeltaDetector()],
        limit=10,
    )
    assert len(gaps) >= 1
    assert gaps[0].signal_kind == "delta"
    # Confirm row landed in the DB
    count = test_db.execute("SELECT count(*) FROM elicitation_gaps").fetchone()
    assert count is not None and count[0] >= 1
