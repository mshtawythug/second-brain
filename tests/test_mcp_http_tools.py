"""Over HTTP, an unexpected tool failure reaches the client as its class, never its text (RM-46).

sec-118 #5: a remote caller who can make a tool fail learns what the failure says,
and an ``OSError`` says where the vault lives. mcp 2.0's SDK puts ``str(exc)`` in the
result; 2.3's does not, but neither names the class. ``sanitize_tool_errors`` wraps
each served tool so both versions answer the same fixed text. ``MCPError``s (the
tools' own ``INVALID_PARAMS`` and ``INTERNAL_ERROR``) pass through verbatim. All
tools here are throwaway ones on a throwaway server; paths are synthetic.
"""
from __future__ import annotations

import logging
from typing import Any

import anyio
import pytest
from mcp.types import INVALID_PARAMS

from brain.errors import McpTransportError
from brain.mcp_compat import MCPError, make_server, mcp_error
from brain.mcp_http_tools import sanitize_tool_errors

pytestmark = pytest.mark.nodb

LEAK = "/synthetic/vault/private-note.md"


def _server() -> Any:
    server = make_server("throwaway")

    def brain_sync_fails() -> dict[str, Any]:
        """Fails like an unreadable vault file."""
        raise PermissionError(13, "Permission denied", LEAK)

    async def brain_async_fails() -> dict[str, Any]:
        """Fails with a message quoting a path."""
        raise ValueError(f"could not parse {LEAK}")

    def brain_refuses(id_prefix: str) -> dict[str, Any]:
        """Refuses with the tools' own INVALID_PARAMS."""
        raise mcp_error(INVALID_PARAMS, f"document not found: {id_prefix}")

    def brain_works(query: str) -> dict[str, Any]:
        """Answers normally."""
        return {"echo": query}

    for tool in (brain_sync_fails, brain_async_fails, brain_refuses, brain_works):
        server.tool()(tool)
    return server


def _call(server: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    async def run() -> Any:
        return await server.call_tool(name, arguments or {})

    return anyio.run(run)


@pytest.mark.parametrize(
    ("name", "kind"), [("brain_sync_fails", "PermissionError"), ("brain_async_fails", "ValueError")]
)
def test_an_unexpected_failure_names_its_class_and_nothing_else(
    name: str, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    server = _server()
    sanitize_tool_errors(server, [name])

    with caplog.at_level(logging.WARNING, logger="brain.mcp.http"), pytest.raises(
        MCPError
    ) as excinfo:
        _call(server, name)

    message = str(excinfo.value)
    assert message == f"tool {name} failed ({kind}); the server log names the tool and class"
    assert "/synthetic" not in message
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert name in logged and kind in logged
    assert "/synthetic" not in logged


def test_the_tools_own_refusals_pass_verbatim() -> None:
    server = _server()
    sanitize_tool_errors(server, ["brain_refuses"])

    with pytest.raises(MCPError) as excinfo:
        _call(server, "brain_refuses", {"id_prefix": "abcdef"})

    assert "document not found: abcdef" in str(excinfo.value)


def test_an_ordinary_call_is_unchanged() -> None:
    server = _server()
    sanitize_tool_errors(server, ["brain_works"])

    result = _call(server, "brain_works", {"query": "synthetic"})

    assert result.model_dump(by_alias=True)["structuredContent"] == {"echo": "synthetic"}


def test_only_the_named_tools_are_wrapped() -> None:
    server = _server()
    sanitize_tool_errors(server, ["brain_works"])

    # Not wrapped: whatever the SDK does with it, it is not our fixed text.
    try:
        outcome = str(_call(server, "brain_sync_fails").model_dump(by_alias=True))
    except Exception as exc:  # noqa: BLE001 - the SDK's own handling, either shape
        outcome = str(exc)
    assert "the server log names the tool and class" not in outcome


def test_a_server_without_a_tool_registry_is_refused() -> None:
    class Bare:
        async def list_tools(self) -> list[Any]:
            return []

        def remove_tool(self, name: str) -> None:
            return None

    with pytest.raises(McpTransportError, match="tool registry"):
        sanitize_tool_errors(Bare(), ["brain_search"])


def test_an_unknown_tool_name_is_refused() -> None:
    with pytest.raises(McpTransportError, match="brain_nope"):
        sanitize_tool_errors(_server(), ["brain_nope"])


def test_an_async_tools_own_refusal_passes_verbatim() -> None:
    server = make_server("throwaway")

    async def brain_async_refuses(id_prefix: str) -> dict[str, Any]:
        """Refuses with the tools' own INVALID_PARAMS, asynchronously."""
        raise mcp_error(INVALID_PARAMS, f"document not found: {id_prefix}")

    server.tool()(brain_async_refuses)
    sanitize_tool_errors(server, ["brain_async_refuses"])

    with pytest.raises(MCPError) as excinfo:
        _call(server, "brain_async_refuses", {"id_prefix": "abcdef"})

    assert "document not found: abcdef" in str(excinfo.value)


# ------------------------------------------- sec-118b N1: person_keys stay home


def test_dropped_keys_vanish_from_every_level_of_a_result() -> None:
    server = make_server("throwaway")

    def brain_search(query: str) -> dict[str, Any]:
        """Returns person_keys at the top, inside a list and inside a dict."""
        return {
            "person_keys": ["a@example.com"],
            "results": [{"matched_filters": {"person_keys": ["a@example.com"], "q": query}}],
            "explanation": {"matched_filters": {"person_keys": ["a@example.com"]}, "n": 1},
        }

    server.tool()(brain_search)
    sanitize_tool_errors(server, ["brain_search"], drop_keys=frozenset({"person_keys"}))

    result = _call(server, "brain_search", {"query": "synthetic"})
    content = result.model_dump(by_alias=True)["structuredContent"]

    assert "@example.com" not in str(content)
    assert content["results"][0]["matched_filters"] == {"q": "synthetic"}
    assert content["explanation"] == {"matched_filters": {}, "n": 1}


def test_without_drop_keys_a_result_is_unchanged() -> None:
    server = make_server("throwaway")

    def brain_search(query: str) -> dict[str, Any]:
        """Returns person_keys."""
        return {"person_keys": ["a@example.com"], "q": query}

    server.tool()(brain_search)
    sanitize_tool_errors(server, ["brain_search"])

    result = _call(server, "brain_search", {"query": "synthetic"})

    assert result.model_dump(by_alias=True)["structuredContent"]["person_keys"] == [
        "a@example.com"
    ]
