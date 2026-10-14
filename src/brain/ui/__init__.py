"""`brain ui` — the local, loopback-only web app over the brain (F14).

v1 ships the **Notes tab only**. Ingest and Agent are deliberately deferred:
each is a long-running *job* (chunk+embed, an Ollama plan/reflect loop) and
exposing one over HTTP forces a background-task subsystem — job store, progress
channel, cancellation — larger than the entire Notes tab. Those two tabs render
a one-line redirect to the CLI command that does the job today. This is a
decision (release plan R-8), not an oversight. A third deferred tab, for the
Quartz wiki build, was deleted at R2 (spec Q10): the wiki is dormant, and a tab
describing a retired subsystem is worse than no tab.

Layering, and the review rule that protects it:

- ``tree`` / ``render`` are **pure** — no I/O, no database, no config.
- ``queries`` holds every SELECT the UI needs beyond :mod:`brain.queries`.
- ``notes_service`` is the **only** module permitted to mutate anything, and
  every operation in it delegates to a function the CLI, the MCP server, or
  F8's ``vault/`` seams already own.
- ``routes_*`` are thin: parse → service → serialize.

**Reject any change that adds SQL to a route module, or writes a vault file
outside** :mod:`brain.ui.notes_service`. That is what stops this becoming a
second implementation of the brain.

Nothing here imports :mod:`brain.cli`; the package is importable from a request
handler without paying for a 9,800-line Typer module.
"""
from __future__ import annotations

from .context import UiContext

#: ``create_app`` is deliberately NOT re-exported here. Importing it would make
#: ``import brain.ui`` pull :mod:`brain.ui.app`, and through it ``starlette`` —
#: defeating the whole point of keeping :mod:`brain.ui.tree` and
#: :mod:`brain.ui.render` importable as pure modules, and making
#: ``tests/test_cli_ui.py::test_ui_tree_and_render_import_without_starlette_or_uvicorn``
#: fail. Callers that need the application import it from its own module:
#:
#:     from brain.ui.app import create_app
__all__ = ["UiContext"]
