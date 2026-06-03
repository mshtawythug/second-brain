"""Tests for brain.elicit.queue — _rank_gaps RRF + build_queue upsert/read-back."""
from __future__ import annotations

import uuid
from typing import Any

import psycopg

from brain.elicit.queue import _rank_gaps, build_queue
from brain.elicit.schema import Gap

# ---------------------------------------------------------------------------
# Helpers
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


class FakeDetector:
    """Hand-written test double that satisfies the GapDetector Protocol.

    Returns a caller-supplied fixed list of Gaps without touching the DB.
    Avoids monkey-patching — the real detector registry is not modified.
    """

    signal_kind = "delta"

    def __init__(self, gaps: list[Gap]) -> None:
        self._gaps = gaps

    def detect(
        self,
        conn: psycopg.Connection[Any],
        *,
        tenant_id: str,
        limit: int,
    ) -> list[Gap]:
        return self._gaps[:limit]


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
    """Resolved gaps are excluded; surfaced siblings are still returned.

    Previously the test was vacuous: an empty return list trivially satisfies
    ``all(g.target_id != 'x' for g in [])``.  Adding a surfaced row 'y' forces
    build_queue to return a non-empty list, making the exclusion assertion
    meaningful.
    """
    test_db.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, status) "
        "VALUES ('default','delta','topic','x', 0.9, ARRAY['d1','d2','d3'], 'resolved')"
    )
    test_db.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, rationale, status) "
        "VALUES ('default','orphan','topic','y', 0.9, ARRAY['d1','d2','d3'], 'r', 'surfaced')"
    )
    from brain.config import Config

    cfg = Config.load()
    gaps = build_queue(test_db, cfg=cfg, tenant_id="default", detectors=[], limit=10)
    assert len(gaps) > 0, "surfaced row 'y' must be returned"
    assert any(g.target_id == "y" for g in gaps), "surfaced gap must be present"
    assert all(g.target_id != "x" for g in gaps), "resolved gap must be absent"


def test_build_queue_purges_orphaned_entity_gaps(test_db: psycopg.Connection) -> None:
    """F2 self-heal: a surfaced gap whose entity was merged/deleted is dropped.

    Surfaced AND snoozed + entity-typed + UUID-target + missing-entity rows are
    purged. A gap whose entity still exists, a *resolved* gap with a missing
    entity, and a ``user_flagged`` gap with a raw-string (non-UUID) target all
    survive.
    """
    # A real entity → its surfaced gap must survive.
    live_id = test_db.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
        "VALUES ('default','org','Acme Corp','acme corp') RETURNING id::text"
    ).fetchone()
    assert live_id is not None
    live_entity = str(live_id[0])
    orphan_uuid = str(uuid.uuid4())  # entity-typed UUID with NO graph_entities row
    orphan_snoozed_uuid = str(uuid.uuid4())
    resolved_uuid = str(uuid.uuid4())

    test_db.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, status) "
        "VALUES "
        "('default','orphan','org',%s, 0.9, ARRAY['d1','d2','d3'], 'surfaced'),"
        "('default','orphan','org',%s, 0.9, ARRAY['d1','d2','d3'], 'surfaced'),"
        "('default','orphan','org',%s, 0.9, ARRAY['d1','d2','d3'], 'snoozed'),"
        "('default','orphan','org',%s, 0.9, ARRAY['d1','d2','d3'], 'resolved'),"
        "('default','user_flagged','topic','some raw topic', 0.9, ARRAY['d1'], 'surfaced')",
        (live_entity, orphan_uuid, orphan_snoozed_uuid, resolved_uuid),
    )
    from brain.config import Config

    cfg = Config.load()
    build_queue(test_db, cfg=cfg, tenant_id="default", detectors=[], limit=10)

    def _exists(target_id: str, status: str) -> bool:
        row = test_db.execute(
            "SELECT 1 FROM elicitation_gaps "
            "WHERE tenant_id='default' AND target_id=%s AND status=%s",
            (target_id, status),
        ).fetchone()
        return row is not None

    assert not _exists(orphan_uuid, "surfaced"), "orphaned surfaced entity gap purged"
    assert not _exists(orphan_snoozed_uuid, "snoozed"), "orphaned snoozed entity gap purged"
    assert _exists(live_entity, "surfaced"), "gap for a live entity survives"
    assert _exists(resolved_uuid, "resolved"), "resolved gap is never purged"
    assert _exists("some raw topic", "surfaced"), "user_flagged raw-string target survives"


def test_build_queue_empty_detectors_returns_empty(test_db: psycopg.Connection) -> None:
    """With no detectors and an empty queue, build_queue returns []."""
    from brain.config import Config

    cfg = Config.load()
    gaps = build_queue(test_db, cfg=cfg, tenant_id="default", detectors=[], limit=10)
    assert gaps == []


