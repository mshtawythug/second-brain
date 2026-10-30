"""``brain ingest-dir --sync``'s terminal half: flag checks, refusals, the prune report (RM-37).

The prune's rules live in :mod:`brain.ingest.import_sync`; this module maps them onto the
command: a usage error (exit 2) for ``--apply`` / ``--allow-empty`` without ``--sync`` or
``--apply`` with ``--dry-run``, a red refusal and exit 1 before anything is written, then one line
per document. A leaf of its own because ``cli_ingest.py`` is over the 800-line ceiling.

Paths reach the terminal (never titles) through :func:`~brain.ingest.text_safety.one_line`, and a
failed delete is reported by class only (:func:`~brain.errors.describe_failure`).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import psycopg
import typer

from .db import connect
from .ingest.import_sync import (
    ImportSyncRefused,
    PruneCandidate,
    PrunePlan,
    PruneReport,
    apply_prune,
    plan_prune,
    refusal_for,
    resolve_sync_root,
)
from .ingest.text_safety import one_line
from .vault.delete import DocumentGraphSyncer

SYNC_HELP = (
    "After the walk, find documents imported from files under DIR that are no longer on disk. "
    "Lists them (a dry run) unless --apply."
)
APPLY_HELP = "With --sync: delete the documents whose files are gone."
ALLOW_EMPTY_HELP = (
    "With --sync: prune even when the walk finds no files, or when --apply would remove more "
    "than half of the documents under DIR."
)

_OUTCOME_LABELS = {
    "deleted": "deleted",
    "kept": "kept (back on disk)",
    "unchecked": "kept (cannot check)",
    "changed": "kept (row changed)",
    "missing": "already gone",
    "failed": "failed",
}


def check_sync_flags(*, sync: bool, apply: bool, allow_empty: bool, dry_run: bool) -> None:
    """Exit 2 on a flag combination that has no meaning (plan decision D4)."""
    if not sync and (apply or allow_empty):
        raise typer.BadParameter("--apply and --allow-empty need --sync")
    if apply and dry_run:
        raise typer.BadParameter("--apply cannot be combined with --dry-run")


def _refuse(reason: str) -> NoReturn:
    typer.secho(f"sync refused: {one_line(reason)}", fg="red", err=True)
    raise typer.Exit(code=1)


def _line(label: str, candidate: PruneCandidate) -> str:
    return f"  {label}: {candidate.document_id[:8]}  {one_line(candidate.relative_path)}"


@dataclass(frozen=True)
class DirSync:
    """One ``--sync`` run of ``ingest-dir``: the resolved root and the run's settings."""

    root: Path
    vault_root: Path
    extensions: frozenset[str]
    supported_files: int
    apply: bool
    allow_empty: bool

    @classmethod
    def start_or_exit(
        cls,
        directory: Path,
        *,
        vault_root: Path,
        extensions: Iterable[str],
        supported_files: int,
        apply: bool,
        allow_empty: bool,
    ) -> DirSync:
        """Resolve the root, or refuse (R1a, R2) before the walk writes anything."""
        try:
            root = resolve_sync_root(directory, vault_root=vault_root)
        except ImportSyncRefused as exc:
            _refuse(str(exc))
        return cls(
            root=root,
            vault_root=vault_root,
            extensions=frozenset(e.lower() for e in extensions),
            supported_files=supported_files,
            apply=apply,
            allow_empty=allow_empty,
        )

    def _plan(self, conn: psycopg.Connection[Any]) -> PrunePlan:
        return plan_prune(
            conn, root=self.root, vault_root=self.vault_root, extensions=self.extensions
        )

    def _refusal(self, plan: PrunePlan, *, apply: bool) -> str | None:
        return refusal_for(
            plan, supported_files=self.supported_files, apply=apply, allow_empty=self.allow_empty
        )

    def preflight_or_exit(self, conn: psycopg.Connection[Any]) -> None:
        """Refuse (R1b, the mass-deletion guard) before the walk writes anything."""
        reason = self._refusal(self._plan(conn), apply=self.apply)
        if reason is not None:
            _refuse(reason)

    def dry_run_or_exit(self, database_url: str) -> None:
        """``--dry-run --sync``: the preflight and the listing on a connection of its own."""
        with connect(database_url) as conn:
            self.preflight_or_exit(conn)
            self.prune(conn, graph_syncer=None)

    def prune(
        self, conn: psycopg.Connection[Any], *, graph_syncer: DocumentGraphSyncer | None
    ) -> PruneReport | None:
        """List the candidates, or (``--apply``) delete them. ``None`` when only listing."""
        plan = self._plan(conn)
        if not self.apply:
            self._print_listing(plan)
            return None
        reason = self._refusal(plan, apply=True)
        if reason is not None:
            _refuse(reason)
        report = apply_prune(conn, plan, vault_root=self.vault_root, graph_syncer=graph_syncer)
        for outcome in report.outcomes:
            line = _line(_OUTCOME_LABELS[outcome.action], outcome.candidate)
            if outcome.detail is not None:
                typer.secho(f"{line} — {outcome.detail}", fg="red", err=True)
            else:
                typer.echo(line)
        typer.echo(
            f"sync: deleted {report.deleted} of {plan.in_scope} documents under "
            f"{one_line(str(plan.root))}"
        )
        self._print_skipped(plan)
        return report

    def _print_listing(self, plan: PrunePlan) -> None:
        for candidate in plan.candidates:
            typer.echo(_line("would delete", candidate))
        typer.echo(
            f"sync: {len(plan.candidates)} of {plan.in_scope} documents under "
            f"{one_line(str(plan.root))} would be removed"
        )
        if plan.candidates:
            typer.echo("  re-run with --apply to delete them")
            if self._refusal(plan, apply=True) is not None:
                typer.secho(
                    "  --apply would refuse: more than half of them would go "
                    "(pass --allow-empty to override)",
                    fg="yellow",
                )
        self._print_skipped(plan)

    @staticmethod
    def _print_skipped(plan: PrunePlan) -> None:
        if plan.unverifiable:
            typer.echo(f"sync: could not check {plan.unverifiable} document(s); kept")
        if plan.under_vault:
            typer.echo(f"sync: skipped {plan.under_vault} document(s) under the brain vault")


def exit_if_prune_failed(report: PruneReport | None) -> None:
    """Exit 1 after the run when any delete failed (each was reported as it happened)."""
    if report is not None and report.failed:
        raise typer.Exit(code=1)
