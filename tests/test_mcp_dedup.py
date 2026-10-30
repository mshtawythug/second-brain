"""MCP ``brain_dedup_list`` (RM-19): read-only, with the F6 lens on BOTH ends of a pair.

A pair names two documents, so excluding confidential documents means a pair is returned only
when neither end is confidential (as ``connect.iter_suggestions`` gates both endpoints). The
confidential document is put at each end in turn, so a one-ended lens fails. All data is
synthetic.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

from brain import mcp_server, near_dup, near_dup_store
from brain.config import Config
from brain.mcp_compat import MCPError
from brain.mcp_tools import dedup as tools
from tests.near_dup_fakes import body, insert_doc, near_copy, pairs

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the data fresh
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=None,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _code(excinfo: pytest.ExceptionInfo[BaseException]) -> int:
    return int(excinfo.value.error.code)  # type: ignore[attr-defined]


def _seed(
    conn: psycopg.Connection[Any], seed: int, *, first: str = "normal", second: str = "normal"
) -> tuple[str, str]:
    text = body(400, seed)
    return (
        insert_doc(conn, text, title=f"Alpha {seed}", sensitivity=first),
        insert_doc(conn, near_copy(text), title=f"Beta {seed}", sensitivity=second),
    )


def test_the_tool_is_advertised() -> None:
    names = {tool.name for tool in asyncio.run(mcp_server.mcp_app.list_tools())}

    assert "brain_dedup_list" in names


def test_lists_a_normal_pair_with_both_titles(
    mcp_state: mcp_server._State, test_db: psycopg.Connection[Any]
) -> None:
    first, second = _seed(test_db, 1)
    near_dup_store.scan(test_db)

    result = tools.brain_dedup_list()

    [pair] = result["pairs"]
    assert {pair["doc_a"]["id"], pair["doc_b"]["id"]} == {first, second}
    assert {pair["doc_a"]["title"], pair["doc_b"]["title"]} == {"Alpha 1", "Beta 1"}
    assert pair["status"] == "open"
    assert pair["similarity"] >= near_dup.JACCARD_CUT


@pytest.mark.parametrize("end", [0, 1], ids=["doc_a", "doc_b"])
def test_a_confidential_end_withholds_the_whole_pair_by_default(
    mcp_state: mcp_server._State, test_db: psycopg.Connection[Any], end: int
) -> None:
    # The confidential document is chosen by its stored position (doc_a < doc_b), not by
    # insertion order, so a lens on one end only fails on one of the two cases.
    first, second = _seed(test_db, 2)
    near_dup_store.scan(test_db)
    test_db.execute(
        "UPDATE documents SET sensitivity = 'confidential' WHERE id = %s",
        (near_dup.ordered_pair(first, second)[end],),
    )

    gated = tools.brain_dedup_list()
    opted_in = tools.brain_dedup_list(include_confidential=True)

    assert gated == {"pairs": []}
    assert len(opted_in["pairs"]) == 1


def test_the_tool_is_read_only(
    mcp_state: mcp_server._State, test_db: psycopg.Connection[Any]
) -> None:
    # Listing never scans: an unsigned corpus lists nothing and stays unsigned.
    _seed(test_db, 3)

    assert tools.brain_dedup_list() == {"pairs": []}
    assert test_db.execute("SELECT count(*) FROM document_minhash").fetchone() == (0,)
    assert pairs(test_db) == []


def test_status_filter(mcp_state: mcp_server._State, test_db: psycopg.Connection[Any]) -> None:
    first, second = _seed(test_db, 4)
    near_dup_store.scan(test_db)
    near_dup_store.set_status(test_db, first, second, "dismissed")

    assert tools.brain_dedup_list() == {"pairs": []}
    assert len(tools.brain_dedup_list(status="dismissed")["pairs"]) == 1


@pytest.mark.parametrize(
    "kwargs", [{"status": "merged"}, {"limit": 0}, {"limit": tools.MAX_LIMIT + 1}]
)
def test_bad_arguments_are_invalid_params(
    mcp_state: mcp_server._State, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(MCPError) as excinfo:
        tools.brain_dedup_list(**kwargs)

    assert _code(excinfo) == INVALID_PARAMS


@pytest.mark.fresh_schema
def test_without_migration_030_it_says_run_init(
    mcp_state: mcp_server._State, test_db: psycopg.Connection[Any]
) -> None:
    test_db.execute("DROP TABLE near_duplicates")
    test_db.execute("DROP TABLE document_minhash")

    with pytest.raises(MCPError) as excinfo:
        tools.brain_dedup_list()

    assert _code(excinfo) == INTERNAL_ERROR
    assert "brain init" in str(excinfo.value)


def test_a_database_failure_is_a_class_only_internal_error(
    mcp_state: mcp_server._State, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken(*_a: object, **_k: object) -> list[Any]:
        raise psycopg.errors.InternalError("synthetic detail that must not reach the caller")

    monkeypatch.setattr("brain.mcp_tools.dedup.list_pairs", _broken)

    with pytest.raises(MCPError) as excinfo:
        tools.brain_dedup_list()

    assert _code(excinfo) == INTERNAL_ERROR
    assert "synthetic detail" not in str(excinfo.value)