def test_build_queue_evidence_guard_drops_sparse_gaps(test_db: psycopg.Connection) -> None:
    """Gaps with fewer evidence docs than elicit_min_evidence_docs are not upserted or returned."""
    from brain.config import Config
    from brain.elicit.detectors import DeltaDetector

    # Seed entity with only 2 ingested docs — below default min_evidence_docs=3.
    eid = test_db.execute(
        "INSERT INTO graph_entities "
        "(tenant_id, entity_type, name, canonical_key, description, doc_count) "
        "VALUES ('default', 'org', 'Sparse', 'sparse', 'desc', 2) RETURNING id"
    ).fetchone()[0]  # type: ignore[index]
    for i in range(2):
        did = test_db.execute(
            "INSERT INTO documents (title, content, content_hash, content_type, kind) "
            "VALUES (%s, 'body', %s, 'note', 'ingested') RETURNING id",
            (f"Sparse doc {i}", f"sparse-{i}-guard-hash"),
        ).fetchone()[0]  # type: ignore[index]
        test_db.execute(
            "INSERT INTO graph_entity_mentions "
            "(tenant_id, entity_id, document_id, source) "
            "VALUES ('default', %s, %s, 'people')",
            (eid, did),
        )

    cfg = Config.load()  # elicit_min_evidence_docs defaults to 3
    gaps = build_queue(
        test_db, cfg=cfg, tenant_id="default", detectors=[DeltaDetector()], limit=10
    )
    assert gaps == []
    count = test_db.execute("SELECT count(*) FROM elicitation_gaps").fetchone()
    assert count is not None and count[0] == 0


def test_build_queue_score_floor_and_include_low_confidence(
    test_db: psycopg.Connection,
) -> None:
    """Gaps below elicit_min_gap_score are excluded; include_low_confidence=True bypasses floor."""
    from brain.config import Config

    # Insert a gap directly with score=0.1, below the default floor of 0.3.
    test_db.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, rationale, status) "
        "VALUES ('default', 'delta', 'topic', 'low-score-target', 0.1, "
        "ARRAY['d1','d2','d3'], 'rationale', 'surfaced')"
    )

    cfg = Config.load()  # elicit_min_gap_score defaults to 0.3

    # Without include_low_confidence: floor=0.3, gap excluded.
    gaps_normal = build_queue(
        test_db, cfg=cfg, tenant_id="default", detectors=[], limit=10
    )
    assert all(g.target_id != "low-score-target" for g in gaps_normal)

    # With include_low_confidence=True: floor=0.0, gap included.
    gaps_lc = build_queue(
        test_db,
        cfg=cfg,
        tenant_id="default",
        detectors=[],
        limit=10,
        include_low_confidence=True,
    )
    assert any(g.target_id == "low-score-target" for g in gaps_lc)


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


# ---------------------------------------------------------------------------
# FakeDetector-based guard tests (spec-mandated: 3 tests below)
# ---------------------------------------------------------------------------


def test_build_queue_min_evidence_guard_via_fake_detector(
    test_db: psycopg.Connection,
) -> None:
    """FakeDetector: gap with < min_evidence_docs evidence_ids is excluded;
    sibling with >= is kept."""
    from brain.config import Config

    sparse = _g("delta", "target-sparse", 8.0, n_ev=2)  # 2 < default min_evidence_docs=3
    ample = _g("delta", "target-ample", 5.0, n_ev=3)  # 3 == min_evidence_docs → included

    cfg = Config.load()  # elicit_min_evidence_docs defaults to 3
    gaps = build_queue(
        test_db,
        cfg=cfg,
        tenant_id="default",
        detectors=[FakeDetector([sparse, ample])],
        limit=10,
    )

    returned_ids = {g.target_id for g in gaps}
    assert "target-sparse" not in returned_ids, "sparse gap must be excluded"
    assert "target-ample" in returned_ids, "ample gap must be returned"

    # Sparse gap must NOT have been upserted into the DB.
    row = test_db.execute(
        "SELECT count(*) FROM elicitation_gaps WHERE target_id = 'target-sparse'"
    ).fetchone()
    assert row is not None and row[0] == 0


def test_build_queue_score_floor_excludes_below_threshold(
    test_db: psycopg.Connection,
) -> None:
    """Gaps with score < elicit_min_gap_score (0.3) are absent from normal read-back."""
    from brain.config import Config

    test_db.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, rationale, status) "
        "VALUES ('default','delta','topic','hi-score',0.9,ARRAY['d1','d2','d3'],'r','surfaced')"
    )
    test_db.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, rationale, status) "
        "VALUES ('default','orphan','topic','lo-score',0.1,ARRAY['d1','d2','d3'],'r','surfaced')"
    )

    cfg = Config.load()  # elicit_min_gap_score defaults to 0.3
    gaps = build_queue(test_db, cfg=cfg, tenant_id="default", detectors=[], limit=10)
    ids = {g.target_id for g in gaps}
    assert "hi-score" in ids, "gap above floor must be returned"
    assert "lo-score" not in ids, "gap below floor must be absent"


