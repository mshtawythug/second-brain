"""MCP saved-search tools (RM-15): list, add, peek-check, remove, and ``brain_brief``'s section.

Pinned here: ``brain_watch_check`` never records (AC10, MCP half) and works lexically with the
embedder down; every tool that names a document applies the F6 lens; no tool returns a count over
both tiers; ``brain_watch_remove`` needs ``confirm``; and MCP ``brain_brief`` reads the terminal
hit set through its lens without running a check. All data is synthetic.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

from brain import mcp_server
from brain.config import Config
from brain.mcp_compat import MCPError
from brain.mcp_tools import saved_search as tools
from tests.saved_search_fakes import DownEmbedder, WordEmbedder, age_watch, ingest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def words() -> WordEmbedder:
    return WordEmbedder()


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the data fresh
    words: WordEmbedder,
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path, vector_sim_floor=0.25),
        embedder=words,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _code(excinfo: pytest.ExceptionInfo[BaseException]) -> int:
    return int(excinfo.value.error.code)  # type: ignore[attr-defined]


def _hit_count(conn: psycopg.Connection[Any]) -> int:
    return int(conn.execute("SELECT count(*) FROM saved_search_hits").fetchone()[0])


def test_the_four_tools_are_advertised() -> None:
    names = {tool.name for tool in asyncio.run(mcp_server.mcp_app.list_tools())}

    assert {
        "brain_watch_list",
        "brain_watch_add",
        "brain_watch_check",
        "brain_watch_remove",
    } <= names


def test_add_then_list_round_trips_without_any_count(
    mcp_state: mcp_server._State, test_db: psycopg.Connection[Any], words: WordEmbedder
) -> None:
    ingest(test_db, words, title="Zephyr", content="zephyr", source_kind="krisp")

    added = tools.brain_watch_add("zephyr", "zephyr", source="krisp", without_tag="Road Map")
    listed = tools.brain_watch_list()

    assert set(added) == {"watch"}
    assert added["watch"]["filters"] == {"source": "krisp", "without_tag": "road-map"}
    assert added["watch"]["mode"] == "lexical"
    assert listed == {"watches": [added["watch"]]}
    assert _hit_count(test_db) == 1  # seeded, but the seed count is not returned


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": "fax"},
        {"person": "Nobody Synthetic"},
        {"name": "  "},
    ],
)
def test_add_refuses_bad_input_as_invalid_params_and_saves_nothing(
    mcp_state: mcp_server._State, kwargs: dict[str, Any]
) -> None:
    args = {"name": "w", "query": "q", **kwargs}

    with pytest.raises(MCPError) as excinfo:
        tools.brain_watch_add(**args)

    assert _code(excinfo) == INVALID_PARAMS
    assert tools.brain_watch_list() == {"watches": []}


def test_add_refuses_a_taken_name(mcp_state: mcp_server._State) -> None:
    tools.brain_watch_add("w", "q")

    with pytest.raises(MCPError) as excinfo:
        tools.brain_watch_add("w", "other")

    assert _code(excinfo) == INVALID_PARAMS


def test_a_semantic_add_with_the_embedder_down_is_refused(
    mcp_state: mcp_server._State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_state, "embedder", DownEmbedder())

    with pytest.raises(MCPError) as excinfo:
        tools.brain_watch_add("orbit", "orbital", semantic=True)

    assert _code(excinfo) == INVALID_PARAMS
    assert "embedder unavailable" in str(excinfo.value)
    assert tools.brain_watch_list() == {"watches": []}


def test_check_peeks_and_never_records_even_with_the_embedder_down(
    mcp_state: mcp_server._State,
    test_db: psycopg.Connection[Any],
    words: WordEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools.brain_watch_add("zephyr", "zephyr")
    age_watch(test_db, "zephyr", hours=3)
    before = test_db.execute("SELECT last_checked_at FROM saved_searches").fetchone()[0]
    doc = ingest(test_db, words, title="Zephyr launch", content="zephyr launch")
    monkeypatch.setattr(mcp_state, "embedder", DownEmbedder())

    first = tools.brain_watch_check()
    second = tools.brain_watch_check("zephyr")

    expected = {
        "results": [
            {
                "watch": "zephyr",
                "mode": "lexical",
                "skipped": None,
                "new": [{"id": doc, "title": "Zephyr launch", "source": "manual"}],
            }
        ]
    }
    assert first == expected and second == expected
    assert _hit_count(test_db) == 0
    after = test_db.execute("SELECT last_checked_at FROM saved_searches").fetchone()[0]
    assert after == before


def test_check_applies_the_lens(
    mcp_state: mcp_server._State, test_db: psycopg.Connection[Any], words: WordEmbedder
) -> None:
    tools.brain_watch_add("zephyr", "zephyr")
    ingest(test_db, words, title="Zephyr open", content="zephyr open")
    ingest(
        test_db, words, title="Zephyr sealed", content="zephyr sealed", sensitivity="confidential"
    )

    gated = tools.brain_watch_check("zephyr")
    permissive = tools.brain_watch_check("zephyr", include_confidential=True)

    assert [n["title"] for n in gated["results"][0]["new"]] == ["Zephyr open"]
    assert sorted(n["title"] for n in permissive["results"][0]["new"]) == [
        "Zephyr open",
        "Zephyr sealed",
    ]


def test_check_of_an_unknown_watch_is_invalid_params(mcp_state: mcp_server._State) -> None:
    with pytest.raises(MCPError) as excinfo:
        tools.brain_watch_check("nope")

    assert _code(excinfo) == INVALID_PARAMS


def test_remove_needs_confirm(mcp_state: mcp_server._State) -> None:
    tools.brain_watch_add("w", "q")

    with pytest.raises(MCPError) as refused:
        tools.brain_watch_remove("w")
    still_there = tools.brain_watch_list()
    removed = tools.brain_watch_remove("w", confirm=True)

    assert _code(refused) == INVALID_PARAMS
    assert "confirm=true" in str(refused.value) and "Nothing was changed" in str(refused.value)
    assert [w["name"] for w in still_there["watches"]] == ["w"]
    assert removed == {"removed": "w"}
    assert tools.brain_watch_list() == {"watches": []}


def test_remove_of_an_unknown_watch_is_invalid_params(mcp_state: mcp_server._State) -> None:
    with pytest.raises(MCPError) as excinfo:
        tools.brain_watch_remove("nope", confirm=True)

    assert _code(excinfo) == INVALID_PARAMS


def test_brain_brief_reads_the_terminal_set_through_its_lens_and_runs_no_check(
    mcp_state: mcp_server._State, test_db: psycopg.Connection[Any], words: WordEmbedder
) -> None:
    tools.brain_watch_add("zephyr", "zephyr")
    watch_id = test_db.execute("SELECT id FROM saved_searches").fetchone()[0]
    recorded = ingest(test_db, words, title="Zephyr open", content="zephyr open")
    sealed = ingest(
        test_db, words, title="Zephyr sealed", content="zephyr sealed", sensitivity="confidential"
    )
    for doc in (recorded, sealed):
        test_db.execute(
            "INSERT INTO saved_search_hits (saved_search_id, document_id) VALUES (%s, %s)",
            (watch_id, doc),
        )
    ingest(test_db, words, title="Zephyr unchecked", content="zephyr unchecked")

    gated = mcp_server.brain_brief(no_enrich=True)
    permissive = mcp_server.brain_brief(no_enrich=True, include_confidential=True)

    assert [h["title"] for h in gated["watch_hits"]] == ["Zephyr open"]
    assert sorted(h["title"] for h in permissive["watch_hits"]) == [
        "Zephyr open",
        "Zephyr sealed",
    ]
    assert _hit_count(test_db) == 2  # "Zephyr unchecked" was never recorded


@pytest.mark.fresh_schema
def test_without_migration_029_the_tools_say_run_init(
    mcp_state: mcp_server._State, test_db: psycopg.Connection[Any]
) -> None:
    test_db.execute("DROP TABLE saved_search_hits")
    test_db.execute("DROP TABLE saved_searches")

    with pytest.raises(MCPError) as excinfo:
        tools.brain_watch_list()

    assert _code(excinfo) == INTERNAL_ERROR
    assert "brain init" in str(excinfo.value)


def test_a_database_failure_is_internal_error_naming_only_its_class(
    mcp_state: mcp_server._State, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_detail = "synthetic-detail-7c1e"

    @contextmanager
    def failing_connect(url: str) -> Iterator[psycopg.Connection[Any]]:
        raise psycopg.OperationalError(secret_detail)
        yield  # pragma: no cover - a generator, never reached

    monkeypatch.setattr(mcp_server, "connect", failing_connect)

    with pytest.raises(MCPError) as excinfo:
        tools.brain_watch_list()

    assert _code(excinfo) == INTERNAL_ERROR
    assert "OperationalError" in str(excinfo.value)
    assert secret_detail not in str(excinfo.value)
