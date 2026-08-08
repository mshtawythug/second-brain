"""Guards that the MCP layer stays portable across the mcp 1.x/2.x rename."""
import asyncio
import importlib.metadata
import re
from pathlib import Path

import pytest

from brain import mcp_compat
from brain.mcp_compat import MCP_MAJOR, MCPError, mcp_error

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "brain"

# The three spellings that are valid on exactly one mcp major and raise
# ImportError on the other. `mcp_compat` resolves them at import time; nothing
# else in `src/` may reference them directly. Dependabot PR #7 widened the pin
# to `<3.0` and merged, and `from mcp import McpError` in `mcp_server.py` took
# down 30 test modules at COLLECTION plus the `brain-mcp` entry point. These
# patterns are what makes that regression fail loudly instead of at import.
_VERSION_LOCKED_IMPORTS = (
    re.compile(r"^\s*from mcp import .*\bMcpError\b", re.M),
    re.compile(r"^\s*from mcp\.server\.fastmcp import", re.M),
    re.compile(r"^\s*from mcp\.server\.mcpserver import", re.M),
)


def _installed_mcp_major() -> int:
    """Return the major version of the installed `mcp` distribution."""
    return int(importlib.metadata.version("mcp").split(".", 1)[0])


def test_compat_binds_the_installed_mcp_major() -> None:
    """`mcp_compat` must resolve against whatever `mcp` is actually installed."""
    assert _installed_mcp_major() == MCP_MAJOR
    assert issubclass(MCPError, Exception)


def test_mcp_error_bridges_the_2_0_constructor_change() -> None:
    """`mcp_error` must produce a populated `.error` on either mcp major.

    1.x is ``McpError(ErrorData(code=, message=))`` and 2.x is
    ``MCPError(code, message)``. Passing the wrong shape does not raise a clean
    ImportError — it raises a pydantic ValidationError deep inside a tool call —
    so this is the arm of the migration that would fail silently in review.
    """
    exc = mcp_error(-32602, "bad prefix")

    assert isinstance(exc, MCPError)
    assert exc.error.code == -32602
    assert exc.error.message == "bad prefix"
    assert str(exc) == "bad prefix"


def test_error_constants_are_importable_from_mcp_types() -> None:
    """`mcp.types` keeps the v1 spelling in 2.0; the server relies on that."""
    from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData

    assert INTERNAL_ERROR == -32603
    assert INVALID_PARAMS == -32602
    assert ErrorData(code=1, message="x").message == "x"


@pytest.mark.parametrize("pattern", _VERSION_LOCKED_IMPORTS, ids=lambda p: p.pattern)
def test_no_version_locked_mcp_imports_outside_the_compat_module(
    pattern: "re.Pattern[str]",
) -> None:
    """Only `mcp_compat` may name a spelling that exists on one mcp major.

    Fails on the pre-migration tree (`mcp_server.py` matches the first two
    patterns) and passes after, independent of which `mcp` is installed.
    """
    offenders = [
        path.relative_to(SRC_ROOT).as_posix()
        for path in sorted(SRC_ROOT.rglob("*.py"))
        if path.name != "mcp_compat.py" and pattern.search(path.read_text())
    ]
    assert not offenders, (
        f"{pattern.pattern!r} is mcp-major-specific and must go through "
        f"brain.mcp_compat; found in: {offenders}"
    )


def test_server_app_is_built_through_the_compat_factory() -> None:
    """The live server object must come up and carry its registered tools."""
    from brain.mcp_server import mcp_app

    assert isinstance(mcp_app, mcp_compat._server_cls)
    tool_names = {tool.name for tool in asyncio.run(mcp_app.list_tools())}
    assert {"brain_search", "brain_show", "brain_status"} <= tool_names