def test_build_queue_include_low_confidence_bypasses_floor(
    test_db: psycopg.Connection,
) -> None:
    """include_low_confidence=True returns gaps regardless of score floor."""
    from brain.config import Config

    test_db.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, rationale, status) "
        "VALUES ('default','delta','topic','lo-conf',0.1,ARRAY['d1','d2','d3'],'r','surfaced')"
    )

    cfg = Config.load()  # elicit_min_gap_score defaults to 0.3
    gaps = build_queue(
        test_db,
        cfg=cfg,
        tenant_id="default",
        detectors=[],
        limit=10,
        include_low_confidence=True,
    )
    assert any(g.target_id == "lo-conf" for g in gaps), "low-conf gap must appear"


def test_build_queue_limit_overrides_config_discovery_cap(
    test_db: psycopg.Connection,
) -> None:
    """A caller ``limit`` larger than ``cfg.elicit_queue_limit`` widens discovery.

    Regression: detectors were always run with ``limit=cfg.elicit_queue_limit``
    (default 20), so ``build_queue(..., limit=30)`` could never surface more than
    20 gaps even though the caller asked for 30. The effective discovery limit
    must be ``max(limit, cfg.elicit_queue_limit)``.
    """
    from brain.config import Config

    cfg = Config.load()  # elicit_queue_limit defaults to 20
    big = cfg.elicit_queue_limit + 10  # 30
    many = [_g("delta", f"target-{i}", float(big - i)) for i in range(big)]

    gaps = build_queue(
        test_db,
        cfg=cfg,
        tenant_id="default",
        detectors=[FakeDetector(many)],
        limit=big,
    )

    assert len(gaps) > cfg.elicit_queue_limit, (
        "caller limit must override the smaller config discovery cap"
    )
    assert len(gaps) == big


# ---------------------------------------------------------------------------
# build_queue: read-back scoping for --signal / --target (no-leak guard)
# ---------------------------------------------------------------------------


def _seed_mixed_open_gaps(conn: psycopg.Connection) -> None:
    """Seed four distinct surfaced gaps across signals/targets above the floor."""
    rows = [
        ("delta", "topic", "delta-target"),
        ("orphan", "topic", "orphan-target"),
        ("user_flagged", "topic", "y"),
        ("user_flagged", "topic", "z"),
    ]
    for signal_kind, target_type, target_id in rows:
        conn.execute(
            "INSERT INTO elicitation_gaps "
            "(tenant_id, signal_kind, target_type, target_id, score, "
            "evidence_ids, rationale, status) "
            "VALUES ('default', %s, %s, %s, 0.9, ARRAY['d1','d2','d3'], 'r', 'surfaced')",
            (signal_kind, target_type, target_id),
        )


def test_build_queue_signal_kinds_filters_readback(
    test_db: psycopg.Connection,
) -> None:
    """signal_kinds restricts the read-back to the requested signal only."""
    from brain.config import Config

    _seed_mixed_open_gaps(test_db)
    cfg = Config.load()

    gaps = build_queue(
        test_db,
        cfg=cfg,
        tenant_id="default",
        detectors=[],
        limit=10,
        signal_kinds=["delta"],
    )

    assert [g.target_id for g in gaps] == ["delta-target"]


def test_build_queue_target_ids_filters_readback(
    test_db: psycopg.Connection,
) -> None:
    """signal_kinds + target_ids narrows to exactly one user-flagged target."""
    from brain.config import Config

    _seed_mixed_open_gaps(test_db)
    cfg = Config.load()

    gaps = build_queue(
        test_db,
        cfg=cfg,
        tenant_id="default",
        detectors=[],
        limit=10,
        signal_kinds=["user_flagged"],
        target_ids=["y"],
    )

    assert [g.target_id for g in gaps] == ["y"]


def test_build_queue_no_filter_returns_whole_queue(
    test_db: psycopg.Connection,
) -> None:
    """No filter (the `elicit list` / default path) still returns every open gap."""
    from brain.config import Config

    _seed_mixed_open_gaps(test_db)
    cfg = Config.load()

    gaps = build_queue(
        test_db, cfg=cfg, tenant_id="default", detectors=[], limit=10
    )

    returned = {g.target_id for g in gaps}
    assert returned == {"delta-target", "orphan-target", "y", "z"}


# ---------------------------------------------------------------------------
# build_queue: read-back min-evidence guard (previously only pre-upsert)
# ---------------------------------------------------------------------------


