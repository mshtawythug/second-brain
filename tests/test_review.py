"""Unit + integration tests for ``brain review scan`` (Plan 03).

Covers the pure scan logic (``_cosine`` / ``_top_pairs`` / ``ReviewFinding``),
the SQL helpers in ``brain.review.queries`` against the real Postgres fixture,
and the conflict / staleness orchestrators with fake enrichers (never live
Ollama). Config parsing + migration 018 are also exercised here.
"""
from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import FrozenInstanceError, replace

import psycopg
import pytest

from brain.config import Config, ConfigError
from brain.enrichment import ContradictionVerdict
from brain.errors import OllamaUnavailable, ReviewError
from brain.review import queries
from brain.review.scans import (
    ReviewFinding,
    _cosine,
    _top_pairs,
    run_conflict_scan,
    run_staleness_scan,
)

_DIM = 4096
_TENANT = "default"


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------
def _pad(values: Sequence[float], dim: int = _DIM) -> list[float]:
    """Right-pad ``values`` with zeros to ``dim`` (the test schema's vector dim)."""
    out = list(values)
    if len(out) < dim:
        out.extend([0.0] * (dim - len(out)))
    return out[:dim]


def _insert_doc(
    conn: psycopg.Connection,
    *,
    title: str,
    summary: str | None,
    content_type: str = "note",
    kind: str = "ingested",
    draft: bool = False,
    ingested_days_ago: int = 0,
    embedding: Sequence[float] | None = None,
) -> str:
    """Insert one document (+ optional lead chunk) and return its UUID string."""
    doc_id = conn.execute(
        """
        INSERT INTO documents
            (title, content, content_hash, content_type, kind, summary,
             draft, ingested_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s,
                now() - make_interval(days => %s))
        RETURNING id::text
        """,
        (
            title,
            f"body of {title}",
            uuid.uuid4().hex,
            content_type,
            kind,
            summary,
            draft,
            ingested_days_ago,
        ),
    ).fetchone()[0]
    if embedding is not None:
        conn.execute(
            "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
            "VALUES (%s::uuid, 0, %s, %s)",
            (doc_id, f"chunk of {title}", _pad(embedding)),
        )
    return doc_id


def _insert_entity(
    conn: psycopg.Connection,
    *,
    canonical_key: str,
    name: str,
    doc_ids: Sequence[str],
    entity_type: str = "project",
    doc_count: int | None = None,
    tenant: str = _TENANT,
) -> str:
    """Insert one graph entity + a mention row per doc; return the entity UUID."""
    eid = conn.execute(
        "INSERT INTO graph_entities "
        "(tenant_id, entity_type, name, canonical_key, doc_count) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id::text",
        (tenant, entity_type, name, canonical_key, doc_count or len(doc_ids)),
    ).fetchone()[0]
    for did in doc_ids:
        conn.execute(
            "INSERT INTO graph_entity_mentions "
            "(tenant_id, entity_id, document_id, source) "
            "VALUES (%s, %s::uuid, %s::uuid, 'people')",
            (tenant, eid, did),
        )
    return eid


def _insert_gap(
    conn: psycopg.Connection,
    *,
    signal_kind: str,
    target_type: str,
    target_id: str,
    status: str = "surfaced",
    score: float = 1.0,
    evidence_ids: Sequence[str] | None = None,
    rationale: str = "seed",
    tenant: str = _TENANT,
) -> str:
    """Insert one elicitation_gaps row directly; return its UUID string."""
    return conn.execute(
        """
        INSERT INTO elicitation_gaps
            (tenant_id, signal_kind, target_type, target_id, status,
             score, evidence_ids, rationale)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id::text
        """,
        (
            tenant,
            signal_kind,
            target_type,
            target_id,
            status,
            score,
            list(evidence_ids or []),
            rationale,
        ),
    ).fetchone()[0]


