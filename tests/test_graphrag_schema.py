"""Unit tests for the GraphRAG value objects (:mod:`brain.graph_rag.schema`).

Pure-logic tests: construction, defaults, frozen immutability, and independence
of the ``default_factory`` mutable defaults. No DB. UUIDs are synthetic strings.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from brain.graph_rag.schema import (
    Edge,
    EdgeContribution,
    EntityMention,
    GraphContext,
    GraphEntity,
    GraphExplanation,
    ThemeGroup,
)
from brain.search import SearchResult

_ENT_A = "11111111-1111-4111-8111-111111111111"
_ENT_B = "22222222-2222-4222-8222-222222222222"
_DOC = "33333333-3333-4333-8333-333333333333"


def _entity(**overrides: object) -> GraphEntity:
    """Build a minimal :class:`GraphEntity` with overridable fields."""
    base: dict[str, object] = {
        "id": _ENT_A,
        "entity_type": "person",
        "name": "Person A",
        "canonical_key": "person-a",
    }
    base.update(overrides)
    return GraphEntity(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# GraphEntity
# --------------------------------------------------------------------------- #
def test_graph_entity_construction_and_defaults() -> None:
    ent = _entity()
    assert ent.id == _ENT_A
    assert ent.entity_type == "person"
    assert ent.name == "Person A"
    assert ent.canonical_key == "person-a"
    # Defaults.
    assert ent.tenant_id == "default"
    assert ent.description is None
    assert ent.doc_count == 0
    assert ent.properties == {}
    assert ent.created_at is None
    assert ent.updated_at is None


def test_graph_entity_accepts_explicit_tenant() -> None:
    """``tenant_id`` defaults to ``"default"`` but is overridable (spec §4 D9)."""
    ent = _entity(tenant_id="acme")
    assert ent.tenant_id == "acme"


def test_graph_entity_is_frozen() -> None:
    ent = _entity()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ent.name = "Changed"  # type: ignore[misc]


def test_graph_entity_properties_default_is_independent() -> None:
    """Each instance gets its own ``properties`` dict (default_factory)."""
    a = _entity()
    b = _entity()
    assert a.properties is not b.properties


def test_graph_entity_accepts_full_payload() -> None:
    now = datetime(2026, 5, 20, tzinfo=UTC)
    ent = _entity(
        entity_type="topic",
        tenant_id="acme",
        description="a topic",
        doc_count=7,
        properties={"alias": ["x"]},
        created_at=now,
        updated_at=now,
    )
    assert ent.entity_type == "topic"
    assert ent.tenant_id == "acme"
    assert ent.description == "a topic"
    assert ent.doc_count == 7
    assert ent.properties == {"alias": ["x"]}
    assert ent.created_at == now
    assert ent.updated_at == now


# --------------------------------------------------------------------------- #
# EntityMention
# --------------------------------------------------------------------------- #
def test_entity_mention_construction_and_default_count() -> None:
    m = EntityMention(entity_id=_ENT_A, document_id=_DOC, source="people")
    assert m.entity_id == _ENT_A
    assert m.document_id == _DOC
    assert m.source == "people"
    assert m.tenant_id == "default"
    assert m.mention_count == 1


def test_entity_mention_accepts_explicit_tenant() -> None:
    m = EntityMention(
        entity_id=_ENT_A, document_id=_DOC, source="people", tenant_id="acme"
    )
    assert m.tenant_id == "acme"


def test_entity_mention_is_frozen() -> None:
    m = EntityMention(entity_id=_ENT_A, document_id=_DOC, source="people")
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.mention_count = 9  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# EdgeContribution
# --------------------------------------------------------------------------- #
def test_edge_contribution_construction_and_default_count() -> None:
    c = EdgeContribution(document_id=_DOC, src_id=_ENT_A, dst_id=_ENT_B)
    assert c.document_id == _DOC
    assert c.src_id == _ENT_A
    assert c.dst_id == _ENT_B
    assert c.tenant_id == "default"
    assert c.cooccur_count == 1


def test_edge_contribution_accepts_explicit_tenant() -> None:
    c = EdgeContribution(
        document_id=_DOC, src_id=_ENT_A, dst_id=_ENT_B, tenant_id="acme"
    )
    assert c.tenant_id == "acme"


def test_edge_contribution_is_frozen() -> None:
    c = EdgeContribution(document_id=_DOC, src_id=_ENT_A, dst_id=_ENT_B)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.cooccur_count = 3  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Edge (aggregate relationship)
# --------------------------------------------------------------------------- #
def test_edge_construction_and_defaults() -> None:
    e = Edge(src_id=_ENT_A, dst_id=_ENT_B, weight=0.42)
    assert e.src_id == _ENT_A
    assert e.dst_id == _ENT_B
    assert e.weight == pytest.approx(0.42)
    assert e.tenant_id == "default"
    assert e.rel_type == "co_occurs"
    assert e.co_count == 0
    assert e.doc_count == 0
    assert e.updated_at is None


def test_edge_accepts_explicit_tenant() -> None:
    e = Edge(src_id=_ENT_A, dst_id=_ENT_B, weight=0.42, tenant_id="acme")
    assert e.tenant_id == "acme"


def test_edge_is_frozen() -> None:
    e = Edge(src_id=_ENT_A, dst_id=_ENT_B, weight=0.5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.weight = 0.1  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# ThemeGroup
# --------------------------------------------------------------------------- #
def test_theme_group_construction_and_defaults() -> None:
    g = ThemeGroup(group_id=0)
    assert g.group_id == 0
    assert g.entities == []
    assert g.doc_ids == []
    assert g.score == 0.0
    assert g.summary is None


def test_theme_group_mutable_defaults_are_independent() -> None:
    a = ThemeGroup(group_id=0)
    b = ThemeGroup(group_id=1)
    assert a.entities is not b.entities
    assert a.doc_ids is not b.doc_ids


def test_theme_group_holds_entities_and_docs() -> None:
    g = ThemeGroup(
        group_id=2,
        entities=[_entity()],
        doc_ids=[_DOC],
        score=1.5,
        summary="a theme",
    )
    assert g.entities[0].canonical_key == "person-a"
    assert g.doc_ids == [_DOC]
    assert g.score == pytest.approx(1.5)
    assert g.summary == "a theme"


def test_theme_group_is_frozen() -> None:
    g = ThemeGroup(group_id=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        g.score = 2.0  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# GraphExplanation
# --------------------------------------------------------------------------- #
def test_graph_explanation_construction_and_defaults() -> None:
    ex = GraphExplanation(mode="local")
    assert ex.mode == "local"
    assert ex.tenant_id == "default"
    assert ex.seed_entity_ids == []
    assert ex.person_keys == []
    assert ex.depth == 0
    assert ex.frontier_cap == 0
    assert ex.min_edge_weight == 0.0
    assert ex.nodes_visited == 0
    assert ex.edges_considered == 0
    assert ex.generic_df_cap is None
    assert ex.matched_filters == {}


def test_graph_explanation_mutable_defaults_are_independent() -> None:
    a = GraphExplanation(mode="themes")
    b = GraphExplanation(mode="themes")
    assert a.seed_entity_ids is not b.seed_entity_ids
    assert a.matched_filters is not b.matched_filters


def test_graph_explanation_is_frozen() -> None:
    ex = GraphExplanation(mode="local")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ex.depth = 3  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# GraphContext
# --------------------------------------------------------------------------- #
def test_graph_context_construction_and_defaults() -> None:
    ctx = GraphContext(session_id="sess-1", mode="local", query="q")
    assert ctx.session_id == "sess-1"
    assert ctx.mode == "local"
    assert ctx.query == "q"
    assert ctx.tenant_id == "default"
    assert ctx.person is None
    assert ctx.themes == []
    assert ctx.entities == []
    assert ctx.docs == []
    assert ctx.explanation is None


def test_graph_context_mutable_defaults_are_independent() -> None:
    a = GraphContext(session_id="s", mode="local", query="q")
    b = GraphContext(session_id="s", mode="local", query="q")
    assert a.themes is not b.themes
    assert a.entities is not b.entities
    assert a.docs is not b.docs


def test_graph_context_is_frozen() -> None:
    ctx = GraphContext(session_id="s", mode="local", query="q")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.mode = "themes"  # type: ignore[misc]


def test_graph_context_carries_full_payload() -> None:
    """``docs`` reuses :class:`SearchResult`; themes/entities/explanation ride along."""
    hit = SearchResult(
        document_id=_DOC,
        title="A doc",
        source_kind="manual",
        snippet="snip",
        score=0.9,
        content_type="note",
        tags=["t"],
    )
    ctx = GraphContext(
        session_id="sess-2",
        mode="themes",
        query="themes with X",
        tenant_id="acme",
        person="Person A",
        themes=[ThemeGroup(group_id=0, entities=[_entity(tenant_id="acme")])],
        entities=[_entity(tenant_id="acme")],
        docs=[hit],
        explanation=GraphExplanation(
            mode="themes", tenant_id="acme", person_keys=["person-a"]
        ),
    )
    assert ctx.tenant_id == "acme"
    assert ctx.person == "Person A"
    assert ctx.themes[0].entities[0].canonical_key == "person-a"
    assert ctx.docs[0].document_id == _DOC
    assert ctx.explanation is not None
    assert ctx.explanation.tenant_id == "acme"
    assert ctx.explanation.person_keys == ["person-a"]