def test_build_queue_readback_drops_persisted_sparse_gap(
    test_db: psycopg.Connection,
) -> None:
    """A persisted surfaced gap above the score floor but below min-evidence
    must NOT resurface on read-back (the guard is no longer pre-upsert only)."""
    from brain.config import Config

    # Score 0.9 (well above default floor 0.3) but only 1 evidence id, below
    # the default min_evidence_docs=3.
    test_db.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, "
        "rationale, status) "
        "VALUES ('default','delta','topic','sparse-persisted',0.9,ARRAY['d1'],"
        "'r','surfaced')"
    )

    cfg = Config.load()  # elicit_min_evidence_docs defaults to 3
    gaps = build_queue(test_db, cfg=cfg, tenant_id="default", detectors=[], limit=10)

    assert all(g.target_id != "sparse-persisted" for g in gaps), (
        "sparse persisted gap must be filtered on read-back"
    )


def test_build_queue_readback_keeps_user_flagged_without_evidence(
    test_db: psycopg.Connection,
) -> None:
    """A user_flagged gap with ZERO evidence above the floor is still returned —
    user_flagged is exempt from the min-evidence guard."""
    from brain.config import Config

    test_db.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, "
        "rationale, status) "
        "VALUES ('default','user_flagged','topic','flagged-no-evidence',0.9,"
        "ARRAY[]::text[],'r','surfaced')"
    )

    cfg = Config.load()
    gaps = build_queue(test_db, cfg=cfg, tenant_id="default", detectors=[], limit=10)

    assert any(g.target_id == "flagged-no-evidence" for g in gaps), (
        "user_flagged gap must be exempt from the min-evidence read-back guard"
    )


# ---------------------------------------------------------------------------
# build_queue: target_name resolution via graph_entities LEFT JOIN
# ---------------------------------------------------------------------------


def test_build_queue_resolves_target_name_from_graph_entity(
    test_db: psycopg.Connection,
) -> None:
    """A gap whose target_id is a real graph_entities UUID resolves target_name."""
    from brain.config import Config

    entity_id = test_db.execute(
        "INSERT INTO graph_entities "
        "(tenant_id, entity_type, name, canonical_key, description, doc_count) "
        "VALUES ('default', 'org', 'Acme', 'acme', 'desc', 3) RETURNING id"
    ).fetchone()[0]  # type: ignore[index]
    test_db.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, "
        "rationale, status) "
        "VALUES ('default','delta','org',%s,0.9,ARRAY['d1','d2','d3'],'r','surfaced')",
        (str(entity_id),),
    )

    cfg = Config.load()
    gaps = build_queue(test_db, cfg=cfg, tenant_id="default", detectors=[], limit=10)

    matched = [g for g in gaps if g.target_id == str(entity_id)]
    assert len(matched) == 1, "the entity-targeted gap must be returned"
    assert matched[0].target_name == "Acme"


def test_build_queue_raw_string_target_has_empty_name(
    test_db: psycopg.Connection,
) -> None:
    """A user_flagged gap with a raw-string (non-UUID) target_id must not crash
    on the TEXT-equality join and resolves to an empty target_name."""
    from brain.config import Config

    test_db.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, "
        "rationale, status) "
        "VALUES ('default','user_flagged','topic','not-a-uuid',0.9,"
        "ARRAY[]::text[],'r','surfaced')"
    )

    cfg = Config.load()
    gaps = build_queue(test_db, cfg=cfg, tenant_id="default", detectors=[], limit=10)

    matched = [g for g in gaps if g.target_id == "not-a-uuid"]
    assert len(matched) == 1, "raw-string target gap must be returned, not crash"
    assert matched[0].target_name == ""


# ---------------------------------------------------------------------------
# build_queue: target_types read-back filter
# ---------------------------------------------------------------------------


def test_build_queue_target_types_filters_readback(
    test_db: psycopg.Connection,
) -> None:
    """target_types restricts the read-back to the requested entity type(s)."""
    from brain.config import Config

    for target_type, target_id in (("org", "org-target"), ("tool", "tool-target")):
        test_db.execute(
            "INSERT INTO elicitation_gaps "
            "(tenant_id, signal_kind, target_type, target_id, score, "
            "evidence_ids, rationale, status) "
            "VALUES ('default','delta',%s,%s,0.9,ARRAY['d1','d2','d3'],'r','surfaced')",
            (target_type, target_id),
        )

    cfg = Config.load()

    only_org = build_queue(
        test_db,
        cfg=cfg,
        tenant_id="default",
        detectors=[],
        limit=10,
        target_types=["org"],
    )
    assert [g.target_id for g in only_org] == ["org-target"]

    both = build_queue(
        test_db,
        cfg=cfg,
        tenant_id="default",
        detectors=[],
        limit=10,
        target_types=None,
    )
    assert {g.target_id for g in both} == {"org-target", "tool-target"}
