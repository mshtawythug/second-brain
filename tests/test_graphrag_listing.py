"""Tests for the GraphRAG entity-listing and graph-stats surfaces (plan 2026-05-23).

Three layers:

* **Unit / relational** (real ``test_db`` fixture against port 5434) —
  :func:`brain.graph_rag.relational.list_entities` type-filter, both sort
  orders, limit (including ``limit=0`` = all), bad ``entity_type``/``sort``
  raises :class:`brain.errors.GraphBackendError`, and
  :func:`brain.graph_rag.relational.graph_stats` counts correctness.
* **CLI** — ``brain graphrag entities`` (human table + ``--json``, type filter,
  sort, tenant scoping, AGE-absent) and ``brain graphrag stats`` (human + json).
* **MCP** — ``brain_graphrag_entities`` + ``brain_graphrag_stats`` parity with
  the CLI ``--json`` payloads.

All entity names are synthetic (Acme Corp, Project Falcon, Jane Doe…); no PII.
Schema + AGE graph reset per-test via the ``test_db`` fixture.
"""
from __future__ import annotations

import json as _json
import os
from typing import Any

import psycopg
import pytest
from mcp.types import INTERNAL_ERROR
from typer.testing import CliRunner

from brain import mcp_server
from brain.cli import app
from brain.config import Config
from brain.errors import GraphBackendError
from brain.graph_rag.relational import graph_stats, list_entities
from brain.graph_rag.schema import EntitySummary, GraphStats
from brain.mcp_compat import MCPError
from tests.conftest import FakeEmbedder

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #
def _insert_entity(
    conn: psycopg.Connection[Any],
    tenant: str,
    entity_type: str,
    name: str,
    canonical_key: str,
    *,
    doc_count: int = 0,
    description: str | None = None,
) -> str:
    """Insert one graph_entities row and return its uuid text."""
    row = conn.execute(
        "INSERT INTO graph_entities "
        "(tenant_id, entity_type, name, canonical_key, doc_count, description) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id::text",
        (tenant, entity_type, name, canonical_key, doc_count, description),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _insert_rel(
    conn: psycopg.Connection[Any], tenant: str, a: str, b: str, weight: float = 0.5
) -> None:
    """Insert a synthetic co-occurs relationship (canonical src < dst)."""
    src, dst = sorted((a, b))
    conn.execute(
        "INSERT INTO graph_relationships "
        "(tenant_id, src_id, dst_id, rel_type, weight, co_count, doc_count) "
        "VALUES (%s, %s, %s, 'co_occurs', %s, 1, 1)",
        (tenant, src, dst, weight),
    )


def _seed_mixed_entities(
    conn: psycopg.Connection[Any], tenant: str = "default"
) -> list[str]:
    """Seed 5 entities across 3 types and return their ids in insertion order.

    Counts by type: 2 org, 1 project, 2 person.
    doc_counts: Acme Corp=5, Beta Inc=2, Project Falcon=8, Jane Doe=10, Bob Smith=3.
    """
    ids: list[str] = []
    ids.append(
        _insert_entity(conn, tenant, "org", "Acme Corp", "acme-corp", doc_count=5)
    )
    ids.append(
        _insert_entity(conn, tenant, "org", "Beta Inc", "beta-inc", doc_count=2)
    )
    ids.append(
        _insert_entity(
            conn,
            tenant,
            "project",
            "Project Falcon",
            "project-falcon",
            doc_count=8,
            description="A top-secret initiative",
        )
    )
    ids.append(
        _insert_entity(conn, tenant, "person", "Jane Doe", "jane-doe", doc_count=10)
    )
    ids.append(
        _insert_entity(conn, tenant, "person", "Bob Smith", "bob-smith", doc_count=3)
    )
    return ids


def _cfg(tenant: str = "default") -> Config:
    return Config(database_url=TEST_DATABASE_URL, graph_tenant_id=tenant)


# --------------------------------------------------------------------------- #
# Unit — list_entities
# --------------------------------------------------------------------------- #
def test_list_entities_all_types_default(test_db: psycopg.Connection[Any]) -> None:
    """Default call returns all entities sorted by doc_count DESC, name ASC."""
    # Arrange
    _seed_mixed_entities(test_db)

    # Act
    rows = list_entities(test_db, "default")

    # Assert
    assert len(rows) == 5
    assert isinstance(rows[0], EntitySummary)
    # First row: highest doc_count → Jane Doe (10)
    assert rows[0].name == "Jane Doe"
    assert rows[0].doc_count == 10
    assert rows[0].entity_type == "person"


def test_list_entities_type_filter_org(test_db: psycopg.Connection[Any]) -> None:
    """entity_type='org' returns only org rows."""
    # Arrange
    _seed_mixed_entities(test_db)

    # Act
    rows = list_entities(test_db, "default", entity_type="org")

    # Assert
    assert len(rows) == 2
    assert all(r.entity_type == "org" for r in rows)


def test_list_entities_type_filter_person(test_db: psycopg.Connection[Any]) -> None:
    """entity_type='person' returns only person rows in doc_count order."""
    # Arrange
    _seed_mixed_entities(test_db)

    # Act
    rows = list_entities(test_db, "default", entity_type="person")

    # Assert
    assert len(rows) == 2
    assert all(r.entity_type == "person" for r in rows)
    assert rows[0].name == "Jane Doe"   # doc_count=10
    assert rows[1].name == "Bob Smith"  # doc_count=3


def test_list_entities_type_filter_project_with_description(
    test_db: psycopg.Connection[Any],
) -> None:
    """entity_type='project' row carries its description."""
    # Arrange
    _seed_mixed_entities(test_db)

    # Act
    rows = list_entities(test_db, "default", entity_type="project")

    # Assert
    assert len(rows) == 1
    assert rows[0].name == "Project Falcon"
    assert rows[0].description == "A top-secret initiative"


def test_list_entities_type_filter_absent_type_returns_empty(
    test_db: psycopg.Connection[Any],
) -> None:
    """Filtering by a valid type with no matching rows returns []."""
    # Arrange
    _seed_mixed_entities(test_db)

    # Act
    rows = list_entities(test_db, "default", entity_type="tool")

    # Assert
    assert rows == []


def test_list_entities_sort_name(test_db: psycopg.Connection[Any]) -> None:
    """sort='name' returns rows in strict alphabetical order."""
    # Arrange
    _seed_mixed_entities(test_db)

    # Act
    rows = list_entities(test_db, "default", sort="name", limit=0)

    # Assert
    names = [r.name for r in rows]
    assert names == sorted(names)


def test_list_entities_sort_docs(test_db: psycopg.Connection[Any]) -> None:
    """sort='docs' returns rows with doc_count non-increasing."""
    # Arrange
    _seed_mixed_entities(test_db)

    # Act
    rows = list_entities(test_db, "default", sort="docs", limit=0)

    # Assert
    doc_counts = [r.doc_count for r in rows]
    assert doc_counts == sorted(doc_counts, reverse=True)


def test_list_entities_limit_applied(test_db: psycopg.Connection[Any]) -> None:
    """limit=2 returns exactly 2 rows."""
    # Arrange
    _seed_mixed_entities(test_db)

    # Act
    rows = list_entities(test_db, "default", limit=2)

    # Assert
    assert len(rows) == 2


def test_list_entities_limit_zero_returns_all(
    test_db: psycopg.Connection[Any],
) -> None:
    """limit=0 omits the LIMIT clause and returns all rows."""
    # Arrange
    _seed_mixed_entities(test_db)

    # Act
    rows = list_entities(test_db, "default", limit=0)

    # Assert
    assert len(rows) == 5


def test_list_entities_bad_entity_type_raises(
    test_db: psycopg.Connection[Any],
) -> None:
    """Invalid entity_type raises GraphBackendError before any DB round-trip."""
    with pytest.raises(GraphBackendError, match="invalid entity_type"):
        list_entities(test_db, "default", entity_type="banana")


def test_list_entities_bad_sort_raises(test_db: psycopg.Connection[Any]) -> None:
    """Invalid sort raises GraphBackendError before any DB round-trip."""
    with pytest.raises(GraphBackendError, match="invalid sort"):
        list_entities(test_db, "default", sort="popularity")


def test_list_entities_tenant_scoped(test_db: psycopg.Connection[Any]) -> None:
    """list_entities is tenant-scoped — seeded tenant ≠ default."""
    # Arrange
    _seed_mixed_entities(test_db, tenant="custom")

    # Act
    default_rows = list_entities(test_db, "default")
    custom_rows = list_entities(test_db, "custom")

    # Assert
    assert default_rows == []
    assert len(custom_rows) == 5


# --------------------------------------------------------------------------- #
# Unit — graph_stats
# --------------------------------------------------------------------------- #
def test_graph_stats_empty_graph(test_db: psycopg.Connection[Any]) -> None:
    """An empty tenant returns zero counts and empty top_entities."""
    # Act
    stats = graph_stats(test_db, "default")

    # Assert
    assert isinstance(stats, GraphStats)
    assert stats.total_entities == 0
    assert stats.total_relationships == 0
    assert stats.total_communities == 0
    assert stats.counts_by_type == {}
    assert stats.top_entities == ()


def test_graph_stats_counts_by_type(test_db: psycopg.Connection[Any]) -> None:
    """counts_by_type groups correctly; total_entities is the sum."""
    # Arrange
    _seed_mixed_entities(test_db)  # 2 org + 1 project + 2 person

    # Act
    stats = graph_stats(test_db, "default")

    # Assert
    assert stats.counts_by_type["org"] == 2
    assert stats.counts_by_type["project"] == 1
    assert stats.counts_by_type["person"] == 2
    assert stats.total_entities == 5
    assert "tool" not in stats.counts_by_type


def test_graph_stats_counts_relationships(test_db: psycopg.Connection[Any]) -> None:
    """total_relationships counts graph_relationships rows for the tenant."""
    # Arrange
    ids = _seed_mixed_entities(test_db)
    _insert_rel(test_db, "default", ids[0], ids[1])
    _insert_rel(test_db, "default", ids[2], ids[3])

    # Act
    stats = graph_stats(test_db, "default")

    # Assert
    assert stats.total_relationships == 2


def test_graph_stats_top_entities_ordered(test_db: psycopg.Connection[Any]) -> None:
    """top_entities are the top-N by doc_count DESC, name ASC."""
    # Arrange
    _seed_mixed_entities(test_db)

    # Act
    stats = graph_stats(test_db, "default")

    # Assert — all 5 fit in the top-10 slot
    assert len(stats.top_entities) == 5
    assert stats.top_entities[0].name == "Jane Doe"         # doc_count=10
    assert stats.top_entities[1].name == "Project Falcon"   # doc_count=8


def test_graph_stats_tenant_scoped(test_db: psycopg.Connection[Any]) -> None:
    """graph_stats is tenant-scoped."""
    # Arrange
    _seed_mixed_entities(test_db, tenant="alpha")

    # Act
    default_stats = graph_stats(test_db, "default")
    alpha_stats = graph_stats(test_db, "alpha")

    # Assert
    assert default_stats.total_entities == 0
    assert alpha_stats.total_entities == 5


# --------------------------------------------------------------------------- #
# CLI — brain graphrag entities
# --------------------------------------------------------------------------- #
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)


