"""End-to-end protocol test for ``brain-mcp``.

Spawns the server via ``python -m brain.mcp_server``, runs a real MCP
``initialize`` + ``tools/list`` round-trip over stdio, and asserts the seven
tools (four read, three write) are advertised with non-empty input schemas.
"""
import os
import shutil
import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)

EXPECTED_TOOLS = {
    "brain_search",
    "brain_show",
    "brain_list",
    "brain_status",
    "brain_ingest_stdin",
    "brain_tag",
    "brain_edit",
}


async def _list_tools_via_stdio() -> dict[str, dict[str, object]]:
    """Spawn brain-mcp and return ``{tool_name: tool_dict}`` from tools/list."""
    # Use `python -m brain.mcp_server` so the test isn't dependent on the
    # console script path resolving on $PATH inside the test runner. The
    # spec asks for the `__main__` block to make this exact invocation work.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "brain.mcp_server"],
        env={
            "DATABASE_URL": TEST_DATABASE_URL,
            # Belt-and-suspenders: the server pulls these from the parent
            # environment, but stdio_client only forwards what we list here
            # plus the SDK's defaults. Path is required so Python can find
            # the editable-installed `brain` package.
            "PATH": os.environ.get("PATH", ""),
            "VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV", ""),
            "BRAIN_MCP_LOG_LEVEL": "WARNING",
            # Point at a non-routable host so warmup fails fast without an
            # Ollama server in the test environment. The failure is caught
            # and logged; the server still comes up.
            "OLLAMA_HOST": "http://127.0.0.1:1",
        },
    )
    with anyio.fail_after(15):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
    return {
        t.name: {
            "description": t.description,
            "inputSchema": t.inputSchema,
        }
        for t in result.tools
    }


@pytest.mark.skipif(
    shutil.which(sys.executable) is None,
    reason="python interpreter not on PATH",
)
def test_brain_mcp_tools_list_advertises_all_tools() -> None:
    """`brain-mcp` responds to tools/list with all seven tools and schemas."""
    tools = anyio.run(_list_tools_via_stdio)

    advertised = set(tools.keys())
    missing = EXPECTED_TOOLS - advertised
    assert not missing, f"server failed to advertise: {missing} (got {advertised})"

    for name in EXPECTED_TOOLS:
        schema = tools[name]["inputSchema"]
        assert isinstance(schema, dict), f"{name}: inputSchema not a dict"
        # FastMCP always emits at minimum {"type":"object","properties":{...}}.
        assert schema.get("type") == "object", f"{name}: schema type != object"
        assert "properties" in schema, f"{name}: schema missing properties"

    # Spot-check brain_search advertises the headline kwargs.
    search_props = tools["brain_search"]["inputSchema"]["properties"]  # type: ignore[index]
    assert "query" in search_props
    assert "limit" in search_props
    assert "source" in search_props
    assert "fts_only" in search_props

    # brain_show takes id_prefix.
    show_props = tools["brain_show"]["inputSchema"]["properties"]  # type: ignore[index]
    assert "id_prefix" in show_props

    # brain_status takes no args.
    status_props = tools["brain_status"]["inputSchema"]["properties"]  # type: ignore[index]
    assert status_props == {}

    # Write tools advertise the right kwargs.
    ingest_props = tools["brain_ingest_stdin"]["inputSchema"]["properties"]  # type: ignore[index]
    for arg in ("content", "source", "external_id", "title", "tags", "metadata"):
        assert arg in ingest_props, f"brain_ingest_stdin missing {arg}"

    tag_props = tools["brain_tag"]["inputSchema"]["properties"]  # type: ignore[index]
    for arg in ("id_prefix", "add", "remove"):
        assert arg in tag_props, f"brain_tag missing {arg}"

    edit_props = tools["brain_edit"]["inputSchema"]["properties"]  # type: ignore[index]
    for arg in (
        "id_prefix",
        "title",
        "content_type",
        "content",
        "metadata",
        "replace_metadata",
    ):
        assert arg in edit_props, f"brain_edit missing {arg}"
