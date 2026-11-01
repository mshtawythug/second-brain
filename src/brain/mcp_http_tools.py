"""Over HTTP, an unexpected tool failure is reported by class, never by its text (RM-46).

sec-118 #5. A tool that fails unexpectedly says why, and the why is local: an
``OSError`` names the vault path, a YAML error quotes the line it choked on. Over
stdio that stays on the machine; over HTTP it leaves it. mcp 2.0's SDK puts
``str(exc)`` into the tool result; 2.3's drops it, but neither keeps the class.
So ``brain mcp serve`` wraps every tool it serves: an ``MCPError`` (the tools'
own ``INVALID_PARAMS`` / ``INTERNAL_ERROR``, already worded for the caller)
passes through untouched, and anything else becomes one fixed sentence naming
the tool and the exception class. The class is logged locally at WARNING; the
message is not, because it can quote document content (the G1 log-privacy rule).

stdio never calls :func:`sanitize_tool_errors`.
"""
from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterable
from typing import Any

from mcp.types import INTERNAL_ERROR

from .errors import McpTransportError
from .mcp_compat import MCPError, mcp_error

logger = logging.getLogger("brain.mcp.http")


def _redacted(name: str, exc: Exception) -> Exception:
    kind = type(exc).__name__
    logger.warning("tool %s failed: %s", name, kind)
    return mcp_error(
        INTERNAL_ERROR, f"tool {name} failed ({kind}); the server log names the tool and class"
    )


def _guarded(name: str, fn: Callable[..., Any], is_async: bool) -> Callable[..., Any]:
    """``fn`` with every non-``MCPError`` failure replaced by :func:`_redacted`."""
    if is_async:

        @functools.wraps(fn)
        async def guarded_async(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except MCPError:
                raise
            except Exception as exc:  # every unexpected failure, by design
                raise _redacted(name, exc) from None

        return guarded_async

    @functools.wraps(fn)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except MCPError:
            raise
        except Exception as exc:  # every unexpected failure, by design
            raise _redacted(name, exc) from None

    return guarded


def sanitize_tool_errors(server: Any, names: Iterable[str]) -> None:
    """Wrap each named tool's function on ``server`` in place.

    The SDK keeps a registered tool's function on its ``Tool`` model (``fn``)
    behind the server's tool manager, the same on mcp 2.0 and 2.3. Replacing
    ``fn`` keeps the schema and argument handling the SDK already derived from
    the original. A server without that registry is refused rather than served
    unwrapped.
    """
    manager = getattr(server, "_tool_manager", None)
    get_tool = getattr(manager, "get_tool", None)
    if get_tool is None:
        raise McpTransportError(
            "this mcp SDK's tool registry is not supported; brain mcp serve cannot "
            "guarantee tool errors stay on this machine"
        )
    for name in names:
        tool = get_tool(name)
        if tool is None:
            raise McpTransportError(f"cannot wrap tool {name}: it is not registered")
        tool.fn = _guarded(name, tool.fn, tool.is_async)
