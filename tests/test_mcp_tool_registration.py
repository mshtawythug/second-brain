"""Guards that the ``@mcp_app.tool()`` decorators land on the intended functions.

A decorator binds to whatever ``def`` follows it. Inserting a helper between an
existing ``@mcp_app.tool()`` and the function it was written for silently moves
the registration onto the helper and drops the real tool off the MCP surface —
no import error, no type error, no test failure anywhere else in the suite.
That is exactly what happened to ``brain_recall`` on this branch.

Both guards derive their expectations from the module at run time rather than
from a hardcoded roster, so neither has a list that can rot as tools are added.

Since RM-06 the roster also covers every tool module of ``brain.mcp_tools``: a
``brain_*`` function defined there must be advertised by the same ``mcp_app``,
which is how the guard catches a module whose ``register`` forgot a tool.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Sequence
from types import ModuleType

from brain import mcp_server, mcp_tools
from brain.mcp_compat import MCPServerProtocol, make_server
from tests import mcp_extension_probe


def _registered_tool_names(app: MCPServerProtocol = mcp_server.mcp_app) -> set[str]:
    """Names the server actually advertises over MCP."""
    return {tool.name for tool in asyncio.run(app.list_tools())}


def _defined_tool_functions(module: ModuleType) -> dict[str, Callable[..., object]]:
    """Module-level ``brain_*`` functions defined in ``module`` itself.

    The ``__module__`` check drops ``brain_*`` names merely imported into the
    module, so this stays a roster of functions that file defines.
    """
    return {
        name: obj
        for name, obj in vars(module).items()
        if inspect.isfunction(obj)
        and obj.__module__ == module.__name__
        and name.startswith("brain_")
    }


def _module_tool_functions(
    extension_modules: Sequence[ModuleType] | None = None,
) -> dict[str, Callable[..., object]]:
    """``mcp_server``'s own ``brain_*`` functions plus every extension module's."""
    if extension_modules is None:
        extension_modules = mcp_tools.discover_tool_modules()
    roster = _defined_tool_functions(mcp_server)
    for module in extension_modules:
        roster.update(_defined_tool_functions(module))
    return roster


def _unregistered(app: MCPServerProtocol, extension_modules: Sequence[ModuleType]) -> list[str]:
    """Roster names ``app`` does not advertise."""
    return sorted(set(_module_tool_functions(extension_modules)) - _registered_tool_names(app))


def test_every_brain_tool_function_is_registered() -> None:
    """A ``brain_*`` tool function that lost its decorator must fail loudly.

    This is the direction that removes capability: the function still exists and
    still imports, it is simply no longer reachable by any agent.
    """
    # Arrange
    extension_modules = mcp_tools.discover_tool_modules()

    # Act
    missing = _unregistered(mcp_server.mcp_app, extension_modules)

    # Assert
    assert not missing, (
        "these brain_* functions are defined in brain/mcp_server.py or a "
        f"brain.mcp_tools module but are NOT registered as MCP tools: {missing}. "
        "The usual cause is a helper inserted between an @mcp_app.tool() "
        "decorator and the function it was meant to decorate, or a tool "
        "module's register() that forgot an app.tool(fn) call."
    )


def test_the_guard_flags_an_extension_tool_that_was_not_registered() -> None:
    """The extension half of the roster is only a guard if it can fail.

    It drives the same helper with the test-only probe on a throwaway server
    whose ``register`` was never called, so the proof does not depend on
    unregistering a real tool module (``brain.mcp_tools.related``, RM-12)
    from the shared server.
    """
    # Arrange
    app = make_server("rm06-unregistered")

    # Act
    missing = _unregistered(app, [mcp_extension_probe])

    # Assert
    assert "brain_probe_state" in missing


def test_no_private_helper_is_exposed_as_a_tool() -> None:
    """The mirror direction: a helper must not capture a stray decorator.

    Independent of the guard above — a private helper can be registered while
    every real tool is also registered, so neither test implies the other.
    """
    # Arrange / Act
    registered = _registered_tool_names()

    # Assert
    private = sorted(name for name in registered if name.startswith("_"))
    assert not private, (
        f"private helpers exposed on the MCP tool surface: {private}. "
        "An underscore-prefixed name reaching agents means an @mcp_app.tool() "
        "decorator bound to the wrong function."
    )