def test_cli_entities_json(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``entities --json`` returns the correct envelope and all entities."""
    # Arrange
    _seed_mixed_entities(test_db)
    _env(monkeypatch)

    # Act
    res = CliRunner().invoke(app, ["graphrag", "entities", "--json"])

    # Assert
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.stdout)
    assert payload["tenant_id"] == "default"
    assert payload["count"] == 5
    assert len(payload["entities"]) == 5
    sample = payload["entities"][0]
    for key in ("entity_type", "name", "canonical_key", "doc_count", "description"):
        assert key in sample, key
    assert "cypher" not in res.stdout.lower()


def test_cli_entities_json_type_filter(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``entities --type org --json`` returns only org entities."""
    # Arrange
    _seed_mixed_entities(test_db)
    _env(monkeypatch)

    # Act
    res = CliRunner().invoke(app, ["graphrag", "entities", "--type", "org", "--json"])

    # Assert
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.stdout)
    assert payload["count"] == 2
    assert all(e["entity_type"] == "org" for e in payload["entities"])


def test_cli_entities_json_sort_name(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``entities --sort name -n 0 --json`` returns entities alphabetically."""
    # Arrange
    _seed_mixed_entities(test_db)
    _env(monkeypatch)

    # Act
    res = CliRunner().invoke(
        app, ["graphrag", "entities", "--sort", "name", "-n", "0", "--json"]
    )

    # Assert
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.stdout)
    names = [e["name"] for e in payload["entities"]]
    assert names == sorted(names)


def test_cli_entities_json_limit(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``entities -n 2 --json`` returns at most 2 entities."""
    # Arrange
    _seed_mixed_entities(test_db)
    _env(monkeypatch)

    # Act
    res = CliRunner().invoke(app, ["graphrag", "entities", "-n", "2", "--json"])

    # Assert
    assert res.exit_code == 0, res.output
    assert _json.loads(res.stdout)["count"] == 2


def test_cli_entities_json_limit_zero(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``entities -n 0 --json`` returns all entities."""
    # Arrange
    _seed_mixed_entities(test_db)
    _env(monkeypatch)

    # Act
    res = CliRunner().invoke(app, ["graphrag", "entities", "-n", "0", "--json"])

    # Assert
    assert res.exit_code == 0, res.output
    assert _json.loads(res.stdout)["count"] == 5


def test_cli_entities_human_table(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The human render includes the 'Entities' table header and a known name."""
    # Arrange
    _seed_mixed_entities(test_db)
    _env(monkeypatch)

    # Act
    res = CliRunner().invoke(app, ["graphrag", "entities"], env={"COLUMNS": "200"})

    # Assert
    assert res.exit_code == 0, res.output
    assert "Entities" in res.output
    assert "Jane Doe" in res.output


def test_cli_entities_tenant_scoped(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``entities --tenant custom`` returns that tenant's data only."""
    # Arrange
    _seed_mixed_entities(test_db, tenant="custom")
    _env(monkeypatch)

    # Act
    custom_res = CliRunner().invoke(
        app, ["graphrag", "entities", "--tenant", "custom", "--json"]
    )
    default_res = CliRunner().invoke(app, ["graphrag", "entities", "--json"])

    # Assert
    assert _json.loads(custom_res.stdout)["count"] == 5
    assert _json.loads(default_res.stdout)["count"] == 0


def test_cli_entities_age_absent_exit1(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGE-absent image → exit 1 with a clear message."""
    # Arrange
    import brain.cli as cli_mod

    monkeypatch.setattr(cli_mod, "age_extension_available", lambda conn: False)
    _env(monkeypatch)

    # Act
    res = CliRunner().invoke(app, ["graphrag", "entities", "--json"])

    # Assert
    assert res.exit_code == 1
    assert "Apache AGE" in res.output


# --------------------------------------------------------------------------- #
# CLI — brain graphrag stats
# --------------------------------------------------------------------------- #
def test_cli_stats_json(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stats --json`` returns the correct envelope with accurate counts."""
    # Arrange
    ids = _seed_mixed_entities(test_db)
    _insert_rel(test_db, "default", ids[0], ids[1])
    _env(monkeypatch)

    # Act
    res = CliRunner().invoke(app, ["graphrag", "stats", "--json"])

    # Assert
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.stdout)
    assert payload["tenant_id"] == "default"
    assert payload["total_entities"] == 5
    assert payload["total_relationships"] == 1
    assert payload["total_communities"] == 0
    assert isinstance(payload["counts_by_type"], dict)
    assert payload["counts_by_type"]["org"] == 2
    assert payload["counts_by_type"]["person"] == 2
    assert "top_entities" in payload
    assert payload["top_entities"][0]["name"] == "Jane Doe"
    assert "cypher" not in res.stdout.lower()


def test_cli_stats_human_table(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The human render includes 'Graph Statistics' and the top-entities table."""
    # Arrange
    _seed_mixed_entities(test_db)
    _env(monkeypatch)

    # Act
    res = CliRunner().invoke(app, ["graphrag", "stats"], env={"COLUMNS": "200"})

    # Assert
    assert res.exit_code == 0, res.output
    assert "Graph Statistics" in res.output
    assert "Entities" in res.output


def test_cli_stats_tenant_scoped(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stats --tenant`` scopes to the given tenant."""
    # Arrange
    _seed_mixed_entities(test_db, tenant="ztenant")
    _env(monkeypatch)

    # Act
    custom_res = CliRunner().invoke(
        app, ["graphrag", "stats", "--tenant", "ztenant", "--json"]
    )
    default_res = CliRunner().invoke(app, ["graphrag", "stats", "--json"])

    # Assert
    assert _json.loads(custom_res.stdout)["total_entities"] == 5
    assert _json.loads(default_res.stdout)["total_entities"] == 0


# --------------------------------------------------------------------------- #
# MCP — brain_graphrag_entities + brain_graphrag_stats
# --------------------------------------------------------------------------- #
def _mcp_state(monkeypatch: pytest.MonkeyPatch) -> mcp_server._State:
    """Install a minimal MCP state pointing at the test DB."""
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    return state


def test_mcp_entities_returns_correct_envelope(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """brain_graphrag_entities returns {tenant_id, count, entities:[…]}."""
    # Arrange
    _seed_mixed_entities(test_db)
    _mcp_state(monkeypatch)

    # Act
    payload = mcp_server.brain_graphrag_entities()

    # Assert
    assert payload["tenant_id"] == "default"
    assert payload["count"] == 5
    assert len(payload["entities"]) == 5
    sample = payload["entities"][0]
    for key in ("entity_type", "name", "canonical_key", "doc_count", "description"):
        assert key in sample, key


def test_mcp_entities_type_filter(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """brain_graphrag_entities entity_type filter returns only that type."""
    # Arrange
    _seed_mixed_entities(test_db)
    _mcp_state(monkeypatch)

    # Act
    payload = mcp_server.brain_graphrag_entities(entity_type="org")

    # Assert
    assert payload["count"] == 2
    assert all(e["entity_type"] == "org" for e in payload["entities"])


def test_mcp_entities_sort_name(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """brain_graphrag_entities sort=name returns alphabetical order."""
    # Arrange
    _seed_mixed_entities(test_db)
    _mcp_state(monkeypatch)

    # Act
    payload = mcp_server.brain_graphrag_entities(sort="name", limit=0)

    # Assert
    names = [e["name"] for e in payload["entities"]]
    assert names == sorted(names)


def test_mcp_entities_limit_zero(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """brain_graphrag_entities limit=0 returns every seeded entity.

    Wave 3 changed what ``limit=0`` MEANS: it is re-mapped to
    ``BRAIN_GRAPH_ENTITIES_MAX_LIMIT`` (500) rather than "unbounded". This
    corpus has 5 entities, so the observable result is unchanged — the
    re-mapping is pinned by
    ``tests/test_mcp_payload_ceilings.py::test_graphrag_entities_limit_zero_no_longer_means_all``.
    """
    # Arrange
    _seed_mixed_entities(test_db)
    _mcp_state(monkeypatch)

    # Act
    payload = mcp_server.brain_graphrag_entities(limit=0)

    # Assert
    assert payload["count"] == 5


def test_mcp_entities_tenant_scoped(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """brain_graphrag_entities is tenant-scoped."""
    # Arrange
    _seed_mixed_entities(test_db, tenant="mcp-tenant")
    _mcp_state(monkeypatch)

    # Act
    custom = mcp_server.brain_graphrag_entities(tenant="mcp-tenant")
    default = mcp_server.brain_graphrag_entities()

    # Assert
    assert custom["count"] == 5
    assert default["count"] == 0


def test_mcp_entities_no_cypher_in_payload(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """brain_graphrag_entities never leaks raw Cypher onto the wire."""
    # Arrange
    _seed_mixed_entities(test_db)
    _mcp_state(monkeypatch)

    # Act
    payload = mcp_server.brain_graphrag_entities()

    # Assert
    assert "cypher" not in _json.dumps(payload).lower()


def test_mcp_entities_age_absent_internal_error(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGE-absent image → INTERNAL_ERROR (consistent with other graphrag tools)."""
    # Arrange
    _mcp_state(monkeypatch)
    monkeypatch.setattr(mcp_server, "age_extension_available", lambda conn: False)

    # Act / Assert
    with pytest.raises(MCPError) as exc_info:
        mcp_server.brain_graphrag_entities()
    assert exc_info.value.error.code == INTERNAL_ERROR
    assert "Apache AGE" in exc_info.value.error.message


def test_mcp_entities_invalid_entity_type_raises_internal_error(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid entity_type string from MCP caller → INTERNAL_ERROR with validation message."""
    # Arrange — MCP callers pass raw strings; Typer StrEnum guard is absent
    _mcp_state(monkeypatch)

    # Act / Assert
    with pytest.raises(MCPError) as exc_info:
        mcp_server.brain_graphrag_entities(entity_type="banana")
    assert exc_info.value.error.code == INTERNAL_ERROR
    assert "banana" in exc_info.value.error.message


def test_mcp_stats_returns_correct_envelope(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """brain_graphrag_stats returns the expected shape with accurate counts."""
    # Arrange
    ids = _seed_mixed_entities(test_db)
    _insert_rel(test_db, "default", ids[0], ids[1])
    _mcp_state(monkeypatch)

    # Act
    payload = mcp_server.brain_graphrag_stats()

    # Assert
    assert payload["tenant_id"] == "default"
    assert payload["total_entities"] == 5
    assert payload["total_relationships"] == 1
    assert payload["total_communities"] == 0
    assert payload["counts_by_type"]["org"] == 2
    assert payload["counts_by_type"]["person"] == 2
    assert "top_entities" in payload
    assert len(payload["top_entities"]) == 5
    assert payload["top_entities"][0]["name"] == "Jane Doe"


def test_mcp_stats_tenant_scoped(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """brain_graphrag_stats is tenant-scoped."""
    # Arrange
    _seed_mixed_entities(test_db, tenant="stats-tenant")
    _mcp_state(monkeypatch)

    # Act
    custom = mcp_server.brain_graphrag_stats(tenant="stats-tenant")
    default = mcp_server.brain_graphrag_stats()

    # Assert
    assert custom["total_entities"] == 5
    assert default["total_entities"] == 0


def test_mcp_stats_no_cypher_in_payload(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """brain_graphrag_stats never leaks raw Cypher onto the wire."""
    # Arrange
    _seed_mixed_entities(test_db)
    _mcp_state(monkeypatch)

    # Act
    payload = mcp_server.brain_graphrag_stats()

    # Assert
    assert "cypher" not in _json.dumps(payload).lower()


def test_mcp_stats_age_absent_internal_error(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGE-absent image → INTERNAL_ERROR."""
    # Arrange
    _mcp_state(monkeypatch)
    monkeypatch.setattr(mcp_server, "age_extension_available", lambda conn: False)

    # Act / Assert
    with pytest.raises(MCPError) as exc_info:
        mcp_server.brain_graphrag_stats()
    assert exc_info.value.error.code == INTERNAL_ERROR


# --------------------------------------------------------------------------- #
# CLI ↔ MCP parity
# --------------------------------------------------------------------------- #
def test_parity_entities_cli_vs_mcp(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """brain_graphrag_entities JSON shape equals CLI ``entities --json`` payload."""
    # Arrange
    _seed_mixed_entities(test_db)
    _mcp_state(monkeypatch)
    _env(monkeypatch)

    # Act
    mcp_payload = mcp_server.brain_graphrag_entities()
    cli_res = CliRunner().invoke(app, ["graphrag", "entities", "--json"])
    cli_payload = _json.loads(cli_res.stdout)

    # Assert — same entities in same order, same count
    assert mcp_payload["count"] == cli_payload["count"]
    assert mcp_payload["entities"] == cli_payload["entities"]


def test_parity_stats_cli_vs_mcp(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """brain_graphrag_stats JSON shape equals CLI ``stats --json`` payload."""
    # Arrange
    _seed_mixed_entities(test_db)
    _mcp_state(monkeypatch)
    _env(monkeypatch)

    # Act
    mcp_payload = mcp_server.brain_graphrag_stats()
    cli_res = CliRunner().invoke(app, ["graphrag", "stats", "--json"])
    cli_payload = _json.loads(cli_res.stdout)

    # Assert — key stats match between MCP and CLI
    for key in (
        "total_entities",
        "total_relationships",
        "total_communities",
        "counts_by_type",
    ):
        assert mcp_payload[key] == cli_payload[key], f"mismatch on {key!r}"
    assert mcp_payload["top_entities"] == cli_payload["top_entities"]


# --------------------------------------------------------------------------- #
# Total-order regression (sibling of F-1, e2e QA 2026-08-20)
#
# ``brain_graphrag_entities`` over-fetches ``limit + 1`` and prefix-slices with
# ``cap_rows`` — the same LOW-3 shape as the link tools. That only works if the
# SQL order is TOTAL. ``name`` is not unique (graph_entities is UNIQUE on
# tenant_id + entity_type + canonical_key), so two entities can share a display
# name and, under a tie, PostgreSQL may order the bounded top-N plan differently
# from the unbounded one. The order clause therefore ends in the rest of that
# unique key. Mutation check: drop ``entity_type ASC, canonical_key ASC`` from
# ``list_entities`` and these go red.
# --------------------------------------------------------------------------- #
_TIED_ENTITIES = 60


def _seed_tied_entities(
    conn: psycopg.Connection[Any], tenant: str = "default"
) -> None:
    """Many entities sharing BOTH ``doc_count`` and ``name``."""
    for i in range(_TIED_ENTITIES):
        _insert_entity(
            conn,
            tenant,
            "person" if i % 2 else "org",
            "Same Name",  # identical on purpose
            f"same-name-{i:03d}",
            doc_count=7,  # identical on purpose
        )


@pytest.mark.parametrize("sort", ["docs", "name"])
def test_list_entities_limit_is_a_prefix_when_names_tie(
    test_db: psycopg.Connection[Any], sort: str
) -> None:
    """``list_entities(limit=n)`` is the first ``n`` rows of the unbounded list."""
    _seed_tied_entities(test_db)

    full = list_entities(test_db, "default", sort=sort, limit=0)

    assert len(full) == _TIED_ENTITIES
    assert len({r.name for r in full}) == 1, "fixture sanity — names all tie"
    assert len({r.doc_count for r in full}) == 1, "fixture sanity — counts all tie"
    for cap in (1, 2, 3, _TIED_ENTITIES // 2, _TIED_ENTITIES - 1):
        bounded = list_entities(test_db, "default", sort=sort, limit=cap)
        assert len(bounded) == cap, f"right COUNT at limit={cap} sort={sort}"
        assert bounded == full[:cap], f"right ROWS at limit={cap} sort={sort}"


def test_graph_stats_top_entities_is_the_list_entities_prefix_when_names_tie(
    test_db: psycopg.Connection[Any],
) -> None:
    """``graph_stats`` documents its top-10 as the ``list_entities`` slice."""
    _seed_tied_entities(test_db)

    stats = graph_stats(test_db, "default")
    expected = list_entities(test_db, "default", sort="docs", limit=10)

    assert list(stats.top_entities) == expected
