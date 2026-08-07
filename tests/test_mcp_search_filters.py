"""Tests for Q1-C MCP-layer metadata filters on ``brain_search``.

Sister to ``test_cli_search.py`` / ``test_cli_explain.py`` — locks the
MCP tool's surface (snake_case params, ISO-string date parsing, person
resolver wiring, has_tag alias semantics, and filter pass-through into
``hybrid_search``). Per plan §3.b test plan.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from mcp import McpError
from mcp.types import INVALID_PARAMS

from brain import mcp_server
from brain.config import Config
from brain.errors import PersonAmbiguous, PersonNotFound
from brain.queries import PersonMatch

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps schema fresh
    fake_embedder: object,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _spy_hybrid_search(captured: dict[str, Any]) -> Any:
    """Build a spy that records kwargs + returns no results."""

    def _spy(*_args: Any, **kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    return _spy


# ---------------------------------------------------------------------------
# ISO date parsing (`_parse_iso_datetime` error paths)
# ---------------------------------------------------------------------------


def test_brain_search_bad_iso_after_raises_invalid_params(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A malformed ISO ``after`` string surfaces as ``INVALID_PARAMS``."""
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_search(query="x", after="not-a-date")
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "after" in exc_info.value.error.message
    assert "ISO date" in exc_info.value.error.message


def test_brain_search_bad_iso_before_raises_invalid_params(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Symmetric: a malformed ISO ``before`` raises ``INVALID_PARAMS``."""
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_search(query="x", before="not-a-date")
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "before" in exc_info.value.error.message


# ---------------------------------------------------------------------------
# Person resolver error paths
# ---------------------------------------------------------------------------


def test_brain_search_person_ambiguous_raises_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """``PersonAmbiguous`` surfaces as ``INVALID_PARAMS`` with the
    candidate list embedded in the message so the MCP caller can show
    the user disambiguation hints."""
    def _raise(_conn: object, name: str) -> Any:
        raise PersonAmbiguous(name, ["Alice Doe", "Alice Xanthus"])

    monkeypatch.setattr(mcp_server, "resolve_person_to_keys", _raise)
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_search(query="x", person="Alice")
    assert exc_info.value.error.code == INVALID_PARAMS
    msg = exc_info.value.error.message
    assert "Alice Doe" in msg
    assert "Alice Xanthus" in msg


def test_brain_search_person_not_found_raises_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """``PersonNotFound`` surfaces as ``INVALID_PARAMS``."""
    def _raise(_conn: object, name: str) -> Any:
        raise PersonNotFound(name)

    monkeypatch.setattr(mcp_server, "resolve_person_to_keys", _raise)
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_search(query="x", person="Nobody")
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "Nobody" in exc_info.value.error.message


# ---------------------------------------------------------------------------
# has_tag / tag conflict + alias semantics
# ---------------------------------------------------------------------------


def test_brain_search_has_tag_tag_conflict_raises_invalid_params(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Conflicting ``tag`` / ``has_tag`` values raise ``INVALID_PARAMS``
    (mirror of the CLI ``BadParameter``)."""
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_search(query="x", tag="a", has_tag="b")
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "tag" in exc_info.value.error.message
    assert "has_tag" in exc_info.value.error.message


def test_brain_search_has_tag_tag_same_value_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Same value on both is the explicit-alias case — no error."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(mcp_server, "hybrid_search", _spy_hybrid_search(captured))
    payload = mcp_server.brain_search(query="x", tag="shared", has_tag="shared")
    assert captured["tag"] == "shared"
    # Return shape is still the Q1-C dict. Superset, not equality — F5's
    # metadata keys are additive by design.
    assert {"session_id", "results"} <= set(payload.keys())


# ---------------------------------------------------------------------------
# Filter pass-through into hybrid_search
# ---------------------------------------------------------------------------


def test_brain_search_filter_combination_threads_to_hybrid_search(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """``kind`` / ``thread`` / ``without_tag`` thread through verbatim."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(mcp_server, "hybrid_search", _spy_hybrid_search(captured))
    mcp_server.brain_search(
        query="x",
        kind="note",
        thread="t1",
        without_tag="private",
    )
    assert captured["content_type"] == "note"
    assert captured["thread_id"] == "t1"
    assert captured["without_tag"] == "private"


def test_brain_search_draft_true_threads_to_hybrid_search(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """The ``draft`` bool round-trips into ``hybrid_search`` unchanged."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(mcp_server, "hybrid_search", _spy_hybrid_search(captured))
    mcp_server.brain_search(query="x", draft=True)
    assert captured["draft"] is True


def test_brain_search_draft_false_threads_to_hybrid_search(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """And ``draft=False`` lands as ``False`` (not None / not absent)."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(mcp_server, "hybrid_search", _spy_hybrid_search(captured))
    mcp_server.brain_search(query="x", draft=False)
    assert captured["draft"] is False


def test_brain_search_person_success_threads_keys_and_display_name(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """On a successful resolve, the keys AND humanized display name
    both reach ``hybrid_search`` so ``SearchExplanation.matched_filters``
    is populated end-to-end."""
    captured: dict[str, Any] = {}

    def _resolve(_conn: object, _name: str) -> PersonMatch:
        return PersonMatch(
            display_name="Alice", keys=["alice@example.com", "alice"]
        )

    monkeypatch.setattr(mcp_server, "resolve_person_to_keys", _resolve)
    monkeypatch.setattr(mcp_server, "hybrid_search", _spy_hybrid_search(captured))

    mcp_server.brain_search(query="x", person="Alice")

    assert captured["person_keys"] == ["alice@example.com", "alice"]
    assert captured["person_display_name"] == "Alice"


def test_brain_search_after_before_parsed_to_datetime(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """ISO strings on ``after`` / ``before`` reach ``hybrid_search`` as
    parsed ``datetime`` objects (not str)."""
    from datetime import datetime as _dt

    captured: dict[str, Any] = {}
    monkeypatch.setattr(mcp_server, "hybrid_search", _spy_hybrid_search(captured))
    mcp_server.brain_search(
        query="x",
        after="2026-01-01",
        before="2026-05-01T12:30:00",
    )
    assert captured["after"] == _dt(2026, 1, 1)
    assert captured["before"] == _dt(2026, 5, 1, 12, 30)
