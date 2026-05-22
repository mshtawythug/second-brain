"""Unit tests for the GraphBackend Protocol + AgeBackend pure logic (G0-4).

No database: these cover the Protocol value objects, the AGE backend's input
validation, agtype parsing, the per-element tenant filter, and the Python-side
affinity scoring in :meth:`AgeBackend.traverse` (with ``_cypher`` mocked to
return synthetic agtype rows so the scoring branches run deterministically).
Live-AGE behaviour is covered in ``tests/test_graph_backend_age.py``.
"""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg.pq import TransactionStatus
from pytest_mock import MockerFixture

from brain.errors import BrainError, GraphBackendError
from brain.graph_rag.backends import (
    AgeBackend,
    GraphBackend,
    PersonScope,
    TraversalHit,
)
from brain.graph_rag.backends._age_helpers import (
    _agtype_loads,
    _all_same_tenant,
    _edge_weight,
    _inline_set_map,
    _require_autocommit,
)

_A = "11111111-1111-4111-8111-111111111111"
_B = "22222222-2222-4222-8222-222222222222"
_C = "33333333-3333-4333-8333-333333333333"
_D = "44444444-4444-4444-8444-444444444444"


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
def test_traversal_hit_defaults_and_frozen() -> None:
    hit = TraversalHit(entity_uuid=_A, affinity=0.5, hops=2)
    assert hit.entity_uuid == _A
    assert hit.affinity == pytest.approx(0.5)
    assert hit.hops == 2
    assert hit.tenant_id == "default"
    with pytest.raises(dataclasses.FrozenInstanceError):
        hit.affinity = 0.9  # type: ignore[misc]


def test_person_scope_defaults_and_frozen() -> None:
    scope = PersonScope(
        seed_entity_uuid=_A,
        entity_uuids=(_B, _C),
        document_uuids=("d1",),
    )
    assert scope.seed_entity_uuid == _A
    assert scope.entity_uuids == (_B, _C)
    assert scope.document_uuids == ("d1",)
    assert scope.tenant_id == "default"
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.tenant_id = "acme"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# AgeBackend construction / Protocol conformance
# --------------------------------------------------------------------------- #
def test_age_backend_default_graph_name() -> None:
    backend = AgeBackend()
    assert backend.graph_name == "brain_graph"


def test_age_backend_custom_graph_name() -> None:
    assert AgeBackend("tenant_graph_1").graph_name == "tenant_graph_1"


@pytest.mark.parametrize("bad", ["bad-name", "1abc", "", "drop;graph", "a b", 'a"b'])
def test_age_backend_rejects_invalid_graph_name(bad: str) -> None:
    with pytest.raises(GraphBackendError, match="invalid AGE graph name"):
        AgeBackend(bad)


def test_age_backend_is_graph_backend() -> None:
    """AgeBackend conforms structurally to the runtime-checkable Protocol."""
    assert isinstance(AgeBackend(), GraphBackend)


def test_graph_backend_error_is_brain_error() -> None:
    assert issubclass(GraphBackendError, BrainError)


# --------------------------------------------------------------------------- #
# agtype parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", 1),
        ("1.5", 1.5),
        ('"hello"', "hello"),
        ("null", None),
        (None, None),
    ],
)
def test_agtype_loads_scalars(raw: str | None, expected: Any) -> None:
    assert _agtype_loads(raw) == expected


def test_agtype_loads_strips_edge_annotation() -> None:
    raw = '{"id": 1, "label": "CO_OCCURS", "properties": {"weight": 0.8}}::edge'
    parsed = _agtype_loads(raw)
    assert parsed["label"] == "CO_OCCURS"
    assert parsed["properties"]["weight"] == 0.8


def test_agtype_loads_strips_annotations_in_list() -> None:
    raw = (
        '[{"id": 1, "properties": {"tenant_id": "default"}}::edge, '
        '{"id": 2, "properties": {"tenant_id": "default"}}::edge]'
    )
    parsed = _agtype_loads(raw)
    assert isinstance(parsed, list)
    assert len(parsed) == 2


def test_agtype_loads_invalid_raises() -> None:
    with pytest.raises(GraphBackendError, match="could not parse agtype"):
        _agtype_loads("not-json {")


