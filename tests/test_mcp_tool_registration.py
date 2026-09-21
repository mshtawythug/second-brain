"""Guards that the ``@mcp_app.tool()`` decorators land on the intended functions.

A decorator binds to whatever ``def`` follows it. Inserting a helper between an
existing ``@mcp_app.tool()`` and the function it was written for silently moves
the registration onto the helper and drops the real tool off the MCP surface —
no import error, no type error, no test failure anywhere else in the suite.
That is exactly what happened to ``brain_recall`` on this branch.

Both guards derive their expectations from the module at run time rather than
from a hardcoded roster, so neither has a list that can rot as tools are added.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable

from brain import mcp_server


def _registered_tool_names() -> set[str]:
    """Names the live server actually advertises over MCP."""
    return {tool.name for tool in asyncio.run(mcp_server.mcp_app.list_tools())}


def _module_tool_functions() -> dict[str, Callable[..., object]]:
    """Module-level ``brain_*`` functions defined in ``mcp_server`` itself.

    The ``__module__`` check drops ``brain_*`` names merely imported into the
    module, so this stays a roster of functions this file defines.
    """
    return {
        name: obj
        for name, obj in vars(mcp_server).items()
        if inspect.isfunction(obj)
        and obj.__module__ == mcp_server.__name__
        and name.startswith("brain_")
    }


def test_every_brain_tool_function_is_registered() -> None:
    """A ``brain_*`` tool function that lost its decorator must fail loudly.

    This is the direction that removes capability: the function still exists and
    still imports, it is simply no longer reachable by any agent.
    """
    # Arrange
    defined = set(_module_tool_functions())

    # Act
    registered = _registered_tool_names()

    # Assert
    missing = sorted(defined - registered)
    assert not missing, (
        "these brain_* functions are defined in brain/mcp_server.py but are NOT "
        f"registered as MCP tools: {missing}. The usual cause is a helper "
        "inserted between an @mcp_app.tool() decorator and the function it was "
        "meant to decorate."
    )


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
