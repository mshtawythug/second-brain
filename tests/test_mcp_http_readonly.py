"""Every MCP tool is classified read or write, and the list fails closed (RM-46).

Over HTTP the server is read-only unless ``--allow-writes``: every tool NOT on
``READ_ONLY_TOOLS`` is removed from the registry, so a tool added later is
withheld until someone classifies it. The real registry is only LISTED here;
removal is exercised on throwaway servers, never on the module-global one.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from brain import mcp_server
from brain.mcp_compat import make_server
from brain.mcp_tool_classes import (
    READ_ONLY_TOOLS,
    apply_read_only,
    registered_tool_names,
    write_tool_names,
)
from brain.mcp_tools import related, saved_search

pytestmark = pytest.mark.nodb

#: The registry at RM-46: 42 tools in ``mcp_server`` plus 9 extension tools.
REGISTERED = 51
READS = 29
WRITES = 22


def _registered() -> tuple[str, ...]:
    return asyncio.run(registered_tool_names(mcp_server.mcp_app))


def test_every_read_tool_is_registered() -> None:
    # A renamed read tool must fail loudly, not vanish from HTTP.
    assert set(READ_ONLY_TOOLS) <= set(_registered())


def test_every_registered_tool_is_classified() -> None:
    names = _registered()

    assert len(names) == REGISTERED
    assert len(READ_ONLY_TOOLS) == READS
    assert len(write_tool_names(names)) == WRITES
    assert set(write_tool_names(names)) | set(READ_ONLY_TOOLS) == set(names)


def test_an_unknown_tool_is_a_write_tool() -> None:
    assert write_tool_names(["brain_search", "brain_brand_new"]) == ("brain_brand_new",)


def test_write_tool_names_are_sorted_and_unique() -> None:
    assert write_tool_names(["brain_z", "brain_a", "brain_z"]) == ("brain_a", "brain_z")


@pytest.mark.parametrize(
    "name",
    [
        "brain_edit",
        "brain_rm",
        "brain_ingest_stdin",
        "brain_note_new",
        "brain_graphrag_build",
        "brain_graphrag_aliases_apply",
        "brain_graphrag_remove",
        "brain_watch_add",
        "brain_watch_remove",
        "brain_connect_accept",
        "brain_rate",
    ],
)
def test_known_write_tools_are_withheld(name: str) -> None:
    assert name not in READ_ONLY_TOOLS
    assert name in _registered()


@pytest.mark.parametrize(
    ("tool", "phrase", "is_read"),
    [
        # Each judgement call rests on its tool's own docstring; if the
        # docstring changes, the classification must be looked at again.
        (mcp_server.brain_link_proposal, "Writes nothing", True),
        (mcp_server.brain_review_findings_list, "without scanning", True),
        (mcp_server.brain_graphrag_communities, "Read-only and fast", True),
        (saved_search.brain_watch_check, "WITHOUT recording it", True),
        (related.brain_related, "No embed call is made", True),
        (mcp_server.brain_review_weekly, "is written to", False),
        (mcp_server.brain_review_scan, "surfacing findings into the queue", False),
        (mcp_server.brain_daily, "writes the file", False),
        (mcp_server.brain_rate, "Record a thumbs-up", False),
    ],
)
def test_judgement_calls_are_pinned_to_their_docstrings(
    tool: Any, phrase: str, is_read: bool
) -> None:
    assert phrase in " ".join((tool.__doc__ or "").split())
    assert (tool.__name__ in READ_ONLY_TOOLS) is is_read


def _throwaway(*names: str) -> Any:
    server = make_server("throwaway")
    for name in names:

        def _tool() -> str:
            """A throwaway tool."""
            return "ok"

        server.tool(name=name)(_tool)
    return server


def test_apply_read_only_removes_the_unclassified_and_keeps_the_read() -> None:
    # RM-46 AC3: a dummy tool nobody classified is withheld.
    server = _throwaway("brain_search", "brain_brand_new", "brain_edit")

    withheld = apply_read_only(server, asyncio.run(registered_tool_names(server)))

    assert withheld == ("brain_brand_new", "brain_edit")
    assert asyncio.run(registered_tool_names(server)) == ("brain_search",)


def test_apply_read_only_with_nothing_to_withhold_keeps_everything() -> None:
    server = _throwaway("brain_search", "brain_show")

    withheld = apply_read_only(server, asyncio.run(registered_tool_names(server)))

    assert withheld == ()
    assert asyncio.run(registered_tool_names(server)) == ("brain_search", "brain_show")


def test_listing_the_real_registry_does_not_change_it() -> None:
    before = _registered()
    _registered()
    assert _registered() == before
    assert len(before) == REGISTERED