# --------------------------------------------------------------------------- #
# per-element tenant filter + edge weight extraction
# --------------------------------------------------------------------------- #
def _edge(tenant: str, weight: float) -> dict[str, Any]:
    return {
        "id": 1,
        "label": "CO_OCCURS",
        "properties": {"tenant_id": tenant, "weight": weight},
    }


def _node(uuid: str, tenant: str) -> dict[str, Any]:
    return {
        "id": 2,
        "label": "Entity",
        "properties": {"entity_uuid": uuid, "tenant_id": tenant},
    }


def test_all_same_tenant_true_when_uniform() -> None:
    rels = [_edge("default", 0.8)]
    nodes = [_node(_A, "default"), _node(_B, "default")]
    assert _all_same_tenant(rels, nodes, "default") is True


def test_all_same_tenant_false_on_foreign_edge() -> None:
    rels = [_edge("acme", 0.8)]
    nodes = [_node(_A, "default"), _node(_B, "default")]
    assert _all_same_tenant(rels, nodes, "default") is False


def test_all_same_tenant_false_on_foreign_node() -> None:
    rels = [_edge("default", 0.8)]
    nodes = [_node(_A, "default"), _node(_B, "acme")]
    assert _all_same_tenant(rels, nodes, "default") is False


def test_all_same_tenant_empty_is_true() -> None:
    assert _all_same_tenant([], [], "default") is True


def test_edge_weight_extracts_float() -> None:
    assert _edge_weight(_edge("default", 0.42)) == pytest.approx(0.42)


def test_edge_weight_missing_raises() -> None:
    bad = {"id": 1, "label": "CO_OCCURS", "properties": {"tenant_id": "default"}}
    with pytest.raises(GraphBackendError, match="missing required 'weight'"):
        _edge_weight(bad)


# --------------------------------------------------------------------------- #
# _inline_set_map (dynamic property maps; AGE rejects a bare-param SET map)
# --------------------------------------------------------------------------- #
def test_inline_set_map_builds_clause_and_params() -> None:
    clause, params = _inline_set_map(
        {"content_type": "transcript", "sent_at": "2026-05-20"},
        reserved=frozenset(),
        context="document_props",
    )
    assert clause == "{content_type: $p0, sent_at: $p1}"
    assert params == {"p0": "transcript", "p1": "2026-05-20"}


def test_inline_set_map_rejects_injection_key() -> None:
    with pytest.raises(GraphBackendError, match="invalid property key"):
        _inline_set_map(
            {"bad key} DETACH DELETE (x) //": "x"},
            reserved=frozenset(),
            context="document_props",
        )


@pytest.mark.parametrize("reserved_key", ["tenant_id", "document_uuid"])
def test_inline_set_map_rejects_reserved_document_key(reserved_key: str) -> None:
    with pytest.raises(GraphBackendError, match="reserved identity key"):
        _inline_set_map(
            {reserved_key: "evil", "content_type": "note"},
            reserved=frozenset({"tenant_id", "document_uuid"}),
            context="document_props",
        )


@pytest.mark.parametrize("reserved_key", ["tenant_id", "entity_uuid"])
def test_inline_set_map_rejects_reserved_entity_key(reserved_key: str) -> None:
    with pytest.raises(GraphBackendError, match="reserved identity key"):
        _inline_set_map(
            {reserved_key: "evil", "name": "x"},
            reserved=frozenset({"tenant_id", "entity_uuid"}),
            context="entity properties",
        )


def test_inline_set_map_allows_canonical_key_for_entities() -> None:
    """canonical_key is NOT reserved for entities — it is legitimately written."""
    clause, params = _inline_set_map(
        {"name": "Alpha", "canonical_key": "alpha"},
        reserved=frozenset({"tenant_id", "entity_uuid"}),
        context="entity properties",
    )
    assert clause == "{name: $p0, canonical_key: $p1}"
    assert params == {"p0": "Alpha", "p1": "alpha"}


# --------------------------------------------------------------------------- #
# _cypher statement construction (no-params vs params branches)
# --------------------------------------------------------------------------- #
def test_cypher_builds_no_params_statement() -> None:
    backend = AgeBackend()
    conn = _FakeConn()
    result = backend._cypher(conn, "RETURN 1")  # type: ignore[arg-type]
    assert result == []
    built = conn.executed[-1]
    assert "ag_catalog.cypher('brain_graph', $$ RETURN 1 $$)" in built
    assert "%s::ag_catalog.agtype" not in built


