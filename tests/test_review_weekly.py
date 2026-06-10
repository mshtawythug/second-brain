"""Orchestrator tests for ``brain.review.weekly``.

Integration against the real Postgres test DB. Covers the no-graph fallback
(tag clusters), the graph-path community ranking, key-people extraction, the
windowed activity/ingest counts, and ``weekly_active_communities`` on an empty
graph. All fixtures are synthetic (``topic-alpha`` / ``person-a`` etc.).
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime

import psycopg

from brain.config import Config
from brain.review.weekly import build_weekly_report, weekly_active_communities

WEEK = "2026-W23"
GENERATED_ON = date(2026, 6, 9)
IN_WINDOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
OUT_WINDOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
WIN_AFTER = datetime(2026, 6, 1, tzinfo=UTC)
WIN_BEFORE = datetime(2026, 6, 7, 23, 59, 59, tzinfo=UTC)


def _cfg(**overrides: object) -> Config:
    base: dict[str, object] = {
        "database_url": "postgresql://u:p@localhost:5/db",
        "graph_enabled": True,
        "graph_tenant_id": "default",
        "review_activity_limit": 20,
        "review_theme_limit": 5,
        "review_open_loop_limit": 20,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def _set_ingested_at(conn: psycopg.Connection, doc_id: str, when: datetime) -> None:
    conn.execute(
        "UPDATE documents SET ingested_at = %s WHERE id = %s", (when, doc_id)
    )


def _add_interaction(conn: psycopg.Connection, doc_id: str, when: datetime) -> None:
    conn.execute(
        "INSERT INTO interactions (document_id, action, source, at) "
        "VALUES (%s, 'opened', 'cli', %s)",
        (doc_id, when),
    )


def _krisp_doc(
    conn: psycopg.Connection,
    *,
    title: str,
    content: str,
    participant_keys: list[str],
    ingested_at: datetime,
) -> str:
    """Insert a krisp-sourced doc carrying ``_participant_keys`` metadata."""
    ext = str(uuid.uuid4())
    src = conn.execute(
        "INSERT INTO sources (kind, external_id) VALUES ('krisp', %s) RETURNING id",
        (ext,),
    ).fetchone()[0]
    meta = json.dumps({"_participant_keys": participant_keys})
    doc_id = conn.execute(
        "INSERT INTO documents "
        "(source_id, title, content, content_hash, content_type, metadata, ingested_at) "
        "VALUES (%s, %s, %s, %s, 'transcript', %s, %s) RETURNING id::text",
        (src, title, content, str(uuid.uuid4()), meta, ingested_at),
    ).fetchone()[0]
    return str(doc_id)


# ---------------------------------------------------------------------------
# no-graph fallback path
# ---------------------------------------------------------------------------


def test_build_weekly_report_no_graph_uses_tag_clusters(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    a1 = seed_doc(title="Alpha 1", content="a1 body", tags=["topic-alpha"])
    a2 = seed_doc(title="Alpha 2", content="a2 body", tags=["topic-alpha"])
    b1 = seed_doc(title="Beta 1", content="b1 body", tags=["topic-beta"])
    for doc in (a1, a2, b1):
        _add_interaction(test_db, doc, IN_WINDOW)

    report = build_weekly_report(
        test_db, _cfg(), week=WEEK, generated_on=GENERATED_ON, no_graph=True
    )

    assert report.graph_used is False
    # Most-frequent tag (topic-alpha, 2 docs) ranks first; each theme's
    # entity_names is just [tag] on the fallback path.
    assert report.themes[0].entity_names == ["topic-alpha"]
    assert report.themes[0].synthesis is None
    keys = [t.key for t in report.themes]
    assert keys == ["topic-alpha", "topic-beta"]


def test_build_weekly_report_no_graph_skips_untagged_docs(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    tagged = seed_doc(title="Tagged", content="t body", tags=["topic-alpha"])
    untagged = seed_doc(title="Untagged", content="u body")
    _add_interaction(test_db, tagged, IN_WINDOW)
    _add_interaction(test_db, untagged, IN_WINDOW)

    report = build_weekly_report(
        test_db, _cfg(), week=WEEK, generated_on=GENERATED_ON, no_graph=True
    )
    assert [t.key for t in report.themes] == ["topic-alpha"]


# ---------------------------------------------------------------------------
# windowed counts + key people
# ---------------------------------------------------------------------------


def test_build_weekly_report_windowed_counts(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    # 5 in-window ingested docs; 3 of them get an in-window interaction.
    in_window = []
    for i in range(5):
        doc = seed_doc(title=f"In {i}", content=f"in body {i}")
        _set_ingested_at(test_db, doc, datetime(2026, 6, 2 + (i % 5), tzinfo=UTC))
        in_window.append(doc)
    for doc in in_window[:3]:
        _add_interaction(test_db, doc, IN_WINDOW)
    # 2 out-of-window ingested docs (excluded).
    for i in range(2):
        old = seed_doc(title=f"Old {i}", content=f"old body {i}")
        _set_ingested_at(test_db, old, OUT_WINDOW)

    report = build_weekly_report(
        test_db, _cfg(), week=WEEK, generated_on=GENERATED_ON, no_graph=True
    )
    assert len(report.activity) == 3
    assert len(report.ingested) == 5


def _action_items_doc(
    conn: psycopg.Connection, body: str, ingested_at: datetime
) -> str:
    doc = conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type, ingested_at) "
        "VALUES ('Action items', %s, %s, 'krisp_action_items', %s) RETURNING id::text",
        (body, str(uuid.uuid4()), ingested_at),
    ).fetchone()[0]
    return str(doc)


def test_build_weekly_report_open_loops_scoped_to_week(
    test_db: psycopg.Connection,
) -> None:
    # An open item ingested in-window appears; an open item ingested out-of-window
    # (the current-week loop a NOW()-relative query would wrongly include) does not.
    _action_items_doc(test_db, "- [ ] in-window loop\n", IN_WINDOW)
    _action_items_doc(test_db, "- [ ] out-of-window loop\n", OUT_WINDOW)

    report = build_weekly_report(
        test_db, _cfg(), week=WEEK, generated_on=GENERATED_ON, no_graph=True
    )
    texts = {row.text for row in report.open_loops}
    assert texts == {"in-window loop"}


def test_build_weekly_report_key_people_dedup_top5(
    test_db: psycopg.Connection,
) -> None:
    # person-a appears in 3 docs, person-b in 2, person-c in 1 → ranked a,b,c.
    d1 = _krisp_doc(
        test_db,
        title="Sync 1",
        content="c1",
        participant_keys=["person-a", "person-b"],
        ingested_at=IN_WINDOW,
    )
    d2 = _krisp_doc(
        test_db,
        title="Sync 2",
        content="c2",
        participant_keys=["person-a", "person-b", "person-c"],
        ingested_at=IN_WINDOW,
    )
    d3 = _krisp_doc(
        test_db,
        title="Sync 3",
        content="c3",
        participant_keys=["person-a"],
        ingested_at=IN_WINDOW,
    )
    for doc in (d1, d2, d3):
        _add_interaction(test_db, doc, IN_WINDOW)

    report = build_weekly_report(
        test_db, _cfg(), week=WEEK, generated_on=GENERATED_ON, no_graph=True
    )
    assert report.key_people[:3] == ["person-a", "person-b", "person-c"]


# ---------------------------------------------------------------------------
# graph leg
# ---------------------------------------------------------------------------


def test_weekly_active_communities_empty_graph_returns_empty(
    test_db: psycopg.Connection,
) -> None:
    assert (
        weekly_active_communities(
            test_db,
            tenant_id="default",
            after=WIN_AFTER,
            before=WIN_BEFORE,
            theme_limit=5,
        )
        == []
    )


def test_build_weekly_report_graph_path(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    # Seed a minimal community: two entities, one membership each, one edge
    # contribution from an in-window doc.
    doc = seed_doc(title="Graph doc", content="graph body", tags=["x"])
    _set_ingested_at(test_db, doc, IN_WINDOW)
    _add_interaction(test_db, doc, IN_WINDOW)
    e1, e2 = str(uuid.uuid4()), str(uuid.uuid4())
    # Canonical ordering for the gec_canonical CHECK (src_id < dst_id).
    src_id, dst_id = sorted([e1, e2])
    ck = str(uuid.uuid4())
    test_db.execute(
        "INSERT INTO graph_entities (id, tenant_id, entity_type, name, canonical_key) "
        "VALUES (%s,'default','topic','topic-alpha','topic-alpha'),"
        "       (%s,'default','project','project-delta','project-delta')",
        (e1, e2),
    )
    test_db.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, source) "
        "VALUES ('default',%s,%s,'people'),('default',%s,%s,'people')",
        (e1, doc, e2, doc),
    )
    test_db.execute(
        "INSERT INTO graph_communities "
        "(tenant_id, community_key, source_graph_hash, members_hash, member_count) "
        "VALUES ('default', %s, 'h', 'mh', 2)",
        (ck,),
    )
    test_db.execute(
        "INSERT INTO graph_community_members "
        "(tenant_id, community_key, entity_id, member_rank, member_weight) "
        "VALUES ('default',%s,%s,0,1.0),('default',%s,%s,1,0.5)",
        (ck, e1, ck, e2),
    )
    test_db.execute(
        "INSERT INTO graph_edge_contributions "
        "(tenant_id, document_id, src_id, dst_id, cooccur_count) "
        "VALUES ('default', %s, %s, %s, 3)",
        (doc, src_id, dst_id),
    )

    report = build_weekly_report(
        test_db, _cfg(), week=WEEK, generated_on=GENERATED_ON, no_graph=False
    )

    assert report.graph_used is True
    assert len(report.themes) == 1
    theme = report.themes[0]
    assert theme.key == ck
    assert set(theme.entity_names) == {"topic-alpha", "project-delta"}
    assert (doc, "Graph doc") in theme.docs


class _FakeEnricher:
    """Minimal enricher double: records the group-summary call, returns a fixed line."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, list[str], list[str]]] = []

    def summarize_group(
        self, *, person: str | None, entity_names: list[str], doc_titles: list[str]
    ) -> str | None:
        self.calls.append((person, entity_names, doc_titles))
        return "synthetic theme synthesis"


