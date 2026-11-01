"""Without --include-confidential, HTTP serves no graph tool and brain_ask runs hybrid only.

RM-46 round 5. The entity graph is derived from every document: entity names,
co-occurrence edges and their weights, themes and communities all come from
confidential documents too. Four security rounds each found one more graph output
that carried that, so the graph family is now withheld in that mode instead of
filtered field by field (``brain.mcp_tool_classes``). With --include-confidential
it is served as before; stdio never withholds anything.

The serve-level tests (tools/list, "Unknown tool", the banner) are in
``tests/test_mcp_http_app.py``. Here: the real registry's graph family, and
``brain_ask``'s modes. Every value is synthetic.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import psycopg
import pytest

from brain import ask as ask_mod
from brain import mcp_server
from brain.ask import ASK_MODES, HYBRID_MODE
from brain.config import Config
from brain.mcp_compat import MCPError
from brain.mcp_tool_classes import (
    ASK_GRAPH_MODES_REFUSAL,
    GRAPH_TOOL_PREFIX,
    READ_ONLY_TOOLS,
    hide_mode_withheld,
    registered_tool_names,
)

from .conftest import TEST_DATABASE_URL

#: Every graph tool at this commit. A new one is withheld by its prefix anyway;
#: this pins the family so a renamed tool cannot slip out of it unnoticed.
GRAPH_TOOLS = (
    "brain_graphrag_aliases_apply",
    "brain_graphrag_build",
    "brain_graphrag_communities",
    "brain_graphrag_communities_build",
    "brain_graphrag_communities_refresh",
    "brain_graphrag_entities",
    "brain_graphrag_entity",
    "brain_graphrag_refresh",
    "brain_graphrag_remove",
    "brain_graphrag_search",
    "brain_graphrag_stats",
    "brain_graphrag_themes",
)
GRAPH_MODES = sorted(set(ASK_MODES) - {HYBRID_MODE})


@pytest.mark.nodb
def test_the_registry_graph_family_is_the_pinned_one() -> None:
    names = anyio.run(registered_tool_names, mcp_server.mcp_app)
    graph = tuple(name for name in names if name.startswith(GRAPH_TOOL_PREFIX))

    assert graph == GRAPH_TOOLS
    # sec-118e: plus the pinned tools built from graph communities or edges.
    assert hide_mode_withheld(names) == tuple(sorted((*GRAPH_TOOLS, "brain_review_weekly")))
    assert len(names) == 51
    # Read-only and confidential-excluded (the default) serves 28 - 6 = 22 tools.
    assert len(READ_ONLY_TOOLS - set(GRAPH_TOOLS)) == 22
    # brain_timeline and brain_explain stay: their output is built from gated
    # documents only (plan, "round 5").
    assert {"brain_timeline", "brain_explain"} <= READ_ONLY_TOOLS - set(GRAPH_TOOLS)


@pytest.fixture
def make_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    tmp_path: Path,
) -> Callable[..., mcp_server._State]:
    def make(*, hide: bool) -> mcp_server._State:
        state = mcp_server._State(
            cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
            embedder=fake_embedder,  # type: ignore[arg-type]
            hide_confidential=hide,
        )
        monkeypatch.setattr(mcp_server, "_state", state)
        return state

    return make


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The mode of every call that reached :func:`brain.ask.ask`."""
    seen: list[str] = []

    def fake_ask(*_: Any, mode: str, **__: Any) -> ask_mod.AskResult:
        seen.append(mode)
        return ask_mod.AskResult(
            answer="synthetic", citations=[], iterations_used=1, sub_queries=["q"],
            fallback_used=False, session_id="s",
        )

    monkeypatch.setattr(ask_mod, "ask", fake_ask)
    return seen


@pytest.mark.parametrize("mode", GRAPH_MODES)
def test_hidden_mode_refuses_every_graph_ask_mode(
    make_state: Callable[..., mcp_server._State], asked: list[str], mode: str
) -> None:
    make_state(hide=True)

    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_ask(question="q", mode=mode)

    assert ASK_GRAPH_MODES_REFUSAL in str(excinfo.value)
    assert asked == []


def test_hidden_mode_still_answers_in_hybrid(
    make_state: Callable[..., mcp_server._State], asked: list[str]
) -> None:
    make_state(hide=True)

    payload = mcp_server.brain_ask(question="q")

    assert payload["answer"] == "synthetic"
    assert asked == [HYBRID_MODE]


@pytest.mark.parametrize("mode", GRAPH_MODES)
def test_graph_ask_modes_run_when_confidential_is_served(
    make_state: Callable[..., mcp_server._State], asked: list[str], mode: str
) -> None:
    make_state(hide=False)

    mcp_server.brain_ask(question="q", mode=mode)

    assert asked == [mode]
