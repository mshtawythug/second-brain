"""Loop and depth arguments are bounded over HTTP, and brain_ask stops at its deadline.

RM-46, sec-118c LOW-2. Since be064a05 a call that passes ``--call-timeout`` gets its
504 but keeps its work-limit slot until the tool returns. ``brain_ask`` took any
``max_iterations`` and ``limit``, so a few calls could hold every slot for hours.

Over HTTP (``McpState.call_timeout_s`` set by ``brain mcp serve``):

- ``brain_ask`` refuses ``max_iterations`` above ``ask_max_iterations`` and
  ``limit`` above ``ask_docs_per_iter``, the values the UI's Agent tab runs
  with, through the same ``INVALID_PARAMS`` ceiling every other MCP limit uses;
- ``brain_ask`` stops at its next step once ``call_timeout_s`` has passed, so the
  work ends, not just the wait;
- the graph tools refuse ``depth`` above ``graph_depth``.

stdio (``call_timeout_s`` unset) is unchanged. Every document is synthetic.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import ask as ask_mod
from brain import chat, mcp_server
from brain.ask_deadline import AskTimedOut, Deadline, deadline_on_step
from brain.config import Config
from brain.mcp_compat import MCPError

from .conftest import TEST_DATABASE_URL

CALL_TIMEOUT_S = 30.0


@pytest.fixture
def make_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    tmp_path: Path,
) -> Iterator[Callable[..., mcp_server._State]]:
    def make(*, call_timeout_s: float | None) -> mcp_server._State:
        state = mcp_server._State(
            cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
            embedder=fake_embedder,  # type: ignore[arg-type]
            call_timeout_s=call_timeout_s,
        )
        monkeypatch.setattr(mcp_server, "_state", state)
        return state

    yield make


@pytest.fixture
def ask_kwargs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What ``brain_ask`` passes to :func:`brain.ask.ask`, one dict per call."""
    seen: list[dict[str, Any]] = []

    def fake_ask(*_: Any, **kwargs: Any) -> ask_mod.AskResult:
        seen.append(kwargs)
        return ask_mod.AskResult(
            answer="synthetic", citations=[], iterations_used=1, sub_queries=["q"],
            fallback_used=False, session_id="s",
        )

    monkeypatch.setattr(ask_mod, "ask", fake_ask)
    return seen


# ------------------------------------------------------------- brain_ask's ceilings


@pytest.mark.parametrize(
    ("argument", "config_field"),
    [("max_iterations", "ask_max_iterations"), ("limit", "ask_docs_per_iter")],
)
def test_http_brain_ask_refuses_a_value_above_its_configured_ceiling(
    make_state: Callable[..., mcp_server._State],
    ask_kwargs: list[dict[str, Any]],
    argument: str,
    config_field: str,
) -> None:
    state = make_state(call_timeout_s=CALL_TIMEOUT_S)
    ceiling = getattr(state.cfg, config_field)

    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_ask(question="q", **{argument: ceiling + 1})

    assert f"{argument}={ceiling + 1} exceeds the configured ceiling of {ceiling}" in str(
        excinfo.value
    )
    assert ask_kwargs == []


def test_http_brain_ask_runs_ordinary_values_unchanged(
    make_state: Callable[..., mcp_server._State], ask_kwargs: list[dict[str, Any]]
) -> None:
    state = make_state(call_timeout_s=CALL_TIMEOUT_S)

    mcp_server.brain_ask(question="q")
    mcp_server.brain_ask(question="q", max_iterations=2, limit=1)
    mcp_server.brain_ask(
        question="q",
        max_iterations=state.cfg.ask_max_iterations,
        limit=state.cfg.ask_docs_per_iter,
    )

    assert [(kw["max_iterations"], kw["limit"]) for kw in ask_kwargs] == [
        (state.cfg.ask_max_iterations, state.cfg.ask_docs_per_iter),
        (2, 1),
        (state.cfg.ask_max_iterations, state.cfg.ask_docs_per_iter),
    ]
    assert all(kw["on_step"] is not None for kw in ask_kwargs)


