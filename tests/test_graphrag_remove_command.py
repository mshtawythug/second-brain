"""Tests for ``brain graphrag remove`` and its MCP twin ``brain_graphrag_remove``.

The targeted repair for a graph remove that never ran. When the vault
watcher's shutdown times out behind a long reconcile (PR #54), the removes
still queued are logged by document id and not applied. The ``documents`` row
is gone and its relational graph rows went with it (``ON DELETE CASCADE``), but
the AGE ``Document`` vertex and its ``MENTIONED_IN`` edges stay. Before this
command, the only repair was ``brain graphrag build --force``, which re-extracts
every document's concepts.

Against the live AGE test instance (port 5434), with the real ``AgeBackend``.
Each AGE count goes through independent raw Cypher, not the backend under test.
All people are synthetic (alpha / bravo); no PII.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping
from typing import Any

import psycopg
import pytest
from mcp.types import INVALID_PARAMS
from typer.testing import CliRunner

from brain import mcp_server
from brain.cli import app
from brain.config import Config
from brain.db import DEFAULT_GRAPH_NAME, load_age
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.reconcile import ReconcileConfig, ResolvedPerson, reconcile_document
from brain.mcp_compat import MCPError
from tests.conftest import TEST_DATABASE_URL, FakeEmbedder
from tests.graph_fakes import seed_note_doc, static_resolver

TENANT = "default"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _cypher_count(conn: psycopg.Connection[Any], query: str, params: Mapping[str, Any]) -> int:
    load_age(conn)
    conn.execute('SET search_path = ag_catalog, "$user", public')
    try:
        row = conn.execute(
            f"SELECT * FROM ag_catalog.cypher('{DEFAULT_GRAPH_NAME}', "
            f"$$ {query} $$, %s::ag_catalog.agtype) AS (v ag_catalog.agtype)",
            (json.dumps(params),),
        ).fetchone()
    finally:
        conn.execute("RESET search_path")
    assert row is not None
    return int(str(row[0]))


def _document_vertices(conn: psycopg.Connection[Any], document_id: str) -> int:
    return _cypher_count(
        conn,
        "MATCH (d:Document {tenant_id: $t, document_uuid: $d}) RETURN count(d)",
        {"t": TENANT, "d": document_id},
    )


def _mentioned_in_edges(conn: psycopg.Connection[Any]) -> int:
    return _cypher_count(
        conn, "MATCH ()-[r:MENTIONED_IN {tenant_id: $t}]->() RETURN count(r)", {"t": TENANT}
    )


def _entity_vertices(conn: psycopg.Connection[Any]) -> int:
    return _cypher_count(conn, "MATCH (e:Entity {tenant_id: $t}) RETURN count(e)", {"t": TENANT})


def _ghost(conn: psycopg.Connection[Any]) -> str:
    """A document indexed into AGE, then deleted with its graph remove lost."""
    backend = AgeBackend()
    backend.bootstrap(conn)
    doc = seed_note_doc(conn, "ghost-doc")
    reconcile_document(
        conn,
        doc,
        backend=backend,
        config=ReconcileConfig(tenant_id=TENANT, generic_df_ratio=1.0),
        person_resolver=static_resolver(
            [ResolvedPerson("alpha", "Alpha"), ResolvedPerson("bravo", "Bravo")]
        ),
    )
    # The lost remove: the row (and, by cascade, its relational graph rows) go,
    # and nothing tells AGE.
    conn.execute("DELETE FROM documents WHERE id = %s", (doc,))
    assert _document_vertices(conn, doc) == 1, "setup: the ghost vertex must remain"
    assert _mentioned_in_edges(conn) == 2, "setup: the ghost's MENTIONED_IN edges remain"
    return doc


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 - keeps schema + graph fresh
) -> Iterator[None]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, graph_generic_df_ratio=1.0),
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
        enricher=None,
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_remove_clears_a_ghost_document_and_a_second_run_is_a_no_op(
    test_db: psycopg.Connection[Any],
) -> None:
    doc = _ghost(test_db)

    first = CliRunner().invoke(app, ["graphrag", "remove", doc])
    second = CliRunner().invoke(app, ["graphrag", "remove", doc])

    assert first.exit_code == 0, first.output
    assert f"{doc}: removed 1 document vertex" in first.output
    assert _document_vertices(test_db, doc) == 0
    assert _mentioned_in_edges(test_db) == 0
    assert _entity_vertices(test_db) == 0, "the now-orphaned people must go too"
    assert second.exit_code == 0, second.output
    assert f"{doc}: no graph state, nothing to remove" in second.output
    assert "0 of 1 id(s) had graph state" in second.output


def test_remove_takes_several_ids_and_reports_each(test_db: psycopg.Connection[Any]) -> None:
    doc = _ghost(test_db)
    never_indexed = str(uuid.uuid4())

    result = CliRunner().invoke(app, ["graphrag", "remove", doc, never_indexed])

    assert result.exit_code == 0, result.output
    assert f"{doc}: removed 1 document vertex" in result.output
    assert f"{never_indexed}: no graph state, nothing to remove" in result.output
    assert "1 of 2 id(s) had graph state" in result.output


def test_remove_needs_full_uuids(test_db: psycopg.Connection[Any]) -> None:
    """A deleted document has no row, so a prefix has nothing to resolve against."""
    doc = _ghost(test_db)

    result = CliRunner().invoke(app, ["graphrag", "remove", doc, doc[:8]])

    assert result.exit_code == 2, result.output
    assert "full document UUID" in result.output
    assert _document_vertices(test_db, doc) == 1, "a bad id must stop the whole run"


def test_remove_refuses_a_document_that_still_exists(test_db: psycopg.Connection[Any]) -> None:
    """The repair is for deleted documents; a live one keeps its graph."""
    backend = AgeBackend()
    backend.bootstrap(test_db)
    live = seed_note_doc(test_db, "live-doc")
    reconcile_document(
        test_db,
        live,
        backend=backend,
        config=ReconcileConfig(tenant_id=TENANT, generic_df_ratio=1.0),
        person_resolver=static_resolver([ResolvedPerson("alpha", "Alpha")]),
    )

    result = CliRunner().invoke(app, ["graphrag", "remove", live])

    assert result.exit_code == 1, result.output
    assert "still exists" in result.output
    assert _document_vertices(test_db, live) == 1


def test_remove_prints_counts_only(test_db: psycopg.Connection[Any]) -> None:
    """No title, name or body text reaches the output: only ids and counts."""
    doc = _ghost(test_db)

    result = CliRunner().invoke(app, ["graphrag", "remove", doc])

    assert result.exit_code == 0, result.output
    for text in ("ghost-doc", "Alpha", "Bravo", "alpha", "bravo"):
        assert text not in result.output


# --------------------------------------------------------------------------- #
# MCP twin
# --------------------------------------------------------------------------- #
def test_mcp_twin_removes_a_ghost_and_is_idempotent(
    test_db: psycopg.Connection[Any], mcp_state: None
) -> None:
    doc = _ghost(test_db)

    first = mcp_server.brain_graphrag_remove(document_ids=[doc])
    second = mcp_server.brain_graphrag_remove(document_ids=[doc])

    assert first["tenant_id"] == TENANT
    assert first["with_graph_state"] == 1
    assert first["removed"][0]["document_id"] == doc
    assert first["removed"][0]["document_vertices_removed"] == 1
    assert _document_vertices(test_db, doc) == 0
    assert second["with_graph_state"] == 0
    assert second["removed"][0]["had_graph_state"] is False


def test_mcp_twin_rejects_a_prefix_and_a_live_document(
    test_db: psycopg.Connection[Any], mcp_state: None
) -> None:
    live = seed_note_doc(test_db, "live-doc")

    with pytest.raises(MCPError) as prefix:
        mcp_server.brain_graphrag_remove(document_ids=[live[:8]])
    with pytest.raises(MCPError) as still_exists:
        mcp_server.brain_graphrag_remove(document_ids=[live])

    assert prefix.value.error.code == INVALID_PARAMS
    assert "full document UUID" in prefix.value.error.message
    assert still_exists.value.error.code == INVALID_PARAMS
    assert "still exists" in still_exists.value.error.message
