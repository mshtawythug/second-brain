"""Tests for the ``brain_rate`` MCP tool (T3 / B3 rate-parity).

The MCP counterpart of the CLI's ``brain rate``: closes the parity gap where
the graph feedback path (entity / community / theme ratings) was reachable only
from the CLI. Mirrors ``tests/test_cli_rate.py`` — both verdicts, append
semantics, id-prefix / verdict / target-type error paths, graph-target rating,
and ``graph_retrieved`` provenance — but exercises the tool function directly
(``source='mcp'``) instead of the Typer CLI.

All data is synthetic (a single "Seed" note + synthetic graph-target ids); no
PII. Reads/writes the AGE test DB on port 5434 only — no Ollama, no prod 5433.
"""
from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from unittest import mock

import psycopg
import pytest
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

from brain import mcp_server
from brain.config import Config
from brain.ingest import ExtractedDoc, ingest_document
from brain.mcp_compat import MCPError

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — fixture keeps schema fresh
    fake_embedder: object,
) -> Iterator[mcp_server._State]:
    """Install an MCP state pointing at the test DB (no enricher needed)."""
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _seed_doc(test_db: psycopg.Connection, fake_embedder: object) -> str:
    """Seed one document and return its id."""
    result = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title="Seed",
            content="Seed body",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    assert result.document_id is not None
    return result.document_id


def _doc_rows(doc_id: str) -> list[tuple[str, str, str | None, bool]]:
    """Read document rows as (action, source, target_type, graph_retrieved)."""
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT action, source, target_type, graph_retrieved "
            "FROM interactions WHERE document_id = %s ORDER BY at",
            (doc_id,),
        ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def _target_rows(
    target_type: str, target_id: str
) -> list[tuple[str, str, str, str | None, bool]]:
    """Read graph-target rows as (action, source, target_type, document_id, gr)."""
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT action, source, target_type, document_id::text, graph_retrieved "
            "FROM interactions WHERE target_type = %s AND target_id = %s ORDER BY at",
            (target_type, target_id),
        ).fetchall()
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


def _count_interactions() -> int:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute("SELECT count(*) FROM interactions").fetchone()
    assert row is not None
    return int(row[0])


