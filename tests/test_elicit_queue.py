"""Tests for brain.elicit.queue — build_queue upsert, RRF, and guards."""
from __future__ import annotations

import os

import psycopg
import pytest

from brain.config import Config
from brain.elicit.queue import BuildQueueResult, build_queue

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _make_cfg(**overrides: object) -> Config:
    """Minimal Config for queue tests — uses test DB URL and default elicit knobs."""
    return Config(database_url=TEST_DATABASE_URL, **overrides)  # type: ignore[arg-type]


def _seed_entity(
    conn: psycopg.Connection,
    *,
    name: str,
    entity_type: str,
    description: str | None,
    doc_kinds: list[str],
) -> str:
    """Insert a graph entity with N document mentions; return entity id string."""
    eid = conn.execute(
        "INSERT INTO graph_entities "
        "(tenant_id, entity_type, name, canonical_key, description, doc_count) "
        "VALUES ('default', %s, %s, %s, %s, %s) RETURNING id",
        (entity_type, name, name.lower(), description, len(doc_kinds)),
    ).fetchone()[0]  # type: ignore[index]
    for i, kind in enumerate(doc_kinds):
        did = conn.execute(
            "INSERT INTO documents (title, content, content_hash, content_type, kind) "
            "VALUES (%s, %s, %s, 'note', %s) RETURNING id",
            (f"{name} doc {i}", "body", f"{name}-{i}-q-hash", kind),
        ).fetchone()[0]  # type: ignore[index]
        conn.execute(
            "INSERT INTO graph_entity_mentions "
            "(tenant_id, entity_id, document_id, source) "
            "VALUES ('default', %s, %s, 'people')",
            (eid, did),
        )
    return str(eid)


# ---------------------------------------------------------------------------
# Happy-path: gaps are inserted
# ---------------------------------------------------------------------------


def test_build_queue_inserts_delta_gaps(test_db: psycopg.Connection) -> None:
    """Entities referenced only in ingested docs produce delta gaps in the queue."""
    _seed_entity(
        test_db,
        name="Acme",
        entity_type="org",
        description="x",
        doc_kinds=["ingested", "ingested", "ingested"],
    )
    cfg = _make_cfg(elicit_min_evidence_docs=3, elicit_min_gap_score=0.0)
    result = build_queue(test_db, cfg=cfg, tenant_id="default")

    assert result.inserted == 1
    assert result.updated == 0

    row = test_db.execute(
        "SELECT signal_kind, target_type, score FROM elicitation_gaps "
        "WHERE status = 'surfaced' ORDER BY score DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[0] == "delta"
    assert row[1] == "org"
    assert row[2] > 0  # RRF score stored


def test_build_queue_returns_result_type(test_db: psycopg.Connection) -> None:
    """build_queue always returns a BuildQueueResult dataclass."""
    cfg = _make_cfg()
    result = build_queue(test_db, cfg=cfg)
    assert isinstance(result, BuildQueueResult)


# ---------------------------------------------------------------------------
# Evidence guard: gaps with too few docs are skipped
# ---------------------------------------------------------------------------


def test_build_queue_skips_low_evidence(test_db: psycopg.Connection) -> None:
    """Entities with fewer evidence docs than min_evidence_docs are skipped."""
    # 2 docs < min_evidence_docs=3 → skipped
    _seed_entity(
        test_db,
        name="Tiny",
        entity_type="org",
        description="x",
        doc_kinds=["ingested", "ingested"],
    )
    cfg = _make_cfg(elicit_min_evidence_docs=3, elicit_min_gap_score=0.0)
    result = build_queue(test_db, cfg=cfg)

    assert result.inserted == 0
    assert result.skipped >= 1
    count = test_db.execute("SELECT count(*) FROM elicitation_gaps").fetchone()
    assert count is not None and count[0] == 0


# ---------------------------------------------------------------------------
# Score guard: gaps below min_gap_score are skipped
# ---------------------------------------------------------------------------


def test_build_queue_skips_low_score(test_db: psycopg.Connection) -> None:
    """Entities whose raw score is below min_gap_score are filtered out."""
    # doc_count=3 → raw score 3.0; require raw score >= 10.0 → skipped
    _seed_entity(
        test_db,
        name="Moderate",
        entity_type="org",
        description="x",
        doc_kinds=["ingested", "ingested", "ingested"],
    )
    cfg = _make_cfg(elicit_min_evidence_docs=1, elicit_min_gap_score=10.0)
    result = build_queue(test_db, cfg=cfg)

    assert result.inserted == 0
    count = test_db.execute("SELECT count(*) FROM elicitation_gaps").fetchone()
    assert count is not None and count[0] == 0


# ---------------------------------------------------------------------------
# Upsert: second run updates, not inserts
# ---------------------------------------------------------------------------


def test_build_queue_updates_existing_gap(test_db: psycopg.Connection) -> None:
    """A second build_queue run updates an existing surfaced gap instead of inserting."""
    _seed_entity(
        test_db,
        name="Persistent",
        entity_type="org",
        description="x",
        doc_kinds=["ingested", "ingested", "ingested"],
    )
    cfg = _make_cfg(elicit_min_evidence_docs=3, elicit_min_gap_score=0.0)

    r1 = build_queue(test_db, cfg=cfg)
    assert r1.inserted == 1
    assert r1.updated == 0

    r2 = build_queue(test_db, cfg=cfg)
    assert r2.inserted == 0
    assert r2.updated == 1

    # Only one row should exist in the queue
    count = test_db.execute(
        "SELECT count(*) FROM elicitation_gaps WHERE status = 'surfaced'"
    ).fetchone()
    assert count is not None and count[0] == 1


# ---------------------------------------------------------------------------
# signal_kinds filter: limit to specific detectors
# ---------------------------------------------------------------------------


def test_build_queue_signal_kinds_filter(test_db: psycopg.Connection) -> None:
    """Passing signal_kinds limits which detectors run."""
    _seed_entity(
        test_db,
        name="FilterMe",
        entity_type="org",
        description=None,
        doc_kinds=["ingested", "ingested", "ingested"],
    )
    cfg = _make_cfg(elicit_min_evidence_docs=3, elicit_min_gap_score=0.0)

    # Only run orphan — should produce an orphan gap (no description)
    result = build_queue(test_db, cfg=cfg, signal_kinds=["orphan"])
    assert result.inserted == 1

    row = test_db.execute(
        "SELECT signal_kind FROM elicitation_gaps WHERE status = 'surfaced'"
    ).fetchone()
    assert row is not None and row[0] == "orphan"


# ---------------------------------------------------------------------------
# Contradiction stub: disabled by default, no errors raised
# ---------------------------------------------------------------------------


def test_build_queue_contradiction_disabled_by_default(test_db: psycopg.Connection) -> None:
    """ContradictionDetector returns empty list when disabled; no exception raised."""
    cfg = _make_cfg(elicit_contradiction_enabled=False)
    result = build_queue(test_db, cfg=cfg, signal_kinds=["contradiction"])
    assert result.inserted == 0
    assert result.skipped == 0
