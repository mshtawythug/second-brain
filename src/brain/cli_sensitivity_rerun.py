"""Escalate-only ``--sensitivity`` for a document an ingest command already stored (sec-114 F11).

Lifted out of :mod:`brain.cli_ingest_web` (RM-41) when ``brain ingest-bookmarks``
(RM-44, plan D7) needed the same rule, so the two commands cannot drift. The
rule: without an explicit flag the stored tier is left alone; an explicit
``confidential`` raises the row and its mirror and fences (the ``brain
mark-confidential`` path); an explicit ``normal`` never lowers a confidential
row, and says how to (``brain mark-normal``).

A CLI-layer module, because it prints. The mirror propagation is a parameter,
so each command passes its own module-level reference and a test can replace it
there.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import psycopg
import typer

from .queries import set_document_sensitivity
from .sensitivity import is_confidential
from .vault.sensitivity_propagate import PropagationFailure

__all__ = ["Propagate", "escalate_stored_sensitivity"]

#: :func:`brain.vault.sensitivity_propagate.propagate_sensitivity_to_vault`'s shape.
Propagate = Callable[..., Iterable[PropagationFailure]]


def escalate_stored_sensitivity(
    conn: psycopg.Connection[Any],
    document_id: str,
    *,
    level: str,
    explicit: bool,
    vault_root: Path,
    label: str,
    propagate: Propagate,
) -> bool:
    """Apply an explicit ``level`` to a stored document, escalate only; ``True`` if raised.

    ``label`` is the item's terminal-safe name (a URL), printed in each line.
    """
    if not explicit:
        return False
    if not is_confidential(level):
        row = conn.execute(
            "SELECT sensitivity FROM documents WHERE id = %s", (document_id,)
        ).fetchone()
        if row is not None and is_confidential(row[0]):
            typer.echo(
                f"  kept confidential {label} "
                f"(lower it with: brain mark-normal {document_id[:8]})"
            )
        return False
    if not set_document_sensitivity(conn, document_id=document_id, level=level):
        return False
    for failure in propagate(conn, document_id, level=level, vault_root=vault_root):
        typer.secho(f"  mirror not updated {label}: {failure.message}", fg="yellow", err=True)
    typer.echo(f"  marked confidential {label}")
    return True