class _FakeEnricher:
    """Fake :class:`OllamaEnricher` — flags GO/STOP summary pairs, counts calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def assess_contradiction(
        self, *, subject: str, summaries: list[str]
    ) -> ContradictionVerdict:
        self.calls.append((subject, list(summaries)))
        joined = " ".join(summaries).upper()
        contradicts = "GO" in joined and "STOP" in joined
        return ContradictionVerdict(
            contradicts=contradicts,
            rationale="go vs stop" if contradicts else "no conflict",
        )


class _RaisingEnricher:
    """Returns one True verdict, then raises OllamaUnavailable on the next call."""

    def __init__(self) -> None:
        self.calls = 0

    def assess_contradiction(
        self, *, subject: str, summaries: list[str]
    ) -> ContradictionVerdict:
        self.calls += 1
        if self.calls == 1:
            return ContradictionVerdict(contradicts=True, rationale="first")
        raise OllamaUnavailable("ollama down")


def _cfg(**overrides: object) -> Config:
    """Load config and override fields (frozen dataclass)."""
    base = Config.load()
    return replace(base, **overrides)  # type: ignore[arg-type]


class _NullEmbedder:
    """Embedder stand-in — scans never call it (they read stored vectors)."""

    dim = _DIM

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        raise AssertionError("scan must not call embedder.embed")

    def count_tokens(self, text: str) -> int:
        return len(text)


# ---------------------------------------------------------------------------
# Pure-logic unit tests (no DB)
# ---------------------------------------------------------------------------
def test_pure_python_cosine() -> None:
    assert _cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)
    assert _cosine([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]) == pytest.approx(0.0)
    assert _cosine([0.0, 0.0, 0.0], [1.0, 2.0, 3.0]) == 0.0
    assert _cosine([1.0, 0.0], [0.0, 0.0]) == 0.0
    # Known angle: cosine of [1,0] and [0.7, sqrt(1-0.49)] is 0.7.
    assert _cosine([1.0, 0.0], [0.7, math.sqrt(0.51)]) == pytest.approx(0.7)


def test_pairwise_sim_above_floor() -> None:
    embeddings = {
        "a": [1.0, 0.0],
        "b": [0.99, 0.01],  # ~1.0 cosine vs a -> above floor
        "c": [0.0, 1.0],  # orthogonal to a/b -> below floor
    }
    pairs = _top_pairs(["a", "b", "c"], embeddings, sim_floor=0.4, max_pairs=5)
    kept = {frozenset((x, y)) for x, y, _ in pairs}
    assert frozenset(("a", "b")) in kept
    assert frozenset(("a", "c")) not in kept
    assert frozenset(("b", "c")) not in kept


def test_pairwise_sim_empty_docs() -> None:
    assert _top_pairs([], {}, sim_floor=0.4, max_pairs=3) == []
    # Docs without embeddings are silently skipped (no crash).
    assert _top_pairs(["x", "y"], {}, sim_floor=0.4, max_pairs=3) == []


def test_top_pairs_respects_max() -> None:
    embeddings = {k: [1.0, 0.01 * i] for i, k in enumerate("abcd")}
    pairs = _top_pairs(list("abcd"), embeddings, sim_floor=0.0, max_pairs=2)
    assert len(pairs) == 2
    # Sorted descending by cosine.
    assert pairs[0][2] >= pairs[1][2]


def test_review_finding_frozen() -> None:
    finding = ReviewFinding(
        kind="stale",
        target_type="doc",
        target_id="d1",
        score=0.7,
        rationale="r",
        evidence_ids=["d1", "d2"],
    )
    with pytest.raises(FrozenInstanceError):
        finding.score = 0.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------
def test_config_review_defaults() -> None:
    cfg = Config.load()
    assert cfg.review_conflict_limit == 30
    assert cfg.review_conflict_pairs_per_entity == 3
    assert cfg.review_embed_sim_floor == pytest.approx(0.40)
    assert cfg.review_stale_age_days == 365
    assert cfg.review_stale_supersede_window_days == 90
    assert cfg.review_stale_sim_floor == pytest.approx(0.60)
    assert cfg.review_stale_limit == 200


def test_config_review_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_REVIEW_CONFLICT_LIMIT", "10")
    monkeypatch.setenv("BRAIN_REVIEW_EMBED_SIM_FLOOR", "0.55")
    monkeypatch.setenv("BRAIN_REVIEW_STALE_AGE_DAYS", "100")
    cfg = Config.load()
    assert cfg.review_conflict_limit == 10
    assert cfg.review_embed_sim_floor == pytest.approx(0.55)
    assert cfg.review_stale_age_days == 100


def test_config_review_invalid_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_REVIEW_CONFLICT_LIMIT", "0")
    with pytest.raises(ConfigError, match="BRAIN_REVIEW_CONFLICT_LIMIT"):
        Config.load()


def test_config_review_invalid_float(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_REVIEW_EMBED_SIM_FLOOR", "1.5")
    with pytest.raises(ConfigError, match="BRAIN_REVIEW_EMBED_SIM_FLOOR"):
        Config.load()


def test_config_review_float_not_a_number(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_REVIEW_STALE_SIM_FLOOR", "high")
    with pytest.raises(ConfigError, match="BRAIN_REVIEW_STALE_SIM_FLOOR"):
        Config.load()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
def test_review_error_carries_partial() -> None:
    err = ReviewError("boom", findings=[1, 2], processed=2, total=5)
    assert err.findings == [1, 2]
    assert err.processed == 2
    assert err.total == 5


# ---------------------------------------------------------------------------
# Migration 018 — signal_kind superset
# ---------------------------------------------------------------------------
def test_migration_018_allows_stale_and_search_failure(
    test_db: psycopg.Connection,
) -> None:
    for kind in ("stale", "search_failure"):
        _insert_gap(
            test_db, signal_kind=kind, target_type="doc", target_id=f"t-{kind}"
        )
    rows = test_db.execute(
        "SELECT count(*) FROM elicitation_gaps WHERE signal_kind IN "
        "('stale','search_failure')"
    ).fetchone()[0]
    assert rows == 2


def test_migration_018_rejects_unknown_signal_kind(
    test_db: psycopg.Connection,
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_gap(
            test_db, signal_kind="bogus", target_type="doc", target_id="x"
        )


# ---------------------------------------------------------------------------
# queries.py — SQL helpers
# ---------------------------------------------------------------------------
def test_iter_entities_skips_no_summary(test_db: psycopg.Connection) -> None:
    summarized = _insert_doc(test_db, title="A", summary="s1")
    no_summary = _insert_doc(test_db, title="B", summary=None)
    _insert_entity(
        test_db,
        canonical_key="ent-mixed",
        name="Mixed",
        doc_ids=[summarized, no_summary],
    )
    cands = queries.iter_entities_for_conflict_scan(
        test_db, tenant_id=_TENANT, min_docs=2, limit=30
    )
    # Only one summarized doc counts -> below the min_docs=2 threshold.
    assert cands == []


def test_iter_entities_returns_candidate(test_db: psycopg.Connection) -> None:
    d1 = _insert_doc(test_db, title="A", summary="s1")
    d2 = _insert_doc(test_db, title="B", summary="s2")
    _insert_entity(
        test_db, canonical_key="ent-two", name="Two", doc_ids=[d1, d2]
    )
    cands = queries.iter_entities_for_conflict_scan(
        test_db, tenant_id=_TENANT, min_docs=2, limit=30
    )
    assert len(cands) == 1
    assert cands[0].canonical_key == "ent-two"
    assert set(cands[0].doc_ids) == {d1, d2}


def test_fetch_best_chunk_embeddings_lead_chunk(test_db: psycopg.Connection) -> None:
    doc_id = _insert_doc(test_db, title="E", summary="s", embedding=[1.0, 2.0, 3.0])
    # A second chunk at index 1 must be ignored (lead chunk is index 0).
    test_db.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
        "VALUES (%s::uuid, 1, 'second', %s)",
        (doc_id, _pad([9.0, 9.0, 9.0])),
    )
    embs = queries.fetch_best_chunk_embeddings(test_db, document_ids=[doc_id])
    assert embs[doc_id][:3] == [1.0, 2.0, 3.0]


def test_fetch_best_chunk_embeddings_empty() -> None:
    # No DB round-trip when there are no ids.
    assert (
        queries.fetch_best_chunk_embeddings(None, document_ids=[])  # type: ignore[arg-type]
        == {}
    )


def test_iter_docs_for_staleness_skips_young_and_transcript(
    test_db: psycopg.Connection,
) -> None:
    old = _insert_doc(
        test_db, title="Old note", summary="s", ingested_days_ago=400
    )
    young = _insert_doc(test_db, title="Young", summary="s", ingested_days_ago=10)
    transcript = _insert_doc(
        test_db,
        title="Old transcript",
        summary="s",
        content_type="transcript",
        ingested_days_ago=400,
    )
    no_summary = _insert_doc(
        test_db, title="Old no-summary", summary=None, ingested_days_ago=400
    )
    # All four participate in the tenant graph (the staleness scan is tenant-
    # scoped via graph_entity_mentions); only the WHERE filters should exclude.
    _insert_entity(
        test_db,
        canonical_key="stale-mix",
        name="Mix",
        doc_ids=[old, young, transcript, no_summary],
    )
    cands = queries.iter_docs_for_staleness_scan(
        test_db, tenant_id=_TENANT, stale_age_days=365, limit=200
    )
    ids = {c.doc_id for c in cands}
    assert ids == {old}
    assert cands[0].age_days >= 365


def test_iter_docs_for_staleness_excludes_other_tenant(
    test_db: psycopg.Connection,
) -> None:
    old = _insert_doc(test_db, title="Old", summary="s", ingested_days_ago=400)
    _insert_entity(
        test_db, canonical_key="t-a", name="A", doc_ids=[old], tenant="tenant-a"
    )
    # Scanning a different tenant must not surface tenant-a's doc.
    cands = queries.iter_docs_for_staleness_scan(
        test_db, tenant_id="tenant-b", stale_age_days=365, limit=200
    )
    assert cands == []


def test_count_stale_docs_missing_summary_tenant_scoped(
    test_db: psycopg.Connection,
) -> None:
    # An aged, no-summary doc mentioned only in tenant-a's graph is counted for
    # tenant-a but never leaks into tenant-b's skip-count.
    old = _insert_doc(
        test_db, title="Old no-summary", summary=None, ingested_days_ago=400
    )
    _insert_entity(
        test_db, canonical_key="t-c", name="C", doc_ids=[old], tenant="tenant-a"
    )
    assert (
        queries.count_stale_docs_missing_summary(
            test_db, tenant_id="tenant-a", stale_age_days=365
        )
        == 1
    )
    assert (
        queries.count_stale_docs_missing_summary(
            test_db, tenant_id="tenant-b", stale_age_days=365
        )
        == 0
    )


def test_upsert_then_list_and_dismiss(test_db: psycopg.Connection) -> None:
    queries.upsert_review_finding(
        test_db,
        tenant_id=_TENANT,
        signal_kind="stale",
        target_type="doc",
        target_id="doc-1",
        score=0.7,
        evidence_ids=["doc-1", "doc-2"],
        rationale="aged",
    )
    rows = queries.list_review_queue(
        test_db, tenant_id=_TENANT, signal_kinds=("contradiction", "stale"), limit=10
    )
    assert len(rows) == 1
    assert rows[0].signal_kind == "stale"
    assert rows[0].score == pytest.approx(0.7)

    finding_id = rows[0].id
    returned = queries.dismiss_review_finding(
        test_db, tenant_id=_TENANT, id_prefix=finding_id[:8]
    )
    assert returned == finding_id
    status = test_db.execute(
        "SELECT status FROM elicitation_gaps WHERE id = %s::uuid", (finding_id,)
    ).fetchone()[0]
    assert status == "dismissed"
    # Idempotent re-dismiss.
    assert (
        queries.dismiss_review_finding(
            test_db, tenant_id=_TENANT, id_prefix=finding_id[:8]
        )
        == finding_id
    )


def test_dismiss_no_match_raises(test_db: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="no review finding"):
        queries.dismiss_review_finding(
            test_db, tenant_id=_TENANT, id_prefix="ffffffff"
        )


def test_upsert_does_not_overwrite_dismissed(test_db: psycopg.Connection) -> None:
    _insert_gap(
        test_db,
        signal_kind="stale",
        target_type="doc",
        target_id="doc-x",
        status="dismissed",
        rationale="original",
    )
    queries.upsert_review_finding(
        test_db,
        tenant_id=_TENANT,
        signal_kind="stale",
        target_type="doc",
        target_id="doc-x",
        score=0.9,
        evidence_ids=["doc-x", "doc-y"],
        rationale="should-not-apply",
    )
    row = test_db.execute(
        "SELECT status, rationale FROM elicitation_gaps WHERE target_id = 'doc-x'"
    ).fetchone()
    assert row[0] == "dismissed"
    assert row[1] == "original"


def test_list_review_queue_excludes_elicit_kinds(test_db: psycopg.Connection) -> None:
    _insert_gap(test_db, signal_kind="delta", target_type="topic", target_id="t1")
    _insert_gap(test_db, signal_kind="stale", target_type="doc", target_id="d1")
    rows = queries.list_review_queue(
        test_db, tenant_id=_TENANT, signal_kinds=("contradiction", "stale"), limit=10
    )
    assert [r.signal_kind for r in rows] == ["stale"]


# ---------------------------------------------------------------------------
# Conflict scan — integration
# ---------------------------------------------------------------------------
def _seed_conflict_entity(conn: psycopg.Connection) -> tuple[str, str, str]:
    """Seed the synthetic go/stop/background trio + entity; return doc ids."""
    doc_go = _insert_doc(
        conn,
        title="Synthetic initiative — Q4 decision",
        summary="Decided to GO ahead with the synthetic initiative in Q4.",
        embedding=[1.0, 0.1, 0.0],
    )
    doc_stop = _insert_doc(
        conn,
        title="Synthetic initiative — reversed Q4 decision",
        summary="Reversed the call and decided to STOP the synthetic initiative.",
        embedding=[1.0, 0.2, 0.0],
    )
    doc_bg = _insert_doc(
        conn,
        title="Synthetic initiative — background",
        summary="General background notes about the synthetic initiative team.",
        embedding=[1.0, 0.0, 0.1],
    )
    _insert_entity(
        conn,
        canonical_key="synthetic-initiative",
        name="Synthetic initiative",
        doc_ids=[doc_go, doc_stop, doc_bg],
    )
    return doc_go, doc_stop, doc_bg


def test_conflict_scan_writes_single_finding(test_db: psycopg.Connection) -> None:
    doc_go, doc_stop, _bg = _seed_conflict_entity(test_db)
    enricher = _FakeEnricher()
    cfg = _cfg(elicit_contradiction_min_docs=2)

    findings = run_conflict_scan(
        test_db, enricher, _NullEmbedder(), cfg, tenant_id=_TENANT
    )

    assert len(findings) == 1
    assert findings[0].kind == "contradiction"
    assert findings[0].target_id == "synthetic-initiative"
    assert set(findings[0].evidence_ids) == {doc_go, doc_stop}

    rows = test_db.execute(
        "SELECT count(*) FROM elicitation_gaps WHERE signal_kind = 'contradiction'"
    ).fetchone()[0]
    assert rows == 1


def test_conflict_scan_idempotent(test_db: psycopg.Connection) -> None:
    _seed_conflict_entity(test_db)
    cfg = _cfg(elicit_contradiction_min_docs=2)

    first = _FakeEnricher()
    run_conflict_scan(test_db, first, _NullEmbedder(), cfg, tenant_id=_TENANT)
    assert first.calls  # adjudicated at least once

    second = _FakeEnricher()
    findings = run_conflict_scan(
        test_db, second, _NullEmbedder(), cfg, tenant_id=_TENANT
    )
    # Already surfaced -> never re-adjudicated.
    assert second.calls == []
    assert findings == []


def test_conflict_scan_skips_dismissed(test_db: psycopg.Connection) -> None:
    _seed_conflict_entity(test_db)
    _insert_gap(
        test_db,
        signal_kind="contradiction",
        target_type="project",
        target_id="synthetic-initiative",
        status="dismissed",
        rationale="user dismissed",
    )
    cfg = _cfg(elicit_contradiction_min_docs=2)
    enricher = _FakeEnricher()

    findings = run_conflict_scan(
        test_db, enricher, _NullEmbedder(), cfg, tenant_id=_TENANT
    )
    assert enricher.calls == []
    assert findings == []
    row = test_db.execute(
        "SELECT status, rationale FROM elicitation_gaps "
        "WHERE target_id = 'synthetic-initiative'"
    ).fetchone()
    assert row == ("dismissed", "user dismissed")


def test_conflict_scan_dry_run_writes_nothing(test_db: psycopg.Connection) -> None:
    _seed_conflict_entity(test_db)
    cfg = _cfg(elicit_contradiction_min_docs=2)
    enricher = _FakeEnricher()

    findings = run_conflict_scan(
        test_db, enricher, _NullEmbedder(), cfg, tenant_id=_TENANT, dry_run=True
    )
    assert len(findings) == 1
    rows = test_db.execute(
        "SELECT count(*) FROM elicitation_gaps WHERE signal_kind = 'contradiction'"
    ).fetchone()[0]
    assert rows == 0


def test_conflict_scan_skips_no_summary_entity(test_db: psycopg.Connection) -> None:
    d1 = _insert_doc(test_db, title="N1", summary=None, embedding=[1.0, 0.0])
    d2 = _insert_doc(test_db, title="N2", summary=None, embedding=[1.0, 0.1])
    _insert_entity(
        test_db, canonical_key="no-summary-ent", name="NoSummary", doc_ids=[d1, d2]
    )
    cfg = _cfg(elicit_contradiction_min_docs=2)
    enricher = _FakeEnricher()
    findings = run_conflict_scan(
        test_db, enricher, _NullEmbedder(), cfg, tenant_id=_TENANT
    )
    assert findings == []
    assert enricher.calls == []


def test_conflict_scan_ollama_unavailable_partial(test_db: psycopg.Connection) -> None:
    # Two entities; the first yields a finding, the second triggers Ollama down.
    _seed_conflict_entity(test_db)
    e_go = _insert_doc(
        test_db, title="Other — go", summary="GO on the other thing", embedding=[1.0, 0.1]
    )
    e_stop = _insert_doc(
        test_db,
        title="Other — stop",
        summary="STOP the other thing",
        embedding=[1.0, 0.2],
    )
    _insert_entity(
        test_db, canonical_key="other-thing", name="Other thing", doc_ids=[e_go, e_stop]
    )
    cfg = _cfg(elicit_contradiction_min_docs=2)

    with pytest.raises(ReviewError) as excinfo:
        run_conflict_scan(
            test_db, _RaisingEnricher(), _NullEmbedder(), cfg, tenant_id=_TENANT
        )
    # The first finding was committed before the failure.
    assert excinfo.value.findings
    surfaced = test_db.execute(
        "SELECT count(*) FROM elicitation_gaps WHERE signal_kind = 'contradiction'"
    ).fetchone()[0]
    assert surfaced == 1


# ---------------------------------------------------------------------------
# Staleness scan — integration
# ---------------------------------------------------------------------------
def test_staleness_scan_writes_finding(test_db: psycopg.Connection) -> None:
    old = _insert_doc(
        test_db,
        title="Compensation ranges — synthetic role",
        summary="old comp",
        ingested_days_ago=400,
        embedding=[1.0, 0.0],
    )
    new = _insert_doc(
        test_db,
        title="Updated salary bands Q1 — synthetic",
        summary="new comp",
        ingested_days_ago=15,
        embedding=[0.7, math.sqrt(0.51)],  # cosine 0.70 vs old
    )
    _insert_entity(
        test_db, canonical_key="comp", name="Compensation", doc_ids=[old, new]
    )
    cfg = _cfg()

    findings = run_staleness_scan(test_db, _NullEmbedder(), cfg, tenant_id=_TENANT)

    assert len(findings) == 1
    assert findings[0].kind == "stale"
    assert findings[0].target_id == old
    assert findings[0].score == pytest.approx(0.70, abs=0.01)
    assert set(findings[0].evidence_ids) == {old, new}
    row = test_db.execute(
        "SELECT score FROM elicitation_gaps WHERE signal_kind = 'stale'"
    ).fetchone()
    assert row[0] == pytest.approx(0.70, abs=0.01)


def test_staleness_scan_below_floor_skipped(test_db: psycopg.Connection) -> None:
    old = _insert_doc(
        test_db, title="Old", summary="s", ingested_days_ago=400, embedding=[1.0, 0.0]
    )
    new = _insert_doc(
        test_db,
        title="New unrelated",
        summary="s",
        ingested_days_ago=15,
        embedding=[0.0, 1.0],  # orthogonal -> cosine 0 < 0.60 floor
    )
    _insert_entity(test_db, canonical_key="topic", name="Topic", doc_ids=[old, new])
    findings = run_staleness_scan(
        test_db, _NullEmbedder(), _cfg(), tenant_id=_TENANT
    )
    assert findings == []


def test_staleness_scan_idempotent(test_db: psycopg.Connection) -> None:
    old = _insert_doc(
        test_db, title="Old", summary="s", ingested_days_ago=400, embedding=[1.0, 0.0]
    )
    new = _insert_doc(
        test_db,
        title="New",
        summary="s",
        ingested_days_ago=15,
        embedding=[0.7, math.sqrt(0.51)],
    )
    _insert_entity(test_db, canonical_key="t2", name="T2", doc_ids=[old, new])
    cfg = _cfg()
    first = run_staleness_scan(test_db, _NullEmbedder(), cfg, tenant_id=_TENANT)
    assert len(first) == 1
    second = run_staleness_scan(test_db, _NullEmbedder(), cfg, tenant_id=_TENANT)
    assert second == []