def test_cypher_builds_params_statement() -> None:
    backend = AgeBackend()
    conn = _FakeConn()
    backend._cypher(conn, "RETURN $x", {"x": 1})  # type: ignore[arg-type]
    built = conn.executed[-1]
    assert "%s::ag_catalog.agtype" in built


# --------------------------------------------------------------------------- #
# _require_autocommit guard (pure)
# --------------------------------------------------------------------------- #
def test_require_autocommit_passes_when_true() -> None:
    conn = SimpleNamespace(autocommit=True)
    _require_autocommit(conn, "op")  # type: ignore[arg-type]


def test_require_autocommit_raises_when_false() -> None:
    conn = SimpleNamespace(autocommit=False)
    with pytest.raises(GraphBackendError, match="autocommit"):
        _require_autocommit(conn, "AgeBackend.bootstrap")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# traverse Python scoring (with _cypher mocked → no DB)
# --------------------------------------------------------------------------- #
class _FakeConn:
    """Minimal connection stand-in for :meth:`AgeBackend._age_session`.

    ``_age_session`` issues ``SET``/``RESET search_path`` and inspects
    ``info.transaction_status``; this records the SQL it sees and reports a
    healthy (non-error) transaction so the RESET path runs.
    """

    autocommit = True

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)

    def execute(self, statement: str, *args: object) -> Any:
        self.executed.append(statement)
        return SimpleNamespace(fetchall=lambda: [], fetchone=lambda: None)


def _row(
    eid: str,
    edges: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    hops: int,
) -> tuple[str, str, str, str]:
    return (json.dumps(eid), json.dumps(edges) + "::edge", json.dumps(nodes), str(hops))


def test_traverse_scores_affinity_and_dedups_best(mocker: MockerFixture) -> None:
    backend = AgeBackend()
    conn = _FakeConn()
    # Two paths reach _C: a 2-hop (0.8*0.5=0.40) and a worse 1-hop (0.3). Best
    # affinity wins; _B reached at 1 hop (0.8).
    rows = [
        _row(_B, [_edge("default", 0.8)], [_node(_A, "default"), _node(_B, "default")], 1),
        _row(
            _C,
            [_edge("default", 0.8), _edge("default", 0.5)],
            [_node(_A, "default"), _node(_B, "default"), _node(_C, "default")],
            2,
        ),
        _row(_C, [_edge("default", 0.3)], [_node(_A, "default"), _node(_C, "default")], 1),
    ]
    mocker.patch.object(backend, "_cypher", return_value=rows)

    hits = backend.traverse(
        conn,  # type: ignore[arg-type]
        "default",
        _A,
        depth=2,
        frontier_cap=10,
    )

    by_id = {h.entity_uuid: h for h in hits}
    assert by_id[_B].affinity == pytest.approx(0.8)
    assert by_id[_B].hops == 1
    # Best path to _C is the 2-hop 0.40, not the 1-hop 0.30.
    assert by_id[_C].affinity == pytest.approx(0.40)
    assert by_id[_C].hops == 2
    # Ordered by affinity desc.
    assert [h.entity_uuid for h in hits] == [_B, _C]


def test_traverse_skips_seed_belt_and_suspenders(mocker: MockerFixture) -> None:
    """Even if a row's entity is the seed (a cycle slipping past the Cypher
    guard), the Python belt-and-suspenders skip drops it."""
    backend = AgeBackend()
    conn = _FakeConn()
    rows = [
        _row(_A, [_edge("default", 0.9)], [_node(_A, "default"), _node(_A, "default")], 1),
        _row(_B, [_edge("default", 0.7)], [_node(_A, "default"), _node(_B, "default")], 1),
    ]
    mocker.patch.object(backend, "_cypher", return_value=rows)

    hits = backend.traverse(conn, "default", _A, depth=2, frontier_cap=10)  # type: ignore[arg-type]

    assert [h.entity_uuid for h in hits] == [_B]  # seed _A never returned


