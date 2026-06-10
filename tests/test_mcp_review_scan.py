"""MCP tests for ``brain_review_scan`` + ``brain_review_findings_list`` (Plan 03).

Installs a fresh ``_State`` pointed at the test DB and drives the tools
directly. The conflict path uses a fake enricher (no Ollama); the staleness
path needs no LLM. Seeding helpers are shared with :mod:`tests.test_review`.
"""
from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from mcp import McpError

from brain import mcp_server
from brain.config import Config
from brain.review import queries

from .conftest import TEST_DATABASE_URL
from .test_review import (
    _FakeEnricher,
    _insert_doc,
    _insert_entity,
    _seed_conflict_entity,
)

_TENANT = "default"


def _install_state(
    monkeypatch: pytest.MonkeyPatch,
    embedder: object,
    tmp_path: Path,
    *,
    enricher: object | None = None,
    contradiction_enabled: bool = False,
) -> mcp_server._State:
    state = mcp_server._State(
        cfg=Config(
            database_url=TEST_DATABASE_URL,
            vault_path=tmp_path,
            elicit_contradiction_enabled=contradiction_enabled,
            elicit_contradiction_min_docs=2,
        ),
        embedder=embedder,  # type: ignore[arg-type]
        enricher=enricher,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    return state


@pytest.fixture
def stale_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps schema fresh
    fake_embedder: object,
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    yield _install_state(monkeypatch, fake_embedder, tmp_path)


def _seed_stale_pair(conn: psycopg.Connection) -> str:
    old = _insert_doc(
        conn,
        title="Compensation ranges — synthetic role",
        summary="old comp",
        ingested_days_ago=400,
        embedding=[1.0, 0.0],
    )
    new = _insert_doc(
        conn,
        title="Updated salary bands — synthetic",
        summary="new comp",
        ingested_days_ago=15,
        embedding=[0.7, math.sqrt(0.51)],
    )
    _insert_entity(conn, canonical_key="comp", name="Compensation", doc_ids=[old, new])
    return old


def test_brain_review_scan_stale(
    stale_state: mcp_server._State,  # noqa: ARG001 — installs state
    test_db: psycopg.Connection,
) -> None:
    old = _seed_stale_pair(test_db)
    payload = mcp_server.brain_review_scan(scan_type="stale")
    assert payload["llm_calls"] == 0
    assert payload["scanned"] >= 1
    kinds = {f["kind"] for f in payload["findings"]}
    assert kinds == {"stale"}
    assert payload["findings"][0]["target_id"] == old


def test_brain_review_scan_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    _seed_conflict_entity(test_db)
    _install_state(
        monkeypatch,
        fake_embedder,
        tmp_path,
        enricher=_FakeEnricher(),
        contradiction_enabled=True,
    )
    payload = mcp_server.brain_review_scan(scan_type="conflicts")
    assert payload["scanned"] >= 1
    assert payload["llm_calls"] >= 1
    assert len(payload["findings"]) == 1
    assert payload["findings"][0]["target_id"] == "synthetic-initiative"


def test_brain_review_scan_conflicts_skipped_when_disabled(
    stale_state: mcp_server._State,  # noqa: ARG001 — installs state, enricher None
    test_db: psycopg.Connection,
) -> None:
    _seed_conflict_entity(test_db)
    payload = mcp_server.brain_review_scan(scan_type="conflicts")
    # Disabled + no enricher -> no LLM, no findings, but candidates still scanned.
    assert payload["llm_calls"] == 0
    assert payload["findings"] == []
    assert payload["scanned"] >= 1


def test_brain_review_scan_invalid_type(
    stale_state: mcp_server._State,  # noqa: ARG001 — installs state
) -> None:
    with pytest.raises(McpError):
        mcp_server.brain_review_scan(scan_type="bogus")


def test_brain_review_scan_invalid_limit(
    stale_state: mcp_server._State,  # noqa: ARG001 — installs state
) -> None:
    with pytest.raises(McpError):
        mcp_server.brain_review_scan(limit=0)


def test_brain_review_findings_list(
    stale_state: mcp_server._State,  # noqa: ARG001 — installs state
    test_db: psycopg.Connection,
) -> None:
    queries.upsert_review_finding(
        test_db,
        tenant_id=_TENANT,
        signal_kind="stale",
        target_type="doc",
        target_id="doc-l",
        score=0.8,
        evidence_ids=["doc-l", "doc-m"],
        rationale="aged",
    )
    payload = mcp_server.brain_review_findings_list(kind="stale")
    assert len(payload["findings"]) == 1
    finding = payload["findings"][0]
    assert finding["kind"] == "stale"
    assert finding["status"] == "surfaced"
    assert finding["target_id"] == "doc-l"
    assert "id" in finding


def test_brain_review_findings_list_invalid_kind(
    stale_state: mcp_server._State,  # noqa: ARG001 — installs state
) -> None:
    with pytest.raises(McpError):
        mcp_server.brain_review_findings_list(kind="bogus")
