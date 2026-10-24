"""End-to-end protocol test for ``brain-mcp``.

Spawns the server via ``python -m brain.mcp_server``, runs a real MCP
``initialize`` + ``tools/list`` round-trip over stdio, and asserts the core
tools (five read, three write) are advertised with non-empty input schemas.

RM-06 adds two checks of the extension seam over the same transport: the
advertised list equals the in-process ``mcp_app.list_tools()``, and a copy of
the package with one extension tool planted in ``brain/mcp_tools`` serves that
tool exactly once, from the module ``main()`` initialised.
"""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

import brain
from brain import mcp_server
from tests import mcp_extension_probe

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

EXPECTED_TOOLS = {
    "brain_search",
    "brain_show",
    "brain_list",
    "brain_status",
    "brain_resurface",
    "brain_ingest_stdin",
    "brain_tag",
    "brain_edit",
}


async def _list_tools_via_stdio(
    *,
    package_root: Path | None = None,
    call: str | None = None,
) -> dict[str, dict[str, object]]:
    """Spawn brain-mcp and return ``{tool_name: tool_dict}`` from tools/list.

    ``package_root`` runs the server from a copy of the ``brain`` package
    instead of the installed one (first on ``PYTHONPATH``, and the cwd, which
    ``-m`` puts at ``sys.path[0]``). ``call`` names a no-argument tool to call
    after listing; its JSON result is stored under ``"__call_result__"``.
    """
    env = {
        # Use `python -m brain.mcp_server` so the test isn't dependent on the
        # console script path resolving on $PATH inside the test runner. The
        # spec asks for the `__main__` block to make this exact invocation work.
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
    }
    if package_root is not None:
        env["PYTHONPATH"] = str(package_root)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "brain.mcp_server"],
        env=env,
        cwd=package_root,
    )
    call_result: Any = None
    with anyio.fail_after(15):
        # One statement, two contexts: ruff 0.16.8+ flags the nested form as
        # SIM117 (checked through 0.17.0); 0.16.4, still admitted by the
        # `ruff>=0.7` floor, does not.
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.list_tools()
            if call is not None:
                called = await session.call_tool(call, {})
                # `isError` is `is_error` on mcp 2.x; the alias dump reads both.
                dumped = called.model_dump(by_alias=True)
                assert not dumped["isError"], dumped
                call_result = json.loads(dumped["content"][0]["text"])
    # `Tool.inputSchema` (mcp 1.x) was renamed to `Tool.input_schema` in mcp
    # 2.0, where `inputSchema` survives only as the pydantic *alias* — i.e. it
    # round-trips on the wire but is no longer reachable by attribute access.
    # Dumping by alias gives the one spelling that works on both majors, and
    # keys this dict by the wire name the assertions below already use.
    tools: dict[str, dict[str, object]] = {
        t.name: {
            "description": t.description,
            "inputSchema": t.model_dump(by_alias=True)["inputSchema"],
        }
        for t in result.tools
    }
    if call is not None:
        tools["__call_result__"] = call_result
    return tools


#: What ``mcp_server`` must put in each :class:`~brain.mcp_tools.ToolContext` field: its own
#: helpers, from the importable module (a ``__main__``-built context names ``__main__.*``).
SERVER_CONTEXT_HELPERS = {
    "get_state": "brain.mcp_server._get_state",
    "connection": "brain.mcp_server._mcp_conn",
    "error": "brain.mcp_server._mcp_error",
    "wrap_db_error": "brain.mcp_server._wrap_db_error",
    "wrap_embed_error": "brain.mcp_server._wrap_embed_error",
    "confidential_lens": "brain.mcp_server._confidential_lens",
}


def _in_process_tool_names() -> set[str]:
    """What this process's importable ``brain.mcp_server`` advertises."""
    return {tool.name for tool in asyncio.run(mcp_server.mcp_app.list_tools())}


