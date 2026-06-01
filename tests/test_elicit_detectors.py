"""Tests for tacit-knowledge gap detectors."""

import pytest

from brain.elicit.detectors import DETECTOR_REGISTRY, DeltaDetector, OrphanEntityDetector


def _seed_entity_with_mentions(conn, *, name, entity_type, description, doc_kinds):
    """Insert one graph entity + N documents/mentions. doc_kinds: list of 'vault'/'ingested'."""
    eid = conn.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key, description, doc_count) "
        "VALUES ('default', %s, %s, %s, %s, %s) RETURNING id",
        (entity_type, name, name.lower(), description, len(doc_kinds)),
    ).fetchone()[0]
    for i, kind in enumerate(doc_kinds):
        did = conn.execute(
            "INSERT INTO documents (title, content, content_hash, content_type, kind) "
            "VALUES (%s, %s, %s, 'note', %s) RETURNING id",
            (f"{name} doc {i}", "body", f"{name}-{i}-hash", kind),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO graph_entity_mentions (tenant_id, entity_id, document_id, source) "
            "VALUES ('default', %s, %s, 'people')",
            (eid, did),
        )
    return str(eid)


def test_delta_detector_finds_ingested_only_entities(test_db):
    _seed_entity_with_mentions(
        test_db,
        name="Acme",
        entity_type="org",
        description="x",
        doc_kinds=["ingested", "ingested", "ingested"],
    )
    _seed_entity_with_mentions(
        test_db,
        name="Beta",
        entity_type="org",
        description="x",
        doc_kinds=["ingested", "vault", "ingested"],
    )
    gaps = DeltaDetector().detect(test_db, tenant_id="default", limit=10)
    assert len(gaps) == 1
    assert gaps[0].signal_kind == "delta"
    assert gaps[0].target_type == "org"
    assert len(gaps[0].evidence_ids) == 3


def test_orphan_detector_finds_null_description_high_mention(test_db):
    _seed_entity_with_mentions(
        test_db,
        name="Gamma",
        entity_type="topic",
        description=None,
        doc_kinds=["ingested", "ingested", "ingested"],
    )
    _seed_entity_with_mentions(
        test_db,
        name="Delta",
        entity_type="topic",
        description="well described",
        doc_kinds=["ingested", "ingested", "ingested"],
    )
    gaps = OrphanEntityDetector().detect(test_db, tenant_id="default", limit=10)
    assert len(gaps) == 1
    assert gaps[0].signal_kind == "orphan"


def test_registry_contains_all_four():
    assert set(DETECTOR_REGISTRY) == {"delta", "orphan", "contradiction", "user_flagged"}
