"""MCP extension tools: tool modules registered on the server without editing ``mcp_server.py``.

**The extension seam (RM-06).** ``mcp_server.py`` calls :func:`register_extension_tools` once,
after its last built-in tool, with a :class:`ToolContext` of its own helpers. Every
non-underscore module of this package is a tool module, one per feature area, each under 400
lines. A tool module:

- keeps its context in a module global, ``_CONTEXT = ContextSlot(__name__)``;
- defines its ``brain_*`` tool functions at module scope, each reaching the server through
  ``_CONTEXT.get()``;
- defines ``register(app, ctx)``, which calls ``_CONTEXT.bind(ctx)`` FIRST and then
  ``app.tool()(fn)`` for each tool. Never the ``@app.tool()`` decorator form: a helper inserted
  between a decorator and its function silently registers the helper instead (the trap that
  once dropped ``brain_recall``);
- never imports ``brain.mcp_server`` (see :mod:`brain.mcp_tools._context` for why).

**The helpers every tool must use** (fields of :class:`ToolContext`):

- ``confidential_lens(include_confidential)`` — **every retrieval tool**, in the SQL predicate
  (not as redaction), and the same value for any facet counts. MCP serves a hosted model, which
  sits outside the F6 trust boundary; a tool that returns document titles or text without the
  lens is an egress hole. Default ``include_confidential`` to ``False``.
- ``get_state()`` and ``connection(state)`` — the config, embedder and DB connection.
- ``error(code, message)``, ``wrap_db_error(exc)``, ``wrap_embed_error(exc)`` — every failure
  reaches the caller as an MCP error, the DB and embed ones class-only.

``tests/test_mcp_tool_registration.py`` fails if a module-level ``brain_*`` function here is not
advertised, and ``tests/test_mcp_tools_seam.py`` fails if a module here imports
``brain.mcp_server``.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Sequence
from types import ModuleType

from ..mcp_compat import MCPServerProtocol
from ._context import _SLOTS, ContextSlot, ToolContext, ToolRegistrationError

__all__ = [
    "ContextSlot",
    "ToolContext",
    "ToolRegistrationError",
    "discover_tool_modules",
    "register_extension_tools",
]


def discover_tool_modules() -> list[ModuleType]:
    """Import and return every tool module of this package, in name order.

    Underscore-prefixed modules (``_context``) are infrastructure, not tool modules.
    """
    names = sorted(
        info.name for info in pkgutil.iter_modules(__path__) if not info.name.startswith("_")
    )
    return [importlib.import_module(f"{__name__}.{name}") for name in names]


def register_extension_tools(
    app: MCPServerProtocol,
    ctx: ToolContext,
    *,
    modules: Sequence[ModuleType] | None = None,
) -> None:
    """Call ``register(app, ctx)`` on every tool module.

    ``modules=None`` means every module :func:`discover_tool_modules` finds; a test passes its
    own module list, so a throwaway server never touches the real modules.

    Raises:
        ToolRegistrationError: a module has no callable ``register``, or one is already bound to
            a different context.
    """
    for module in discover_tool_modules() if modules is None else modules:
        register = getattr(module, "register", None)
        if not callable(register):
            raise ToolRegistrationError(
                f"{module.__name__} has no register(app, ctx); every brain.mcp_tools "
                "module must define one"
            )
        register(app, ctx)


def _reset_contexts_for_tests(modules: Sequence[ModuleType] | None = None) -> None:
    """Unbind tool modules' contexts so a test may register them again (test-only).

    ``modules=None`` unbinds every slot; otherwise only the slots those modules own.
    """
    owners = None if modules is None else {module.__name__ for module in modules}
    for owner, slot in _SLOTS.items():
        if owners is None or owner in owners:
            slot.reset()