def test_stdio_brain_ask_keeps_any_value_and_has_no_deadline(
    make_state: Callable[..., mcp_server._State], ask_kwargs: list[dict[str, Any]]
) -> None:
    make_state(call_timeout_s=None)

    mcp_server.brain_ask(question="q", max_iterations=1000, limit=500)

    assert (ask_kwargs[0]["max_iterations"], ask_kwargs[0]["limit"]) == (1000, 500)
    assert ask_kwargs[0]["on_step"] is None


# ---------------------------------------------------------- brain_ask's deadline


def _counting_chat(calls: list[str], *, plan_sleep_s: float) -> Callable[..., dict[str, Any]]:
    """Plans slowly, then never finds coverage sufficient, so the loop wants to go on."""

    def _chat(prompt: str, *, schema: dict[str, Any], **_: Any) -> dict[str, Any]:
        if "sub_queries" in schema:
            calls.append("plan")
            time.sleep(plan_sleep_s)
            return {"sub_queries": ["synthetic"]}
        if "sufficient" in schema:
            calls.append("reflect")
            return {"sufficient": False, "follow_up_queries": [f"more {len(calls)}"]}
        calls.append("synthesize")
        return {"answer": "synthetic [1]"}

    return _chat


def test_http_brain_ask_stops_working_once_its_deadline_has_passed(
    make_state: Callable[..., mcp_server._State], monkeypatch: pytest.MonkeyPatch
) -> None:
    make_state(call_timeout_s=0.05)
    calls: list[str] = []
    monkeypatch.setattr(chat, "chat_json", _counting_chat(calls, plan_sleep_s=0.2))

    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_ask(question="synthetic question")

    assert calls == ["plan"]
    assert "deadline" in str(excinfo.value)


def test_http_brain_ask_within_its_deadline_answers_as_before(
    make_state: Callable[..., mcp_server._State],
    monkeypatch: pytest.MonkeyPatch,
    seed_doc: Callable[..., str],
) -> None:
    seed_doc(title="Synthetic playbook", content="The synthetic playbook covers mentorship.")
    make_state(call_timeout_s=CALL_TIMEOUT_S)
    calls: list[str] = []
    monkeypatch.setattr(chat, "chat_json", _counting_chat(calls, plan_sleep_s=0.0))

    payload = mcp_server.brain_ask(question="synthetic playbook")

    assert payload["answer"]
    assert calls[0] == "plan" and calls[-1] == "synthesize"


def test_a_deadline_is_late_only_once_its_clock_passes_it() -> None:
    now = [100.0]
    deadline = Deadline(lambda: now[0], 5.0)
    on_step = deadline_on_step(deadline)

    now[0] = 105.0
    on_step("retrieve", 1)
    now[0] = 105.5
    with pytest.raises(AskTimedOut):
        on_step("reflect", 1)


# ------------------------------------------------------------ the graph tools' depth


@pytest.mark.parametrize(
    "call",
    [
        lambda depth: mcp_server.brain_graphrag_search(query="q", mode="local", depth=depth),
        lambda depth: mcp_server.brain_graphrag_themes(person="someone", depth=depth),
        lambda depth: mcp_server.brain_graphrag_entity(name="q", depth=depth),
    ],
    ids=["search", "themes", "entity"],
)
def test_http_graph_tools_refuse_a_depth_above_the_configured_one(
    make_state: Callable[..., mcp_server._State], call: Any
) -> None:
    state = make_state(call_timeout_s=CALL_TIMEOUT_S)
    ceiling = state.cfg.graph_depth

    with pytest.raises(MCPError) as excinfo:
        call(ceiling + 1)

    assert f"depth={ceiling + 1} exceeds the configured ceiling of {ceiling}" in str(
        excinfo.value
    )


def test_http_graph_depth_at_the_configured_one_runs(
    make_state: Callable[..., mcp_server._State],
) -> None:
    state = make_state(call_timeout_s=CALL_TIMEOUT_S)

    payload = mcp_server.brain_graphrag_entity(name="nothing-here", depth=state.cfg.graph_depth)

    assert payload["mode"] == "local"


def test_stdio_graph_depth_is_not_bounded_here(
    make_state: Callable[..., mcp_server._State],
) -> None:
    state = make_state(call_timeout_s=None)
    deeper = state.cfg.graph_depth + 1

    payload = mcp_server.brain_graphrag_entity(name="nothing-here", depth=deeper)

    assert payload["mode"] == "local"