def test_traverse_skips_cross_tenant_path(mocker: MockerFixture) -> None:
    backend = AgeBackend()
    conn = _FakeConn()
    rows = [
        # A path whose edge belongs to another tenant — Python defence drops it.
        _row(_B, [_edge("acme", 0.9)], [_node(_A, "default"), _node(_B, "default")], 1),
        _row(_C, [_edge("default", 0.7)], [_node(_A, "default"), _node(_C, "default")], 1),
    ]
    mocker.patch.object(backend, "_cypher", return_value=rows)

    hits = backend.traverse(conn, "default", _A, depth=2, frontier_cap=10)  # type: ignore[arg-type]

    assert [h.entity_uuid for h in hits] == [_C]


def test_traverse_applies_min_edge_weight(mocker: MockerFixture) -> None:
    backend = AgeBackend()
    conn = _FakeConn()
    rows = [
        _row(_B, [_edge("default", 0.2)], [_node(_A, "default"), _node(_B, "default")], 1),
        _row(_C, [_edge("default", 0.9)], [_node(_A, "default"), _node(_C, "default")], 1),
    ]
    mocker.patch.object(backend, "_cypher", return_value=rows)

    hits = backend.traverse(
        conn,  # type: ignore[arg-type]
        "default",
        _A,
        depth=2,
        frontier_cap=10,
        min_edge_weight=0.5,
    )

    assert [h.entity_uuid for h in hits] == [_C]


def test_traverse_caps_frontier(mocker: MockerFixture) -> None:
    backend = AgeBackend()
    conn = _FakeConn()
    rows = [
        _row(_B, [_edge("default", 0.9)], [_node(_A, "default"), _node(_B, "default")], 1),
        _row(_C, [_edge("default", 0.8)], [_node(_A, "default"), _node(_C, "default")], 1),
    ]
    mocker.patch.object(backend, "_cypher", return_value=rows)

    hits = backend.traverse(conn, "default", _A, depth=2, frontier_cap=1)  # type: ignore[arg-type]

    assert len(hits) == 1
    assert hits[0].entity_uuid == _B  # highest affinity kept


def test_traverse_cap_keeps_best_when_best_is_row_order_last(
    mocker: MockerFixture,
) -> None:
    """Cap is applied AFTER scoring: the strongest path is kept even when AGE
    returns it LAST. A pre-scoring cap would keep the first (weak) row — so this
    test fails against a pre-scoring-cap implementation and passes only with
    cap-after-scoring."""
    backend = AgeBackend()
    conn = _FakeConn()
    rows = [
        _row(_B, [_edge("default", 0.2)], [_node(_A, "default"), _node(_B, "default")], 1),
        _row(_C, [_edge("default", 0.4)], [_node(_A, "default"), _node(_C, "default")], 1),
        _row(_D, [_edge("default", 0.95)], [_node(_A, "default"), _node(_D, "default")], 1),
    ]
    mocker.patch.object(backend, "_cypher", return_value=rows)

    hits = backend.traverse(conn, "default", _A, depth=2, frontier_cap=1)  # type: ignore[arg-type]

    assert [h.entity_uuid for h in hits] == [_D]  # strongest, despite being last


def test_traverse_tie_break_by_entity_uuid(mocker: MockerFixture) -> None:
    """Equal affinity → deterministic order by entity_uuid ASC, so the
    top-frontier_cap selection is reproducible (not AGE row order)."""
    backend = AgeBackend()
    conn = _FakeConn()
    # Row order is _C then _B, but equal affinity → sorted [_B, _C] (B < C).
    rows = [
        _row(_C, [_edge("default", 0.5)], [_node(_A, "default"), _node(_C, "default")], 1),
        _row(_B, [_edge("default", 0.5)], [_node(_A, "default"), _node(_B, "default")], 1),
    ]
    mocker.patch.object(backend, "_cypher", return_value=rows)

    full = backend.traverse(conn, "default", _A, depth=2, frontier_cap=10)  # type: ignore[arg-type]
    assert [h.entity_uuid for h in full] == [_B, _C]  # B < C despite row order
    top1 = backend.traverse(conn, "default", _A, depth=2, frontier_cap=1)  # type: ignore[arg-type]
    assert [h.entity_uuid for h in top1] == [_B]  # tie-break winner, not row order


