"""The brief's "new this week" entities (RM-17, ``brain.brief_entities``).

Integration tests against the test Postgres (port 5434). Every entity and document is synthetic.
Each test dates its rows relative to one fixed ``NOW`` so the 7-day window is deterministic.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from brain.brief_entities import (
    NewEntity,
    entity_tables_present,
    for_brief,
    new_entities,
    section_lines,
)
from brain.config import Config
from tests.graph_entity_fakes import TENANT, seed_doc, seed_entity, seed_mention

NOW = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
SINCE = NOW - timedelta(days=7)
YESTERDAY = NOW - timedelta(days=1)
TEN_DAYS_AGO = NOW - timedelta(days=10)


def _doc(conn: psycopg.Connection, **kwargs: Any) -> str:
    return seed_doc(conn, **kwargs)


def _entity(conn: psycopg.Connection, name: str, **kwargs: Any) -> str:
    kwargs.setdefault("created_at", YESTERDAY)
    return seed_entity(conn, name, **kwargs)


def _mention(conn: psycopg.Connection, entity_id: str, document_id: str, **kwargs: Any) -> None:
    seed_mention(conn, entity_id, document_id, **kwargs)


def _names(conn: psycopg.Connection, *, exclude_confidential: bool = True) -> list[str]:
    return [
        entity.name
        for entity in new_entities(
            conn, tenant_id=TENANT, since=SINCE, exclude_confidential=exclude_confidential
        )
    ]


# --- the four acceptance criteria ------------------------------------------------------------


def test_ac1_entity_first_mentioned_by_a_document_ingested_yesterday_is_listed(
    test_db: psycopg.Connection,
) -> None:
    entity = _entity(test_db, "Avery Example")
    _mention(test_db, entity, _doc(test_db, ingested_at=YESTERDAY))

    [listed] = new_entities(test_db, tenant_id=TENANT, since=SINCE, exclude_confidential=True)

    assert listed == NewEntity(
        entity_id=entity,
        name="Avery Example",
        entity_type="person",
        first_ingested_at=YESTERDAY,
        document_count=1,
    )


def test_ac2_entity_recreated_yesterday_but_first_mentioned_ten_days_ago_is_not_listed(
    test_db: psycopg.Connection,
) -> None:
    # The row was re-created yesterday (aggregates.py deletes orphans), so created_at is new,
    # but the earliest mentioning document is ten days old: created_at is only a prefilter.
    entity = _entity(test_db, "Blake Example", created_at=YESTERDAY)
    _mention(test_db, entity, _doc(test_db, ingested_at=TEN_DAYS_AGO))
    _mention(test_db, entity, _doc(test_db, ingested_at=YESTERDAY))

    assert _names(test_db) == []
    assert _names(test_db, exclude_confidential=False) == []


def test_ac3_entity_known_only_from_confidential_documents_is_gated(
    test_db: psycopg.Connection,
) -> None:
    entity = _entity(test_db, "Casey Example")
    _mention(test_db, entity, _doc(test_db, ingested_at=YESTERDAY, sensitivity="confidential"))

    assert _names(test_db, exclude_confidential=True) == []
    assert _names(test_db, exclude_confidential=False) == ["Casey Example"]


def test_ac4_graph_disabled_returns_nothing_without_error(test_db: psycopg.Connection) -> None:
    entity = _entity(test_db, "Drew Example")
    _mention(test_db, entity, _doc(test_db, ingested_at=YESTERDAY))
    cfg = Config(database_url="postgresql://u:p@localhost:5/db", graph_enabled=False)  # type: ignore[call-arg]

    assert for_brief(test_db, cfg, exclude_confidential=True, now=NOW) == []


# --- the lens is INSIDE the aggregate ---------------------------------------------------------


def test_lens_is_applied_before_the_min_not_after_it(test_db: psycopg.Connection) -> None:
    # Old confidential mention + new normal mention. Gated, the confidential row never reaches
    # the MIN, so the entity is new to that audience; permissive, its first mention is old.
    entity = _entity(test_db, "Emery Example")
    _mention(test_db, entity, _doc(test_db, ingested_at=TEN_DAYS_AGO, sensitivity="confidential"))
    _mention(test_db, entity, _doc(test_db, ingested_at=YESTERDAY))

    assert _names(test_db, exclude_confidential=True) == ["Emery Example"]
    assert _names(test_db, exclude_confidential=False) == []


def test_gated_document_count_counts_only_visible_documents(
    test_db: psycopg.Connection,
) -> None:
    entity = _entity(test_db, "Finley Example")
    _mention(test_db, entity, _doc(test_db, ingested_at=YESTERDAY))
    _mention(test_db, entity, _doc(test_db, ingested_at=YESTERDAY, sensitivity="confidential"))

    [gated] = new_entities(test_db, tenant_id=TENANT, since=SINCE, exclude_confidential=True)
    [full] = new_entities(test_db, tenant_id=TENANT, since=SINCE, exclude_confidential=False)

    assert (gated.document_count, full.document_count) == (1, 2)


# --- drafts, types, tenant, prefilter, order, limit -------------------------------------------


def test_draft_only_entity_is_never_listed(test_db: psycopg.Connection) -> None:
    entity = _entity(test_db, "Gray Example")
    _mention(test_db, entity, _doc(test_db, ingested_at=YESTERDAY, draft=True))

    assert _names(test_db, exclude_confidential=True) == []
    assert _names(test_db, exclude_confidential=False) == []


def test_old_draft_mention_does_not_make_an_entity_old(test_db: psycopg.Connection) -> None:
    entity = _entity(test_db, "Harper Example")
    _mention(test_db, entity, _doc(test_db, ingested_at=TEN_DAYS_AGO, draft=True))
    _mention(test_db, entity, _doc(test_db, ingested_at=YESTERDAY))

    assert _names(test_db, exclude_confidential=False) == ["Harper Example"]


def test_only_people_orgs_and_projects_are_listed(test_db: psycopg.Connection) -> None:
    doc = _doc(test_db, ingested_at=YESTERDAY)
    for name, entity_type in [
        ("Indigo Example", "person"),
        ("Juniper Org", "org"),
        ("Kestrel Project", "project"),
        ("lattice topic", "topic"),
        ("mortar tool", "tool"),
    ]:
        _mention(test_db, _entity(test_db, name, entity_type=entity_type), doc)

    assert sorted(_names(test_db)) == ["Indigo Example", "Juniper Org", "Kestrel Project"]


def test_other_tenants_entities_are_not_listed(test_db: psycopg.Connection) -> None:
    doc = _doc(test_db, ingested_at=YESTERDAY)
    _mention(test_db, _entity(test_db, "Nova Example", tenant_id="other"), doc, tenant_id="other")

    assert _names(test_db) == []


def test_created_at_prefilter_drops_an_entity_created_before_the_window(
    test_db: psycopg.Connection,
) -> None:
    # The ticket's accepted risk: the prefilter can only delay a name, never leak one.
    entity = _entity(test_db, "Oakley Example", created_at=TEN_DAYS_AGO)
    _mention(test_db, entity, _doc(test_db, ingested_at=YESTERDAY))

    assert _names(test_db) == []


def test_most_mentioned_first_then_newest_then_name_and_limit(
    test_db: psycopg.Connection,
) -> None:
    two_days_ago = NOW - timedelta(days=2)
    busy = _entity(test_db, "Parker Example")
    for _ in range(2):
        _mention(test_db, busy, _doc(test_db, ingested_at=two_days_ago))
    for name in ["Rowan Example", "Quinn Example"]:
        _mention(test_db, _entity(test_db, name), _doc(test_db, ingested_at=YESTERDAY))
    _mention(test_db, _entity(test_db, "Sage Example"), _doc(test_db, ingested_at=two_days_ago))

    names = _names(test_db)
    limited = new_entities(
        test_db, tenant_id=TENANT, since=SINCE, exclude_confidential=True, limit=2
    )

    assert names == ["Parker Example", "Quinn Example", "Rowan Example", "Sage Example"]
    assert [entity.name for entity in limited] == ["Parker Example", "Quinn Example"]


# --- for_brief: the cheap existence check and the window --------------------------------------


def test_for_brief_uses_a_seven_day_window_and_the_configured_tenant(
    test_db: psycopg.Connection,
) -> None:
    inside = _entity(test_db, "Tatum Example", created_at=NOW - timedelta(days=6))
    _mention(test_db, inside, _doc(test_db, ingested_at=NOW - timedelta(days=6)))
    outside = _entity(test_db, "Umber Example", created_at=NOW - timedelta(days=8))
    _mention(test_db, outside, _doc(test_db, ingested_at=NOW - timedelta(days=8)))
    cfg = Config(database_url="postgresql://u:p@localhost:5/db")  # type: ignore[call-arg]

    listed = for_brief(test_db, cfg, exclude_confidential=True, now=NOW)

    assert [entity.name for entity in listed] == ["Tatum Example"]


def test_tables_absent_means_no_section_and_no_error(test_db: psycopg.Connection) -> None:
    cfg = Config(database_url="postgresql://u:p@localhost:5/db")  # type: ignore[call-arg]
    with pytest.raises(_RolledBack), test_db.transaction():
        test_db.execute("DROP TABLE graph_entity_mentions CASCADE")
        assert entity_tables_present(test_db) is False
        assert for_brief(test_db, cfg, exclude_confidential=True, now=NOW) == []
        raise _RolledBack
    assert entity_tables_present(test_db) is True


class _RolledBack(Exception):
    """Raised inside the transaction so the DROP above is rolled back."""


# --- rendering -------------------------------------------------------------------------------


def test_section_lines_name_type_and_collapse_whitespace() -> None:
    entity = NewEntity(
        entity_id=str(uuid.uuid4()),
        name="Vale\n  Example",
        entity_type="org",
        first_ingested_at=YESTERDAY,
        document_count=3,
    )

    assert section_lines([entity]) == ["[org] Vale Example"]
    assert section_lines([]) == []


def test_to_dict_is_the_wire_shape() -> None:
    entity_id = str(uuid.uuid4())
    entity = NewEntity(
        entity_id=entity_id,
        name="Wren Example",
        entity_type="project",
        first_ingested_at=YESTERDAY,
        document_count=2,
    )

    assert entity.to_dict() == {
        "id": entity_id,
        "name": "Wren Example",
        "type": "project",
        "first_ingested_at": YESTERDAY.isoformat(),
        "documents": 2,
    }
