"""`brain ingest-folder` — ingest the files saved into a drop folder (RM-33).

Registered through :data:`brain.cli_registry.EXTENSION_REGISTRARS` (RM-06), so
``cli.py`` is not touched. The move-first runner is :mod:`brain.drop_folder`, the
folders are read by :mod:`brain.settings.drop_folders`, and images are OCR'd by
:mod:`brain.ingest.image`; this module checks the scope, picks the folders, builds
the ingest step and prints the report.

**Scope (protocol point 5).** ``--dir PATH`` for one folder or ``--configured`` for
every configured folder with ``auto = true``, and ``--since Nd`` either way. ``--dir``
uses the folder's configured entry when it has one (``auto = false`` included: that
is what "only an explicit run" means), else the defaults: kind ``screenshot``,
``.png/.jpg/.jpeg``, ``confidential``. The opt-in LaunchAgent ``com.brain.dropfolders``
runs ``brain ingest-folder --configured --since 2d`` every 10 minutes.

**OCR.** A folder that takes images is skipped, with the fix printed, when OCR is off
or ``tesseract`` is missing: otherwise every run would move each file and move it back.

**Exit codes.** 0 when nothing failed (files left for the next run included); 1 when a
file failed or a folder could not be used; 2 for the scope refusal. Failures are
printed by class only (``describe_failure``).
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import typer

from .cli_ingest import (
    _ALLOW_SECRETS_HELP,
    _build_embedder,
    _build_enricher,
    _build_graph_syncer,
    _emit_egress_notice,
    _emit_secret_notice,
)
from .config import Config, ConfigError, _brain_home_root
from .db import connect
from .drop_folder import (
    SETTLE_S,
    DropFolder,
    DropFolderError,
    DropReport,
    IngestFn,
    run,
    scan,
)
from .errors import describe_failure
from .ingest import Embedder, ingest_document
from .ingest.dispatch import extract_path
from .ingest.image import extract_image, ocr_unavailable_hint
from .ingest.ocr import OcrSetup, resolve_ocr
from .ingest.outcome import IngestOutcome, classify
from .sensitivity import VALID_SENSITIVITY_LEVELS
from .settings.drop_folders import (
    DEFAULT_EXTENSIONS,
    DEFAULT_KIND,
    DEFAULT_SENSITIVITY,
    load_drop_folders,
)

if TYPE_CHECKING:
    from .enrichment import OllamaEnricher
    from .graph_rag.sync import GraphSyncer

SCOPE_REFUSAL = "ingest-folder requires --dir PATH or --configured, and --since Nd"
MAX_SINCE_DAYS = 365
GUIDE = "docs/guides/drop-folders.md"
#: The extensions the image extractor reads, which need OCR.
IMAGE_EXTENSIONS = DEFAULT_EXTENSIONS
_SINCE = re.compile(r"(\d{1,3})d")
_STATUS = {
    IngestOutcome.INGESTED: "ingested",
    IngestOutcome.UPDATED: "updated",
    IngestOutcome.MIRROR_REPAIRED: "updated",
    IngestOutcome.SKIPPED: "unchanged",
    IngestOutcome.EMPTY: "empty",
}

#: The production OCR resolver; tests swap it for a fake (``monkeypatch.setattr``).
ocr_resolver: Callable[[Mapping[str, str]], OcrSetup] = resolve_ocr


def ingest_folder(
    directory: Path | None = typer.Option(
        None,
        "--dir",
        exists=True,
        file_okay=False,
        dir_okay=True,
        help="One folder (its configured settings if it has them, else the screenshot "
        "defaults).",
    ),
    configured: bool = typer.Option(
        False, "--configured", help="Every folder in drop-folders.toml with auto = true."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Only files modified in the last N days, as Nd (1d to 365d)."
    ),
    sensitivity: str | None = typer.Option(
        None, "--sensitivity", help="Override the folder's sensitivity for this run."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="List what would be ingested."),
    enrich: bool = typer.Option(True, "--enrich/--no-enrich", help="Add the Ollama auto-summary."),
    allow_secrets: bool = typer.Option(False, "--allow-secrets", help=_ALLOW_SECRETS_HELP),
) -> None:
    """Ingest new files from drop folders: move each to imported/, then ingest it.

    Requires --dir PATH or --configured, and --since Nd. Screenshots are OCR'd
    and stored confidential. Folders live in $BRAIN_HOME/drop-folders.toml.
    """
    cutoff = _scope_or_exit(directory, configured, since, now=datetime.now(UTC))
    if sensitivity is not None and sensitivity not in VALID_SENSITIVITY_LEVELS:
        typer.secho(
            f"--sensitivity must be one of {', '.join(sorted(VALID_SENSITIVITY_LEVELS))}",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=2)
    folders = _folders_or_exit(directory)
    if sensitivity is not None:
        folders = [replace(f, sensitivity=sensitivity) for f in folders]
    if not folders:
        typer.echo(f"no drop folders configured (see {GUIDE})")
        return
    cfg = Config.load()  # publishes the .env chain, so the OCR knobs below read it
    ocr = ocr_resolver(os.environ)
    usable, failed = _check_ocr(folders, ocr)
    if dry_run:
        _dry_run(usable, cutoff)
    elif usable:
        run_failed = _ingest_all(
            cfg, usable, cutoff, ocr=ocr, enrich=enrich, allow_secrets=allow_secrets
        )
        failed = failed or run_failed
    if failed:
        raise typer.Exit(code=1)


def _refuse(message: str) -> None:
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(code=2)


def _scope_or_exit(
    directory: Path | None, configured: bool, since: str | None, *, now: datetime
) -> datetime:
    if since is None or (directory is None) == (not configured):
        _refuse(SCOPE_REFUSAL)
    match = _SINCE.fullmatch(since or "")
    days = int(match.group(1)) if match else 0
    if not 1 <= days <= MAX_SINCE_DAYS:
        _refuse(f"{SCOPE_REFUSAL}: --since must be 1d to {MAX_SINCE_DAYS}d")
    return now - timedelta(days=days)


def _folders_or_exit(directory: Path | None) -> list[DropFolder]:
    try:
        configured = load_drop_folders(_brain_home_root())
    except ConfigError as exc:
        # The settings leaf names the entry and key, never a value.
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1) from exc
    if directory is None:
        return [f for f in configured if f.auto]
    path = directory.resolve()
    match = next((f for f in configured if f.path == path), None)
    if match is not None:
        return [match]
    return [
        DropFolder(
            path=path,
            mode="ingest",
            kind=DEFAULT_KIND,
            extensions=DEFAULT_EXTENSIONS,
            sensitivity=DEFAULT_SENSITIVITY,
        )
    ]


def _check_ocr(folders: list[DropFolder], ocr: OcrSetup) -> tuple[list[DropFolder], bool]:
    """Skip, with the fix, every image folder when OCR cannot run. ``(usable, any_skipped)``."""
    hint = ocr_unavailable_hint(ocr)
    if hint is None:
        return folders, False
    usable = [f for f in folders if not f.extensions & IMAGE_EXTENSIONS]
    for folder in folders:
        if folder not in usable:
            typer.secho(f"skipped {folder.path}: {hint}", fg="red", err=True)
    return usable, len(usable) < len(folders)


def _dry_run(folders: list[DropFolder], cutoff: datetime) -> None:
    until = datetime.now(UTC) - timedelta(seconds=SETTLE_S)
    for folder in folders:
        found = scan(folder, since=cutoff, until=until)
        typer.echo(f"{folder.path}: {len(found)} file(s)")
        for path in found:
            typer.echo(f"  would ingest  {path.name}")


@dataclass(frozen=True)
class _Context:
    cfg: Config
    embedder: Embedder
    enricher: OllamaEnricher | None
    graph_syncer: GraphSyncer
    ocr: OcrSetup
    enrich: bool
    allow_secrets: bool


def _ingest_all(
    cfg: Config,
    folders: list[DropFolder],
    cutoff: datetime,
    *,
    ocr: OcrSetup,
    enrich: bool,
    allow_secrets: bool,
) -> bool:
    """Run every folder; ``True`` when any file or folder failed."""
    ctx = _Context(
        cfg=cfg,
        embedder=_build_embedder(cfg),
        enricher=_build_enricher(cfg) if enrich else None,
        graph_syncer=_build_graph_syncer(cfg),
        ocr=ocr,
        enrich=enrich,
        allow_secrets=allow_secrets,
    )
    failed = False
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        for folder in folders:
            try:
                report = run(conn, folder, since=cutoff, ingest=_ingest_step(ctx, folder))
            except (DropFolderError, OSError) as exc:
                typer.secho(f"skipped {folder.path}: {_folder_error(exc)}", fg="red", err=True)
                failed = True
                continue
            _print_report(report)
            failed = failed or report.failed
    return failed


def _folder_error(exc: DropFolderError | OSError) -> str:
    # A DropFolderError message is fixed text; an OSError's could quote a path.
    return str(exc) if isinstance(exc, DropFolderError) else describe_failure(exc)


def _ingest_step(ctx: _Context, folder: DropFolder) -> IngestFn:
    def ingest(
        conn: psycopg.Connection[Any], path: Path, *, title: str, timeout: float
    ) -> str:
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            doc = extract_image(path, ocr=ctx.ocr, timeout=timeout)
        else:
            doc = extract_path(path)
        result = ingest_document(
            conn,
            embedder=ctx.embedder,
            doc=replace(doc, title=title),
            source_kind=folder.kind,
            vault_root=ctx.cfg.vault_path,
            enricher=ctx.enricher,
            enrich=ctx.enrich,
            enrich_min_tokens=ctx.cfg.enrich_min_tokens,
            graph_syncer=ctx.graph_syncer,
            secret_guard=ctx.cfg.secret_guard,
            allow_secrets=ctx.allow_secrets,
            sensitivity=folder.sensitivity,
        )
        _emit_secret_notice(result.secret_notice)
        _emit_egress_notice(result.egress_notice)
        outcome, _ = classify(result)
        return _STATUS[outcome]

    return ingest


def _print_report(report: DropReport) -> None:
    for outcome in report.outcomes:
        line = f"  {outcome.status:<9} {outcome.name}"
        if outcome.detail:
            line += f" ({outcome.detail})"
        typer.secho(line, fg="red" if outcome.status == "failed" else None)
    counts = report.counts()
    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "nothing new"
    if report.deferred:
        summary += f"; {report.deferred} left for the next run"
    typer.echo(f"{report.folder}: {summary}")


def register(app: typer.Typer) -> None:
    app.command(name="ingest-folder")(ingest_folder)
