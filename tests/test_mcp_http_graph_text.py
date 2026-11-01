"""Graph FREE TEXT stays home too: theme and community summaries (RM-46, rev-118 round 2).

sec-118 #2c filtered entity NAMES out of graph results over HTTP, but a summary
written from those names (a synthesized theme) or from confidential titles (a
stored community summary) still carried them.

- Themes, over HTTP without ``--include-confidential``: a theme that lost an
  entity to the filter loses its summary (it was written from that name), and a
  theme left with no public entity is dropped. Chosen over re-synthesizing from
  the public names, which would mean threading the policy into the themes
  builder; the cost is an LLM call whose text is thrown away.
- Global mode, stdio AND HTTP: a community any of whose members a confidential
  document mentions is excluded, before the ranking cut. That is
  ``list_communities``' rule, applied to the retrieval that returns the same
  stored summaries (F6 consistency).
- ``graph_rag_search(public_entities_only=…)`` is the one switch, used by the MCP
  graph tools and by ``brain_ask``'s graph modes (whose answer is written from
  those labels).

All names, titles and summaries are synthetic. Runs against the test DB on 5434.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

import brain.graph_rag as graph_rag_pkg
from brain import ask as ask_mod
from brain.config import Config
from brain.graph_rag import retrieve
from brain.graph_rag.communities import communities_with_confidential_members
from brain.graph_rag.global_ import _retrieve_global
from brain.graph_rag.public_entities import withhold_confidential_entities
from brain.graph_rag.schema import CommunityGroup, GraphContext, GraphEntity, ThemeGroup
from tests.conftest import TEST_DATABASE_URL, FakeEmbedder

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


# --------------------------------------------------- (a) themes and their summaries


def _e(entity_id: str, name: str) -> GraphEntity:
    return GraphEntity(id=entity_id, entity_type="project", name=name, canonical_key=name.lower())


def test_a_theme_summary_written_from_a_withheld_name_is_dropped(
    test_db: psycopg.Connection[Any], graph: dict[str, str]
) -> None:
    lantern = _e(graph["lantern"], "Project Lantern")
    zephyr = _e(graph["zephyr"], "Codename Zephyr")
    ctx = GraphContext(
        session_id="s",
        mode="themes",
        query="q",
        themes=[
            ThemeGroup(group_id=1, entities=[lantern, zephyr], summary="Lantern and Zephyr"),
            ThemeGroup(group_id=2, entities=[zephyr], summary="Only Zephyr"),
            ThemeGroup(group_id=3, entities=[lantern], summary="Only Lantern"),
        ],
        communities=[
            CommunityGroup(community_key="k1", entities=[lantern, zephyr], summary="Both"),
            CommunityGroup(community_key="k2", entities=[lantern], summary="Lantern"),
        ],
    )

    filtered = withhold_confidential_entities(test_db, ctx)

    assert [(t.group_id, t.summary) for t in filtered.themes] == [(1, None), (3, "Only Lantern")]
    assert [e.name for e in filtered.themes[0].entities] == ["Project Lantern"]
    assert [c.community_key for c in filtered.communities] == ["k2"]
    assert "Zephyr" not in repr(filtered)


# ------------------------------------------------- the one switch, and its users


def test_graph_rag_search_applies_the_switch_to_any_mode(
    test_db: psycopg.Connection[Any], graph: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    zephyr = _e(graph["zephyr"], "Codename Zephyr")
    lantern = _e(graph["lantern"], "Project Lantern")

    def fake_local(*_: Any, **__: Any) -> GraphContext:
        return GraphContext(session_id="s", mode="local", query="q", entities=[zephyr, lantern])

    monkeypatch.setattr(retrieve, "_retrieve_local", fake_local)
    names = {}
    for public_only in (True, False):
        ctx = retrieve.graph_rag_search(
            test_db, _cfg(), "q", backend=object(), mode="local",  # type: ignore[arg-type]
            public_entities_only=public_only,
        )
        names[public_only] = [e.name for e in ctx.entities]

    assert names[True] == ["Project Lantern"]
    assert names[False] == ["Codename Zephyr", "Project Lantern"]


def test_ask_graph_modes_pass_the_switch_to_graph_retrieval(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    def fake_search(*_: Any, **kwargs: Any) -> GraphContext:
        seen.append(kwargs["public_entities_only"])
        return GraphContext(session_id="s", mode="local", query="q")

    monkeypatch.setattr(graph_rag_pkg, "graph_rag_search", fake_search)
    for public_only in (True, False):
        ask_mod._retrieve(
            test_db, _cfg(), embedder=FakeEmbedder(), query="q", limit=3, mode="local",
            backend=object(), exclude_confidential=True,  # type: ignore[arg-type]
            public_entities_only=public_only,
        )

    assert seen == [True, False]


@pytest.mark.parametrize("hide", [True, False])
def test_brain_ask_graph_mode_passes_the_mode_from_mcp_state(
    monkeypatch: pytest.MonkeyPatch, hide: bool
) -> None:
    from brain import mcp_server

    seen: list[bool] = []

    def fake_ask(*_: Any, **kwargs: Any) -> ask_mod.AskResult:
        seen.append(kwargs["public_entities_only"])
        return ask_mod.AskResult(
            answer="synthetic", citations=[], iterations_used=1, sub_queries=["q"],
            fallback_used=False, session_id=uuid.uuid4().hex,
        )

    state = mcp_server._State(
        cfg=_cfg(), embedder=FakeEmbedder(), hide_confidential=hide  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    monkeypatch.setattr(ask_mod, "ask", fake_ask)

    mcp_server.brain_ask(question="q", mode="local")

    assert seen == [hide]