def test_traverse_raises_on_path_overflow(mocker: MockerFixture) -> None:
    """If more within-depth paths exist than the safe bound, traverse() raises
    rather than returning a silently-truncated (possibly wrong) result.

    frontier_cap=1 → bound = max(1*50, 5000) = 5000; returning 5001 rows trips
    the overflow guard BEFORE any scoring (so the dummy rows are never parsed)."""
    backend = AgeBackend()
    conn = _FakeConn()
    overflow_rows = [("a", "b", "c", "1")] * 5001
    mocker.patch.object(backend, "_cypher", return_value=overflow_rows)

    with pytest.raises(GraphBackendError, match="exceeded safe path bound"):
        backend.traverse(conn, "default", _A, depth=2, frontier_cap=1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"depth": 0, "frontier_cap": 10}, "depth must be >= 1"),
        ({"depth": 2, "frontier_cap": 0}, "frontier_cap must be >= 1"),
        ({"depth": 2, "frontier_cap": 10, "min_edge_weight": 1.5}, "min_edge_weight"),
        ({"depth": 2, "frontier_cap": 10, "min_edge_weight": -0.1}, "min_edge_weight"),
    ],
)
def test_traverse_validates_params(kwargs: dict[str, Any], match: str) -> None:
    backend = AgeBackend()
    conn = _FakeConn()
    with pytest.raises(GraphBackendError, match=match):
        backend.traverse(conn, "default", _A, **kwargs)  # type: ignore[arg-type]


def test_scope_person_validates_frontier_cap() -> None:
    backend = AgeBackend()
    conn = _FakeConn()
    with pytest.raises(GraphBackendError, match="frontier_cap must be >= 1"):
        backend.scope_person(conn, "default", _A, frontier_cap=0)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# traverse / scope_person reject non-int caps BEFORE Cypher interpolation
# (type hints are not an injection boundary; bool is an int subclass)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["2", 2.0, True, None])
def test_traverse_rejects_non_int_depth(bad: Any) -> None:
    backend = AgeBackend()
    conn = _FakeConn()
    with pytest.raises(GraphBackendError, match="depth must be an int"):
        backend.traverse(conn, "default", _A, depth=bad, frontier_cap=10)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["2", 2.0, True, None])
def test_traverse_rejects_non_int_frontier_cap(bad: Any) -> None:
    backend = AgeBackend()
    conn = _FakeConn()
    with pytest.raises(GraphBackendError, match="frontier_cap must be an int"):
        backend.traverse(conn, "default", _A, depth=2, frontier_cap=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["x", None, True])
def test_traverse_rejects_non_numeric_min_edge_weight(bad: Any) -> None:
    backend = AgeBackend()
    conn = _FakeConn()
    with pytest.raises(GraphBackendError, match="min_edge_weight must be a number"):
        backend.traverse(
            conn,  # type: ignore[arg-type]
            "default",
            _A,
            depth=2,
            frontier_cap=10,
            min_edge_weight=bad,
        )


@pytest.mark.parametrize("bad", ["5", 5.0, True, None])
def test_scope_person_rejects_non_int_frontier_cap(bad: Any) -> None:
    backend = AgeBackend()
    conn = _FakeConn()
    with pytest.raises(GraphBackendError, match="frontier_cap must be an int"):
        backend.scope_person(conn, "default", _A, frontier_cap=bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# _age_session search_path lifecycle (both finally branches)
# --------------------------------------------------------------------------- #
def test_age_session_sets_and_resets_search_path() -> None:
    backend = AgeBackend()
    conn = _FakeConn()
    with backend._age_session(conn):  # type: ignore[arg-type]
        pass
    assert any("SET search_path = ag_catalog" in s for s in conn.executed)
    assert any(s == "RESET search_path" for s in conn.executed)


def test_age_session_skips_reset_on_aborted_transaction() -> None:
    """When the transaction aborted in the block, RESET is skipped (the caller's
    rollback restores search_path; issuing SQL on an aborted txn would error)."""
    backend = AgeBackend()
    conn = _FakeConn()
    conn.info.transaction_status = TransactionStatus.INERROR
    with pytest.raises(RuntimeError, match="boom"):  # noqa: SIM117 - clarity
        with backend._age_session(conn):  # type: ignore[arg-type]
            raise RuntimeError("boom")
    assert any("SET search_path = ag_catalog" in s for s in conn.executed)
    assert not any("RESET" in s for s in conn.executed)
