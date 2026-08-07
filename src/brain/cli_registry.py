"""Registration hub for the extracted `brain` CLI command modules.

``cli.py`` imports this module once and calls the ``register_*`` functions
below; each one attaches a group of commands (or a sub-app) to the root Typer
app. Adding a new command module therefore touches exactly two places — a new
``register_*`` entry here, and the call in ``cli.py`` — instead of growing the
import block at the top of ``cli.py``.

**Call position matters.** Typer renders ``brain --help`` in registration
order, so each ``register_*`` call sits in ``cli.py`` exactly where the
commands used to be declared. That is why ``brain list`` and the
``rm`` / ``mark-draft`` / ``mark-published`` trio have separate registrars
even though both live in :mod:`brain.cli_docs`: ``resurface`` / ``tag`` /
``edit`` are declared between them.

Extracted command modules must never import ``brain.cli`` at module scope —
``cli.py`` imports them, so the dependency only runs the other way at call
time. See the delegation block in each module.
"""
from __future__ import annotations

from collections.abc import Callable

import typer

from .cli_docs import register_lifecycle, register_list
from .cli_ingest import register as register_ingest
from .cli_note import register as register_note
from .cli_recall import register as register_recall
from .cli_search import register as register_search
from .cli_sensitivity import register_backfill as register_backfill_sensitivity
from .cli_usage import register as register_usage

#: Every registrar that attaches to the ROOT app, in the order ``cli.py``
#: invokes them. ``cli.py`` calls the individual functions directly so each
#: command lands at the right spot in the ``brain --help`` listing.
#:
#: HONESTY NOTE: nothing in production reads this tuple, and its only other
#: referent is a docstring in ``tests/test_usage_privacy.py``. (That module's
#: tests were themselves dead until 2026-08-07 — its skip predicate matched on
#: ``CommandInfo.name``, which Typer leaves ``None`` for a command registered
#: without an explicit name. Two layers of "looks wired, is not".) It is therefore an index
#: that cannot go stale loudly — it already had: ``__all__`` omitted
#: ``register_recall`` and ``register_usage`` while they were listed here, and
#: ``cli_review_extra`` is in neither (it attaches in ``cli.py`` directly, like
#: ``register_backfill_sensitivity`` below). Treat it as documentation of the
#: root-app surface, not as a registration mechanism — and if you add a
#: registrar, add it in BOTH places or the next reader inherits the same
#: half-truth.
REGISTRARS: tuple[tuple[str, Callable[[typer.Typer], None]], ...] = (
    ("note", register_note),
    ("ingest", register_ingest),
    ("search", register_search),
    ("recall", register_recall),
    ("usage", register_usage),
    ("list", register_list),
    ("lifecycle", register_lifecycle),
)

#: Registrars that attach to a SUB-APP rather than the root ``app``, and so
#: cannot live in :data:`REGISTRARS` (whose entries are all
#: ``Callable[[typer.Typer], None]`` invoked with the root app by
#: :func:`register_all`). Kept re-exported here anyway so ``cli.py`` keeps its
#: single import site for command registration.
#:
#: ``register_backfill_sensitivity`` takes the existing ``backfill_app`` and
#: attaches ``brain backfill scan-secrets``. Typing it into ``REGISTRARS``
#: would either be a lie or force a union receiver, and a union would let a
#: future contributor pass the wrong app with no type error — the command would
#: silently register at the wrong level of the CLI.
__all__ = [
    "REGISTRARS",
    "register_all",
    "register_backfill_sensitivity",
    "register_ingest",
    "register_lifecycle",
    "register_list",
    "register_note",
    # Were missing while present in REGISTRARS — the drift this module's
    # honesty note describes.
    "register_recall",
    "register_search",
    "register_usage",
]


def register_all(app: typer.Typer) -> None:
    """Attach every extracted command module to ``app`` in registrar order.

    Convenience for building a throwaway app (tests, tooling). Production
    wiring in ``cli.py`` calls the individual registrars instead, so that
    each command keeps its original position in ``brain --help``.
    """
    for _name, registrar in REGISTRARS:
        registrar(app)
