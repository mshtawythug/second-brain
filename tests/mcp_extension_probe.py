"""A test-only MCP extension tool module, in the shape every ``brain.mcp_tools`` module takes.

Not a test module itself (no ``test_`` prefix): ``tests/test_mcp_tools_seam.py`` passes it to
``register_extension_tools(..., modules=[...])`` on a throwaway server, and
``tests/test_mcp_server_protocol.py`` copies this file into a copy of the ``brain`` package as
``brain/mcp_tools/stdio_probe.py``, so the same source runs as a real extension under
``python -m brain.mcp_server``. It must therefore import nothing from ``tests``.
"""

from __future__ import annotations

from typing import Any

from brain.mcp_compat import MCPServerProtocol
from brain.mcp_tools import ContextSlot, ToolContext

_CONTEXT = ContextSlot(__name__)


def brain_probe_state() -> dict[str, Any]:
    """Name the type of the server state this tool's context reads (test-only probe)."""
    state = _CONTEXT.get().get_state()
    return {"state_type": type(state).__name__}


def register(app: MCPServerProtocol, ctx: ToolContext) -> None:
    """Bind ``ctx`` (refusing a different one), then register the tool on ``app``."""
    _CONTEXT.bind(ctx)
    app.tool()(brain_probe_state)
