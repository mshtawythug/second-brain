"""Version-agnostic bindings for the three `mcp` SDK names that moved in 2.0."""
import importlib
from collections.abc import Callable
from typing import Any, Protocol, TypeVar, cast

from mcp.types import ErrorData

_F = TypeVar("_F", bound=Callable[..., Any])

# ---------------------------------------------------------------------------
# What actually changed between mcp 1.x and 2.x
#
#   1.x                                  2.x
#   ---------------------------------    ------------------------------------
#   mcp.McpError                         mcp.MCPError
#   mcp.server.fastmcp.FastMCP           mcp.server.mcpserver.MCPServer
#   McpError(ErrorData(code=, message=)) MCPError(code, message, data=None)
#
# There is no alias in either direction — `McpError` is absent from mcp 2.0.0
# and `mcp.server.mcpserver` is absent from every 1.x — so the names have to be
# resolved at import time. Everything else this project touches is unchanged:
# `mcp.types.{INTERNAL_ERROR,INVALID_PARAMS,ErrorData}` (2.0 keeps `mcp.types`
# as a deliberate mirror of the new standalone `mcp_types` package), the
# `.tool()` decorator signature, `.run(transport=...)`, `.list_tools()`, and
# the client-side `ClientSession` / `StdioServerParameters` / `stdio_client`.
#
# The names are pulled via `importlib` rather than a `try: from mcp import ...`
# block on purpose. Under `mypy --strict` a direct conditional import needs a
# `# type: ignore` that is *required* on one major and *unused* (and therefore
# an error, because strict enables `warn_unused_ignores`) on the other — so
# there is no spelling of it that type-checks on both. Going through
# `import_module` keeps this module clean on either major, and the
# `MCPServerProtocol` cast below hands the typed surface back so the 41
# `@mcp_app.tool()` call sites still satisfy `disallow_untyped_decorators`.
# ---------------------------------------------------------------------------


class MCPServerProtocol(Protocol):
    """The slice of the SDK server class this project actually uses.

    Structurally satisfied by both `FastMCP` (1.x) and `MCPServer` (2.x); see
    the module comment for why the binding is a cast rather than an import.
    """

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[_F], _F]:
        """Register the decorated function as an MCP tool."""
        ...

    def run(self, transport: str = "stdio") -> None:
        """Serve the registered tools over ``transport``."""
        ...

    async def list_tools(self) -> list[Any]:
        """Return the registered tool descriptors."""
        ...


def _resolve() -> tuple[int, type[Exception], Any]:
    """Bind (major, error class, server class) for the installed `mcp`."""
    root = importlib.import_module("mcp")
    try:
        server_mod = importlib.import_module("mcp.server.mcpserver")
    except ModuleNotFoundError:
        server_mod = importlib.import_module("mcp.server.fastmcp")
        return 1, cast(type[Exception], root.McpError), server_mod.FastMCP
    return 2, cast(type[Exception], root.MCPError), server_mod.MCPServer


MCP_MAJOR, MCPError, _server_cls = _resolve()
"""``MCP_MAJOR`` is 1 or 2; ``MCPError`` is that major's connection-error type."""


def make_server(name: str) -> MCPServerProtocol:
    """Construct the SDK server object under whichever `mcp` major is installed."""
    return cast(MCPServerProtocol, _server_cls(name=name))


def mcp_error(code: int, message: str) -> Exception:
    """Build an `mcp` error exception, bridging the 2.0 constructor change.

    1.x takes a prebuilt :class:`~mcp.types.ErrorData`; 2.x takes ``code`` and
    ``message`` positionally and builds the ``ErrorData`` itself. Both leave the
    result reachable as ``exc.error``, which is what every call site reads.
    """
    if MCP_MAJOR >= 2:
        return MCPError(code, message)
    return MCPError(ErrorData(code=code, message=message))
