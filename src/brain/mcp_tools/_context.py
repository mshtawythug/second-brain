"""The helpers an MCP extension tool receives from ``mcp_server``, and the slot that holds them.

A tool module in :mod:`brain.mcp_tools` never imports ``brain.mcp_server``. Under
``python -m brain.mcp_server`` the server runs as ``__main__``, so an import of
``brain.mcp_server`` loads a SECOND copy whose ``_state`` is ``None``, and every tool reading it
would fail in exactly the process an MCP client talks to. The server passes its own helpers in
a :class:`ToolContext` instead, and each tool module keeps that context in a
:class:`ContextSlot` module global, bound by its ``register(app, ctx)``.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

import psycopg

from ..errors import BrainError, EmbedError


class ToolRegistrationError(BrainError):
    """An extension tool module was registered wrongly, or called before it was registered."""


@dataclass(frozen=True)
class ToolContext:
    """``mcp_server``'s helpers, passed to every extension tool module.

    Each field is the importable server module's own function, so ``get_state`` reads the
    ``_state`` that ``main()`` set (and that tests install with
    ``monkeypatch.setattr(mcp_server, "_state", ...)``).
    """

    #: ``mcp_server._get_state``: the initialised server state, or a ``BrainError``.
    get_state: Callable[[], Any]
    #: ``mcp_server._mcp_conn``: a DB connection for one tool call, given the state.
    connection: Callable[[Any], AbstractContextManager[psycopg.Connection[Any]]]
    #: ``mcp_server._mcp_error``: build the SDK's error with a JSON-RPC code and message.
    error: Callable[[int, str], Exception]
    #: ``mcp_server._wrap_db_error``: a class-only ``INTERNAL_ERROR`` for a Postgres failure.
    wrap_db_error: Callable[[psycopg.Error], Exception]
    #: ``mcp_server._wrap_embed_error``: a class-only ``INTERNAL_ERROR`` for an embed failure.
    wrap_embed_error: Callable[[EmbedError], Exception]
    #: ``mcp_server._confidential_lens``: the F6 ``sensitivity`` filter for retrieval.
    confidential_lens: Callable[[bool], str | None]


#: Every slot ever created, by owning module name, so the test-only reset can find them.
_SLOTS: dict[str, ContextSlot] = {}


class ContextSlot:
    """One tool module's bound :class:`ToolContext`.

    :meth:`bind` refuses a *different* context once one is bound: under ``python -m``, the
    server registers extensions exactly once, from the importable module, and a second bind
    would mean the ``__main__`` copy registered too, with a context whose ``_state`` stays
    ``None``. Binding an equal context again is a no-op.
    """

    def __init__(self, owner: str) -> None:
        self._owner = owner
        self._context: ToolContext | None = None
        _SLOTS[owner] = self

    @property
    def current(self) -> ToolContext | None:
        """The bound context, or ``None`` before ``register`` ran."""
        return self._context

    def bind(self, ctx: ToolContext) -> None:
        """Bind ``ctx``, refusing a different context than the one already bound.

        Raises:
            ToolRegistrationError: a different context is already bound.
        """
        if self._context is None:
            self._context = ctx
        elif self._context != ctx:
            raise ToolRegistrationError(
                f"{self._owner} is already bound to a different ToolContext; an extension "
                "tool module is registered once per process (tests: call "
                "brain.mcp_tools._reset_contexts_for_tests first)"
            )

    def get(self) -> ToolContext:
        """The bound context.

        Raises:
            ToolRegistrationError: ``register`` has not run for this module.
        """
        if self._context is None:
            raise ToolRegistrationError(
                f"{self._owner} is not registered: register(app, ctx) has not run"
            )
        return self._context

    def reset(self) -> None:
        """Unbind (test-only; reached through ``brain.mcp_tools._reset_contexts_for_tests``)."""
        self._context = None