def _seed_graph_community(
    conn: psycopg.Connection,
    doc: str,
    *,
    summary: str | None = None,
) -> str:
    """Seed a minimal one-doc community; return its community_key."""
    e1, e2 = str(uuid.uuid4()), str(uuid.uuid4())
    src_id, dst_id = sorted([e1, e2])
    ck = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO graph_entities (id, tenant_id, entity_type, name, canonical_key) "
        "VALUES (%s,'default','topic','topic-alpha','topic-alpha'),"
        "       (%s,'default','project','project-delta','project-delta')",
        (e1, e2),
    )
    conn.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, source) "
        "VALUES ('default',%s,%s,'people'),('default',%s,%s,'people')",
        (e1, doc, e2, doc),
    )
    conn.execute(
        "INSERT INTO graph_communities "
        "(tenant_id, community_key, source_graph_hash, members_hash, member_count, summary) "
        "VALUES ('default', %s, 'h', 'mh', 2, %s)",
        (ck, summary),
    )
    conn.execute(
        "INSERT INTO graph_community_members "
        "(tenant_id, community_key, entity_id, member_rank, member_weight) "
        "VALUES ('default',%s,%s,0,1.0),('default',%s,%s,1,0.5)",
        (ck, e1, ck, e2),
    )
    conn.execute(
        "INSERT INTO graph_edge_contributions "
        "(tenant_id, document_id, src_id, dst_id, cooccur_count) "
        "VALUES ('default', %s, %s, %s, 3)",
        (doc, src_id, dst_id),
    )
    return ck


