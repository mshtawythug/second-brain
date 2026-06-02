"""Tests for tacit-knowledge gap detectors."""

import time

from brain.elicit.detectors import (
    DETECTOR_REGISTRY,
    ContradictionDetector,
    DeltaDetector,
    OrphanEntityDetector,
)


def _seed_entity_with_mentions(conn, *, name, entity_type, description, doc_kinds):
    """Insert one graph entity + N documents/mentions. doc_kinds: list of 'vault'/'ingested'."""
    eid = conn.execute(
        "INSERT INTO graph_entities "
        "(tenant_id, entity_type, name, canonical_key, description, doc_count) "
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


def _seed_entity_with_summaries(
    conn, *, name, entity_type, doc_count, summaries, tenant_id="default"
):
    """Insert one graph entity whose doc_count is set explicitly, plus documents with summaries.

    ``summaries`` is a list of strings (or None entries for docs without a summary).
    ``doc_count`` is stored verbatim on graph_entities so SQL filters on it work as expected.
    """
    eid = conn.execute(
        "INSERT INTO graph_entities "
        "(tenant_id, entity_type, name, canonical_key, description, doc_count) "
        "VALUES (%s, %s, %s, %s, NULL, %s) RETURNING id",
        (tenant_id, entity_type, name, name.lower(), doc_count),
    ).fetchone()[0]
    doc_ids = []
    for i, summary in enumerate(summaries):
        did = conn.execute(
            "INSERT INTO documents (title, content, content_hash, content_type, kind, summary) "
            "VALUES (%s, %s, %s, 'note', 'ingested', %s) RETURNING id",
            (f"{name}-doc-{i}", "body text", f"{name}-sum-{i}", summary),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO graph_entity_mentions "
            "(tenant_id, entity_id, document_id, source) "
            "VALUES (%s, %s, %s, 'people')",
            (tenant_id, eid, did),
        )
        doc_ids.append(str(did))
    return str(eid), doc_ids


# ---------------------------------------------------------------------------
# Fake enrichers — no HTTP calls
# ---------------------------------------------------------------------------


class _FakeContraEnricher:
    """Hand-written fake: always returns a fixed verdict without calling Ollama."""

    def __init__(self, contradicts: bool) -> None:
        self._c = contradicts
        self.call_count = 0

    def assess_contradiction(self, *, subject: str, summaries: list):  # noqa: ARG002
        from brain.enrichment import ContradictionVerdict

        self.call_count += 1
        return ContradictionVerdict(contradicts=self._c, rationale="opposing decisions")


class _RaisingEnricher:
    """Hand-written fake that raises if assess_contradiction is ever called."""

    def assess_contradiction(self, **_kwargs):
        raise AssertionError("assess_contradiction should NOT have been called")


# ---------------------------------------------------------------------------
# Pre-existing tests (unchanged)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# ContradictionDetector — Wave 4 tests
# ---------------------------------------------------------------------------


def test_contradiction_disabled_returns_empty(test_db):
    """Detector with enabled=False must return [] without touching the DB or enricher."""
    detector = ContradictionDetector(enabled=False)
    result = detector.detect(test_db, tenant_id="default", limit=10)
    assert result == []


def test_contradiction_no_enricher_returns_empty(test_db):
    """Detector with enabled=True but enricher=None must also return []."""
    detector = ContradictionDetector(enabled=True, enricher=None, min_docs=1)
    result = detector.detect(test_db, tenant_id="default", limit=10)
    assert result == []


def test_contradiction_detects_opposing_summaries(test_db):
    """When enricher says contradicts=True, detect() returns one gap with correct fields."""
    # Arrange: one entity with doc_count >= min_docs and two summarised docs.
    eid, doc_ids = _seed_entity_with_summaries(
        test_db,
        name="TestTopic",
        entity_type="topic",
        doc_count=2,
        summaries=["We decided to adopt approach A.", "We reversed course and adopted approach B."],
    )
    fake = _FakeContraEnricher(contradicts=True)
    detector = ContradictionDetector(enabled=True, enricher=fake, min_docs=1)

    # Act
    gaps = detector.detect(test_db, tenant_id="default", limit=10)

    # Assert
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.signal_kind == "contradiction"
    assert gap.target_id == eid
    assert gap.target_type == "topic"
    assert set(gap.evidence_ids) == set(doc_ids)
    assert "TestTopic" in gap.rationale
    assert "opposing decisions" in gap.rationale
    assert fake.call_count == 1


def test_contradiction_no_gap_when_not_contradicting(test_db):
    """When the fake enricher says contradicts=False, detect() returns []."""
    _seed_entity_with_summaries(
        test_db,
        name="ConsistentTopic",
        entity_type="topic",
        doc_count=2,
        summaries=["Summary A about consistent approach.", "Summary B about consistent approach."],
    )
    fake = _FakeContraEnricher(contradicts=False)
    detector = ContradictionDetector(enabled=True, enricher=fake, min_docs=1)

    gaps = detector.detect(test_db, tenant_id="default", limit=10)
    assert gaps == []
    assert fake.call_count == 1


def test_contradiction_skips_when_summaries_sparse(test_db):
    """Entity with doc_count >= min_docs but only 1 non-null summary → no LLM call, no gaps."""
    # One doc has a summary, one has NULL — HAVING count(summary) >= 2 must exclude this.
    _seed_entity_with_summaries(
        test_db,
        name="SparseEntity",
        entity_type="org",
        doc_count=5,
        summaries=["Only one summary.", None],
    )
    raising = _RaisingEnricher()
    detector = ContradictionDetector(enabled=True, enricher=raising, min_docs=1)

    # Must return [] without ever calling assess_contradiction.
    gaps = detector.detect(test_db, tenant_id="default", limit=10)
    assert gaps == []


def test_contradiction_skips_below_min_docs(test_db):
    """Entity whose doc_count < min_docs is excluded before any LLM call."""
    _seed_entity_with_summaries(
        test_db,
        name="ThinEntity",
        entity_type="topic",
        doc_count=1,
        summaries=["Summary one.", "Summary two."],
    )
    raising = _RaisingEnricher()
    # min_docs=5 means doc_count=1 doesn't qualify.
    detector = ContradictionDetector(enabled=True, enricher=raising, min_docs=5)

    gaps = detector.detect(test_db, tenant_id="default", limit=10)
    assert gaps == []


def test_contradiction_perf_guard(test_db):
    """Seeding ~200 entities with 2 summarised docs each; fake enricher returns False.

    detect() must complete well under 30 seconds even for a larger entity roster.
    """
    # Arrange: 200 entities, each with 2 summarised docs and doc_count=2.
    fake = _FakeContraEnricher(contradicts=False)
    for i in range(200):
        _seed_entity_with_summaries(
            test_db,
            name=f"PerfEntity{i:03d}",
            entity_type="topic",
            doc_count=2,
            summaries=[f"Summary alpha {i}.", f"Summary beta {i}."],
        )

    detector = ContradictionDetector(enabled=True, enricher=fake, min_docs=1)

    # Act + time (using time.monotonic per task spec — NOT in src)
    start = time.monotonic()
    gaps = detector.detect(test_db, tenant_id="default", limit=200)
    elapsed = time.monotonic() - start

    # Assert: no contradictions (fake always returns False), runs in < 30s.
    assert gaps == []
    assert fake.call_count == 200
    assert elapsed < 30.0, f"detect() took {elapsed:.2f}s — exceeded 30s budget"