def _package_copy_with_probe(root: Path) -> Path:
    """Copy the installed ``brain`` package under ``root`` and plant the probe tool.

    No production module is modified or patched: the copy is a real tree with
    one more module in ``brain/mcp_tools``, which discovery finds as it would
    find RM-12's.
    """
    destination = root / "brain"
    shutil.copytree(
        Path(brain.__file__).parent,
        destination,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copyfile(
        Path(mcp_extension_probe.__file__),
        destination / "mcp_tools" / "stdio_probe.py",
    )
    return root


@pytest.mark.skipif(
    shutil.which(sys.executable) is None,
    reason="python interpreter not on PATH",
)
def test_stdio_tool_list_equals_in_process_list() -> None:
    """RM-06 AC5: ``python -m`` serves exactly what the importable module registers."""
    # Act
    served = set(anyio.run(_list_tools_via_stdio))

    # Assert
    assert served == _in_process_tool_names()


@pytest.mark.skipif(
    shutil.which(sys.executable) is None,
    reason="python interpreter not on PATH",
)
def test_served_module_registers_extensions_once_and_reads_main_state(
    tmp_path: Path,
) -> None:
    """An extension tool is served under ``python -m`` and reads ``main()``'s state.

    Each half fails under a different broken wiring. If the ``__main__`` block
    served its own copy, the probe would be missing (that copy registers no
    extension). If the ``__name__`` guard were gone, the ``__main__`` copy would
    bind the probe first and the import of ``brain.mcp_server`` would be
    refused a second context, so the server would not start. If the context
    held the wrong copy's ``_get_state``, the call would fail: only the served
    module's ``_state`` was set by ``main()``. And the ``context`` map pins each
    helper ``mcp_server`` passes: a swapped or stubbed field, or a context built
    by the ``__main__`` copy, names a different function.
    """
    # Arrange
    root = _package_copy_with_probe(tmp_path)

    # Act
    tools = anyio.run(
        lambda: _list_tools_via_stdio(package_root=root, call="brain_probe_state")
    )

    # Assert
    call_result = tools.pop("__call_result__")
    assert set(tools) == _in_process_tool_names() | {"brain_probe_state"}
    assert call_result == {
        "state_type": "_State",
        "context": SERVER_CONTEXT_HELPERS,
    }


@pytest.mark.skipif(
    shutil.which(sys.executable) is None,
    reason="python interpreter not on PATH",
)
def test_brain_mcp_tools_list_advertises_all_tools() -> None:
    """`brain-mcp` responds to tools/list with the core tools and their schemas.

    NOT "all tools", despite what this docstring said until 2026-08-20:
    ``EXPECTED_TOOLS`` is a hardcoded roster of 8 names and the check is a
    SUBSET test, so it stays green while any of the other 30-odd tools silently
    disappears -- which is exactly what happened to ``brain_recall`` on this
    branch. What this test does cover, and the reason to keep it, is the real
    stdio round trip and the SHAPE of the advertised schemas.

    Completeness is covered separately by
    ``tests/test_mcp_tool_registration.py``, which derives the expected roster
    from the module instead of hardcoding one.
    """
    tools = anyio.run(_list_tools_via_stdio)

    advertised = set(tools.keys())
    missing = EXPECTED_TOOLS - advertised
    assert not missing, f"server failed to advertise: {missing} (got {advertised})"

    for name in EXPECTED_TOOLS:
        schema = tools[name]["inputSchema"]
        assert isinstance(schema, dict), f"{name}: inputSchema not a dict"
        # The SDK always emits at minimum {"type":"object","properties":{...}}.
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

    # brain_resurface advertises its three optional params (Plan 02).
    resurface_props = tools["brain_resurface"]["inputSchema"]["properties"]  # type: ignore[index]
    for arg in ("limit", "min_age_days", "source_kind"):
        assert arg in resurface_props, f"brain_resurface missing {arg}"

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