def test_build_weekly_report_graph_uses_stored_community_summary(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    doc = seed_doc(title="Graph doc", content="graph body", tags=["x"])
    _set_ingested_at(test_db, doc, IN_WINDOW)
    _seed_graph_community(test_db, doc, summary="A stored community summary.")
    enricher = _FakeEnricher()

    report = build_weekly_report(
        test_db,
        _cfg(),
        week=WEEK,
        generated_on=GENERATED_ON,
        no_graph=False,
        enricher=enricher,
    )
    # Stored summary wins; the enricher is NOT consulted.
    assert report.themes[0].synthesis == "A stored community summary."
    assert enricher.calls == []


def test_build_weekly_report_graph_synthesizes_when_summary_missing(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    doc = seed_doc(title="Graph doc", content="graph body", tags=["x"])
    _set_ingested_at(test_db, doc, IN_WINDOW)
    _seed_graph_community(test_db, doc, summary=None)
    enricher = _FakeEnricher()

    report = build_weekly_report(
        test_db,
        _cfg(),
        week=WEEK,
        generated_on=GENERATED_ON,
        no_graph=False,
        enricher=enricher,
    )
    # No stored summary → best-effort enricher synthesis is used.
    assert report.themes[0].synthesis == "synthetic theme synthesis"
    assert len(enricher.calls) == 1


def test_build_weekly_report_graph_disabled_falls_back(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    doc = seed_doc(title="Doc", content="body", tags=["topic-alpha"])
    _add_interaction(test_db, doc, IN_WINDOW)
    report = build_weekly_report(
        test_db,
        _cfg(graph_enabled=False),
        week=WEEK,
        generated_on=GENERATED_ON,
        no_graph=False,
    )
    assert report.graph_used is False
    assert report.themes[0].entity_names == ["topic-alpha"]
