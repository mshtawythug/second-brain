"""Global graph search never returns a community with a confidential member (RM-46 rev-118 round 2).

A community's stored summary is written from its members' names and its
documents' titles. ``brain_graphrag_communities`` already withheld one any of
whose members a confidential document mentions (``list_communities``); global
mode returned the same stored summaries ungated. With ``exclude_confidential``
(stdio and HTTP alike) the same rule now runs before the ranking cut, so the
limit fills from eligible communities.

The HTTP-only graph filters this file also tested were removed in round 5, when
the graph family stopped being served without ``--include-confidential``
(``tests/test_mcp_http_graph_withheld.py``).

All names, titles and summaries are synthetic. Runs against the test DB on 5434.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from brain.config import Config
from brain.graph_rag.communities import communities_with_confidential_members
from brain.graph_rag.global_ import _retrieve_global
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.usefixtures("test_db")

TENANT = "default"


def _doc(conn: psycopg.Connection[Any], title: str, sensitivity: str = "normal") -> str:
    row = conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type, sensitivity) "
        "VALUES (%s, %s, %s, 'note', %s) RETURNING id::text",
        (title, f"{title} body", uuid.uuid4().hex, sensitivity),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _entity(conn: psycopg.Connection[Any], name: str) -> str:
    row = conn.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
        "VALUES (%s, 'project', %s, %s) RETURNING id::text",
        (TENANT, name, name.lower()),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _mention(conn: psycopg.Connection[Any], entity_id: str, doc_id: str) -> None:
    conn.execute(
        "INSERT INTO graph_entity_mentions (tenant_id, entity_id, document_id, source) "
        "VALUES (%s, %s, %s, 'people')",
        (TENANT, entity_id, doc_id),
    )


def _community(conn: psycopg.Connection[Any], summary: str, *members: str) -> str:
    row = conn.execute(
        "INSERT INTO graph_communities (tenant_id, source_graph_hash, members_hash, "
        "member_count, summary) VALUES (%s, 'synthetic', %s, %s, %s) "
        "RETURNING community_key::text",
        (TENANT, uuid.uuid4().hex, len(members), summary),
    ).fetchone()
    assert row is not None
    key = str(row[0])
    for rank, entity_id in enumerate(members):
        conn.execute(
            "INSERT INTO graph_community_members (tenant_id, community_key, entity_id, "
            "member_rank, member_weight) VALUES (%s, %s, %s, %s, 1.0)",
            (TENANT, key, entity_id, rank),
        )
    return key


@pytest.fixture
def graph(test_db: psycopg.Connection[Any]) -> dict[str, str]:
    public_doc = _doc(test_db, "Example Lantern Plan")
    secret_doc = _doc(test_db, "Example Zephyr Reorg", sensitivity="confidential")
    lantern = _entity(test_db, "Project Lantern")
    zephyr = _entity(test_db, "Codename Zephyr")
    _mention(test_db, lantern, public_doc)
    _mention(test_db, zephyr, secret_doc)
    clean = _community(test_db, "Roadmap community around Project Lantern", lantern)
    tainted = _community(test_db, "Roadmap community around Codename Zephyr", zephyr, lantern)
    return {"lantern": lantern, "zephyr": zephyr, "clean": clean, "tainted": tainted}


def _cfg() -> Config:
    return Config(database_url=TEST_DATABASE_URL, graph_tenant_id=TENANT)


# ------------------------------------------------------------- (b) global mode


def test_a_community_with_a_confidential_member_is_named_by_the_shared_rule(
    test_db: psycopg.Connection[Any], graph: dict[str, str]
) -> None:
    tainted = communities_with_confidential_members(
        test_db, TENANT, [graph["clean"], graph["tainted"]]
    )

    assert tainted == {graph["tainted"]}
    assert communities_with_confidential_members(test_db, TENANT, []) == set()


def test_global_mode_never_returns_a_community_with_a_confidential_member(
    test_db: psycopg.Connection[Any], graph: dict[str, str]
) -> None:
    ctx = _retrieve_global(
        test_db, _cfg(), "roadmap community", tenant=TENANT, exclude_confidential=True
    )

    keys = [community.community_key for community in ctx.communities]
    assert keys == [graph["clean"]]
    assert all("Zephyr" not in (c.summary or "") for c in ctx.communities)


def test_global_mode_with_confidential_included_returns_both(
    test_db: psycopg.Connection[Any], graph: dict[str, str]
) -> None:
    ctx = _retrieve_global(
        test_db, _cfg(), "roadmap community", tenant=TENANT, exclude_confidential=False
    )

    assert {c.community_key for c in ctx.communities} == {graph["clean"], graph["tainted"]}


def test_global_mode_fills_its_limit_from_eligible_communities(
    test_db: psycopg.Connection[Any], graph: dict[str, str]
) -> None:
    # The exclusion runs BEFORE the ranking cut, so a limit of 1 still returns one.
    ctx = _retrieve_global(
        test_db, _cfg(), "roadmap community", tenant=TENANT, limit=1, exclude_confidential=True
    )

    assert [c.community_key for c in ctx.communities] == [graph["clean"]]