# --------------------------------------------------------------------------- #
# 1. Document rating — happy paths + return shape
# --------------------------------------------------------------------------- #
def test_rate_document_useful_records_row(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    doc_id = _seed_doc(test_db, fake_embedder)
    payload = mcp_server.brain_rate(id=doc_id, verdict="useful")
    assert payload["action"] == "rated_useful"
    assert payload["document_id"] == doc_id
    assert payload["graph_retrieved"] is False
    assert payload["interaction_id"]
    rows = _doc_rows(doc_id)
    assert len(rows) == 1
    assert rows[0] == ("rated_useful", "mcp", None, False)


def test_rate_document_irrelevant_records_row(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    doc_id = _seed_doc(test_db, fake_embedder)
    payload = mcp_server.brain_rate(id=doc_id, verdict="irrelevant")
    assert payload["action"] == "rated_irrelevant"
    rows = _doc_rows(doc_id)
    assert len(rows) == 1
    assert rows[0][0] == "rated_irrelevant"


def test_rate_document_appends_each_call(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Ratings APPEND — re-rating the same doc adds a new row."""
    doc_id = _seed_doc(test_db, fake_embedder)
    mcp_server.brain_rate(id=doc_id, verdict="useful")
    time.sleep(0.001)  # guarantee a distinct ``at`` for the ORDER BY
    mcp_server.brain_rate(id=doc_id, verdict="useful")
    rows = _doc_rows(doc_id)
    assert len(rows) == 2
    assert {r[0] for r in rows} == {"rated_useful"}


def test_rate_document_with_graph_retrieved(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A document rating can carry graph_retrieved provenance (XOR-valid)."""
    doc_id = _seed_doc(test_db, fake_embedder)
    payload = mcp_server.brain_rate(
        id=doc_id, verdict="useful", graph_retrieved=True
    )
    assert payload["graph_retrieved"] is True
    rows = _doc_rows(doc_id)
    assert rows[0] == ("rated_useful", "mcp", None, True)


# --------------------------------------------------------------------------- #
# 2. Verdict + id-prefix error mapping → INVALID_PARAMS
# --------------------------------------------------------------------------- #
def test_rate_invalid_verdict_invalid_params(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    doc_id = _seed_doc(test_db, fake_embedder)
    with pytest.raises(MCPError) as exc_info:
        mcp_server.brain_rate(id=doc_id, verdict="bogus")
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "useful" in exc_info.value.error.message
    assert _count_interactions() == 0


def test_rate_unknown_id_invalid_params(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    _seed_doc(test_db, fake_embedder)
    with pytest.raises(MCPError) as exc_info:
        mcp_server.brain_rate(id="deadbeef", verdict="useful")
    assert exc_info.value.error.code == INVALID_PARAMS
    assert _count_interactions() == 0


def test_rate_short_id_prefix_invalid_params(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A <6-char prefix is rejected by _resolve_id before any INSERT."""
    _seed_doc(test_db, fake_embedder)
    with pytest.raises(MCPError) as exc_info:
        mcp_server.brain_rate(id="abc", verdict="useful")
    assert exc_info.value.error.code == INVALID_PARAMS
    assert _count_interactions() == 0


# --------------------------------------------------------------------------- #
# 3. Graph-target rating (entity / community / theme) — the closed gap
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("target_type", ["entity", "community", "theme"])
def test_rate_graph_target_records_target_row(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
    target_type: str,
) -> None:
    """``target_type`` rates a graph target: target_* set, document_id NULL."""
    _seed_doc(test_db, fake_embedder)  # an unrelated doc must not be touched
    target_id = f"{target_type}-{uuid.uuid4()}"
    payload = mcp_server.brain_rate(
        id=target_id, verdict="useful", target_type=target_type
    )
    assert payload["action"] == "rated_useful"
    assert payload["target_type"] == target_type
    assert payload["target_id"] == target_id
    assert payload["graph_retrieved"] is False
    assert "document_id" not in payload
    rows = _target_rows(target_type, target_id)
    assert len(rows) == 1
    assert rows[0] == ("rated_useful", "mcp", target_type, None, False)


def test_rate_graph_target_irrelevant(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    _seed_doc(test_db, fake_embedder)
    target_id = f"entity-{uuid.uuid4()}"
    mcp_server.brain_rate(
        id=target_id, verdict="irrelevant", target_type="entity"
    )
    rows = _target_rows("entity", target_id)
    assert len(rows) == 1
    assert rows[0][0] == "rated_irrelevant"


def test_rate_graph_target_with_graph_retrieved(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """``graph_retrieved`` stamps provenance on a graph-target row."""
    _seed_doc(test_db, fake_embedder)
    target_id = f"community-{uuid.uuid4()}"
    payload = mcp_server.brain_rate(
        id=target_id,
        verdict="useful",
        target_type="community",
        graph_retrieved=True,
    )
    assert payload["graph_retrieved"] is True
    rows = _target_rows("community", target_id)
    assert len(rows) == 1
    assert rows[0][4] is True


def test_rate_invalid_target_type_invalid_params(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A bogus target_type is rejected at the boundary; nothing written."""
    _seed_doc(test_db, fake_embedder)
    with pytest.raises(MCPError) as exc_info:
        mcp_server.brain_rate(id="anything", verdict="useful", target_type="bogus")
    assert exc_info.value.error.code == INVALID_PARAMS
    msg = exc_info.value.error.message
    assert "entity" in msg and "community" in msg and "theme" in msg
    assert _count_interactions() == 0


# --------------------------------------------------------------------------- #
# 4. DB failure → INTERNAL_ERROR (recording IS the tool's job — not swallowed)
# --------------------------------------------------------------------------- #
def test_rate_db_failure_internal_error(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A persistence failure surfaces as INTERNAL_ERROR (unlike brain_show's
    best-effort open logging — recording the rating is this tool's purpose)."""
    doc_id = _seed_doc(test_db, fake_embedder)
    boom = psycopg.OperationalError("simulated logging outage")
    with (
        mock.patch("brain.mcp_server.record_interaction", side_effect=boom),
        pytest.raises(MCPError) as exc_info,
    ):
        mcp_server.brain_rate(id=doc_id, verdict="useful")
    assert exc_info.value.error.code == INTERNAL_ERROR
    assert _doc_rows(doc_id) == []
