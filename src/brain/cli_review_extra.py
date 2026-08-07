"""`brain review snooze` / `resolve` — the review queue's missing status writers.

Migration 017 gave ``elicitation_gaps`` a four-state lifecycle
(``surfaced → snoozed → dismissed → resolved``) and a ``snoozed_until``
deadline, and :func:`brain.review.queries.list_review_queue` has honoured both
since it was written — holding snoozed rows back until their deadline passes.
Nothing ever wrote those two states from the ``brain review`` surface, so the
reader was unreachable from the user's side. These two commands close that loop.

Thin Typer orchestration only: the SQL lives in :mod:`brain.review.queries`
alongside the sibling ``dismiss`` writer, and shared CLI helpers stay owned by
``cli.py`` (resolved at call time — see the delegation block below — because
``cli.py`` imports this module to register its commands).
"""
from __future__ import annotations

import typer

from .config import Config
from .db import connect

#: Default snooze window, in days. Matches the "look at this again next week"
#: rhythm of ``brain review weekly``; override with ``--days``.
DEFAULT_SNOOZE_DAYS = 7


# ---------------------------------------------------------------------------
# Delegation to `brain.cli`-owned helpers.
#
# `_load_config_or_exit` stays in `cli.py` — ~20 commands call it and the test
# suite patches it at `brain.cli.<name>`. Resolving the attribute at call time
# keeps that patch point effective here. Same pattern as `cli_search.py`.
# ---------------------------------------------------------------------------


def _load_config_or_exit() -> Config:
    """Load config via the ``brain.cli`` patch point (clean exit on bad env)."""
    from . import cli as _cli

    return _cli._load_config_or_exit()


def review_snooze(
    id_prefix: str = typer.Argument(
        ..., metavar="FINDING-ID", help="ID prefix of the finding to snooze."
    ),
    days: int = typer.Option(
        DEFAULT_SNOOZE_DAYS,
        "--days",
        help=f"Days to hide the finding for (default: {DEFAULT_SNOOZE_DAYS}).",
    ),
) -> None:
    """Hide a review finding until ``--days`` from now.

    Snoozing is the "not now" verb, distinct from ``dismiss`` ("this was noise")
    and ``resolve`` ("I acted on it"). The finding drops out of
    ``brain review list`` and comes back on its own once the deadline passes —
    no follow-up command needed. Re-snoozing an already-snoozed finding simply
    moves the deadline.
    """
    from .review.queries import snooze_review_finding

    cfg = _load_config_or_exit()
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        try:
            finding_id = snooze_review_finding(
                conn, tenant_id=cfg.graph_tenant_id, id_prefix=id_prefix, days=days
            )
        except ValueError as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(code=1) from exc
    plural = "" if days == 1 else "s"
    typer.echo(f"Snoozed {finding_id[:8]} for {days} day{plural}.")


def review_resolve(
    id_prefix: str = typer.Argument(
        ..., metavar="FINDING-ID", help="ID prefix of the finding to resolve."
    ),
) -> None:
    """Close a review finding as acted upon (sets ``status='resolved'``).

    Use this when the contradiction was reconciled or the stale note was
    updated — ``dismiss`` is for findings that were never real. Resolving
    releases the row from the ``WHERE status <> 'resolved'`` partial unique
    index, so a future scan may legitimately re-surface the same target if it
    goes stale or conflicting again. Idempotent.
    """
    from .review.queries import resolve_review_finding

    cfg = _load_config_or_exit()
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        try:
            finding_id = resolve_review_finding(
                conn, tenant_id=cfg.graph_tenant_id, id_prefix=id_prefix
            )
        except ValueError as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(code=1) from exc
    typer.echo(f"Resolved {finding_id[:8]}.")


def register_review_extra_commands(review_app: typer.Typer) -> None:
    """Attach ``snooze`` / ``resolve`` to the ``brain review`` sub-app.

    Called from ``cli.py`` immediately after ``review dismiss`` so Typer lists
    the four status verbs together in ``brain review --help``.
    """
    review_app.command("snooze")(review_snooze)
    review_app.command("resolve")(review_resolve)
