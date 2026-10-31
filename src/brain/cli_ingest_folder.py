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

**OCR and whisper.** A folder that takes images is skipped, with the fix printed, when
OCR is off or ``tesseract`` is missing, and a folder that takes audio when voice memos
cannot run (:mod:`brain.ingest.whisper`): otherwise every run would move each file and
move it back. Whisper's knobs are read only when some folder takes audio, so a bad one
never stops a screenshot folder.

**Voice memos** (RM-38) are transcribed by :func:`~brain.ingest.voice.extract_voice`,
with whisper's own timeout (``2 × duration + 60 s``), not what is left of the run's
deadline (RM-38.D1): the deadline stops new files from starting, never a memo already
being transcribed, so one run can last about two hours at the 60-minute cap.

**Failures.** A failure that would repeat on every run (an image that cannot be read,
is too large or times out in OCR, another malformed file, the secret guard) is raised
as :class:`~brain.drop_folder.RejectedFileError`, so the file stays in ``imported/`` as
``rejected``; any other failure (the database, the embedder) moves it back for the next
run. ``OcrUnavailableError`` is the second kind: it is configuration, not the file. So
are :data:`~brain.ingest.voice.TRANSIENT_VOICE_ERRORS` (RM-38.D4): whisper failing on the
WAV this run converted points at the model or the binary. Each prints a WARNING naming the
file's ``imported/`` name (its content hash) and the class, so a memo that fails every run
is visible in every run's log. A :class:`~brain.ingest.voice.WhisperFailedError` is also
counted per memo (:mod:`brain.ingest.voice_failures`, in ``$BRAIN_HOME/state``): the
third one rejects the memo instead (sec-107 F5), and a memo stored clears the count (only
once the ingest succeeds). A tool that cannot start is not counted: that is the machine,
never the memo. The ledger is best-effort (sec-107b N3): a ledger that cannot be read or
written is a class-only warning, and the memo is handled as if it had no count.

**A run with an audio folder** first removes temporary directories a crashed run left
(:func:`~brain.ingest.voice.sweep_stale_tempdirs`, sec-107 F4), and runs with SIGTERM
turned into ``SystemExit`` (:func:`~brain.ingest.capped_process.sigterm_as_system_exit`,
rev-107 F1), so a ``launchctl bootout`` mid-whisper kills whisper's process group and
removes the decoded WAV instead of leaving both behind.

**A capture folder** (``mode = "capture"``, RM-39) gets
:func:`brain.capture_import.importer` as its ingest step, the same one ``brain capture
import`` uses, so the two cannot diverge; each shared file becomes an inbox note and its
``imported/`` copy is deleted (:mod:`brain.capture_import`).

**Exit codes.** 0 when nothing failed (files left for the next run included); 1 when a
file failed or was rejected, or a folder could not be used; 2 for the scope refusal.
Failures are printed by class only (``describe_failure``), and every file name and path
through ``one_line``, so a name carrying an escape sequence or a newline cannot drive the
terminal or forge a line in the LaunchAgent's log.
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

from . import capture_import
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
    RejectedFileError,
    run,
    scan,
)
from .errors import SecretGuardError, describe_failure
from .ingest import Embedder, ExtractedDoc, IngestResult, ingest_document, voice_failures
from .ingest.capped_process import sigterm_as_system_exit
from .ingest.dispatch import extract_path
from .ingest.image import ImageError, OcrUnavailableError, extract_image, ocr_unavailable_hint
from .ingest.ocr import OcrSetup, resolve_ocr
from .ingest.outcome import IngestOutcome, classify
from .ingest.text_safety import one_line
from .ingest.voice import (
    TRANSIENT_VOICE_ERRORS,
    VoiceError,
    WhisperFailedError,
    extract_voice,
    sweep_stale_tempdirs,
)
from .ingest.whisper import WhisperSetup, resolve_whisper
from .sensitivity import VALID_SENSITIVITY_LEVELS
from .settings.drop_folders import (
    CONFIG_NAME,
    DEFAULT_EXTENSIONS,
    DEFAULT_KIND,
    DEFAULT_SENSITIVITY,
    VOICE_EXTENSIONS,
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
#: The production whisper resolver (RM-38); tests swap it for a fake the same way.
whisper_resolver: Callable[[Mapping[str, str]], WhisperSetup] = resolve_whisper


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
        typer.echo(f"no drop folders to run (see {GUIDE})")
        return
    cfg = Config.load()  # publishes the .env chain, so the OCR knobs below read it
    ocr = ocr_resolver(os.environ)
    whisper = _whisper_for(folders)
    usable, failed = _check_tools(folders, ocr, whisper)
    if dry_run:
        failed = _dry_run(usable, cutoff) or failed
    elif usable:
        with sigterm_as_system_exit():  # a bootout unwinds, killing whisper (rev-107 F1)
            run_failed = _ingest_all(
                cfg,
                usable,
                cutoff,
                ocr=ocr,
                whisper=whisper if isinstance(whisper, WhisperSetup) else None,
                enrich=enrich,
                allow_secrets=allow_secrets,
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
    return since_or_exit(since, SCOPE_REFUSAL, now=now)


def since_or_exit(since: str | None, refusal: str, *, now: datetime) -> datetime:
    """The cutoff ``--since Nd`` names (1d to 365d); else ``refusal`` and exit 2.

    Shared with ``brain capture import`` (RM-39), which has its own scope refusal.
    """
    match = _SINCE.fullmatch(since or "")
    days = int(match.group(1)) if match else 0
    if not 1 <= days <= MAX_SINCE_DAYS:
        _refuse(f"{refusal}: --since must be 1d to {MAX_SINCE_DAYS}d")
    return now - timedelta(days=days)


def _folders_or_exit(directory: Path | None) -> list[DropFolder]:
    try:
        config = load_drop_folders(_brain_home_root())
    except ConfigError as exc:
        # The settings leaf names the entry and key, never a value.
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1) from exc
    configured = config.folders
    if directory is None:
        # A missing folder (an unplugged volume) is skipped, never fatal to the others
        # (rev-105 F6). Named by index: the path is in the file, and may be a volume name.
        for index in config.missing:
            typer.secho(
                f"skipped folder[{index}] in {CONFIG_NAME}: it does not exist or is not a "
                "directory; the other folders still run",
                fg="yellow",
                err=True,
            )
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


def _whisper_for(folders: list[DropFolder]) -> WhisperSetup | str | None:
    """Whisper for the audio folders: ``None`` when no folder takes audio, the
    ``ConfigError``'s text (it names the variable, never the value) on a bad knob."""
    if not any(f.extensions & VOICE_EXTENSIONS for f in folders):
        return None
    try:
        return whisper_resolver(os.environ)
    except ConfigError as exc:
        return str(exc)


def _check_tools(
    folders: list[DropFolder], ocr: OcrSetup, whisper: WhisperSetup | str | None
) -> tuple[list[DropFolder], bool]:
    """Skip, with the fix, each folder whose files cannot be read. ``(usable, any_skipped)``."""
    ocr_hint = ocr_unavailable_hint(ocr)
    voice_hint = whisper.unavailable if isinstance(whisper, WhisperSetup) else whisper
    usable: list[DropFolder] = []
    for folder in folders:
        hint = None
        if folder.extensions & IMAGE_EXTENSIONS and ocr_hint is not None:
            hint = ocr_hint
        elif folder.extensions & VOICE_EXTENSIONS and voice_hint is not None:
            hint = voice_hint
        if hint is None:
            usable.append(folder)
        else:
            typer.secho(f"skipped {one_line(str(folder.path))}: {hint}", fg="red", err=True)
    return usable, len(usable) < len(folders)


def _dry_run(folders: list[DropFolder], cutoff: datetime, *, verb: str = "ingest") -> bool:
    """List what a run would take; ``True`` when a folder could not be read."""
    until = datetime.now(UTC) - timedelta(seconds=SETTLE_S)
    failed = False
    for folder in folders:
        try:
            found = scan(folder, since=cutoff, until=until)
        except OSError as exc:
            # TCC's EPERM on ~/Desktop, a folder removed since: by class, never a traceback
            # that would print the path and the message (sec-105 L3).
            where = one_line(str(folder.path))
            typer.secho(f"skipped {where}: {describe_failure(exc)}", fg="red", err=True)
            failed = True
            continue
        typer.echo(f"{one_line(str(folder.path))}: {len(found)} file(s)")
        for path in found:
            typer.echo(f"  would {verb}  {one_line(path.name)}")
    return failed


@dataclass(frozen=True)
class _Context:
    cfg: Config
    embedder: Embedder
    enricher: OllamaEnricher | None
    graph_syncer: GraphSyncer
    ocr: OcrSetup
    enrich: bool
    allow_secrets: bool
    whisper: WhisperSetup | None = None  # None: no folder in this run takes audio (RM-38)


def _ingest_all(
    cfg: Config,
    folders: list[DropFolder],
    cutoff: datetime,
    *,
    ocr: OcrSetup,
    whisper: WhisperSetup | None,
    enrich: bool,
    allow_secrets: bool,
) -> bool:
    """Run every folder; ``True`` when any file or folder failed."""
    if whisper is not None:
        sweep_stale_tempdirs(max_seconds=whisper.settings.max_seconds)  # sec-107 F4
    ctx = _Context(
        cfg=cfg,
        embedder=_build_embedder(cfg),
        enricher=_build_enricher(cfg) if enrich else None,
        graph_syncer=_build_graph_syncer(cfg),
        ocr=ocr,
        enrich=enrich,
        allow_secrets=allow_secrets,
        whisper=whisper,
    )
    failed = False
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        for folder in folders:
            try:
                report = run(conn, folder, since=cutoff, ingest=_ingest_step(ctx, folder))
            except (DropFolderError, OSError) as exc:
                where = one_line(str(folder.path))
                typer.secho(f"skipped {where}: {_folder_error(exc)}", fg="red", err=True)
                failed = True
                continue
            _print_report(report)
            failed = failed or report.failed
    return failed


def _folder_error(exc: DropFolderError | OSError) -> str:
    # A DropFolderError message is fixed text; an OSError's could quote a path.
    return str(exc) if isinstance(exc, DropFolderError) else describe_failure(exc)


def _ingest_step(ctx: _Context, folder: DropFolder) -> IngestFn:
    if folder.mode == "capture":  # RM-39: the one path `brain capture import` uses too
        return capture_import.importer(
            capture_import.CaptureContext(
                cfg=ctx.cfg,
                embedder=ctx.embedder,
                sensitivity=folder.sensitivity,
                folder=folder.path,
            )
        )

    def ingest(
        conn: psycopg.Connection[Any], path: Path, *, title: str, timeout: float
    ) -> str:
        try:
            doc = _extract(ctx, path, timeout)
        except OcrUnavailableError:
            raise  # configuration, not this file: it goes back for the next run
        except TRANSIENT_VOICE_ERRORS as exc:
            _retry_or_reject(path, exc)
            raise  # the machine, not this memo (RM-38.D4): it goes back too
        except (ImageError, ValueError) as exc:
            # Unreadable, too large, an OCR timeout, a malformed file: every run would fail.
            raise RejectedFileError("the file cannot be read") from exc
        try:
            result = _ingest(ctx, folder, conn, doc, title)
        except SecretGuardError as exc:
            raise RejectedFileError("the secret guard refused the file") from exc
        if path.suffix.lower() in VOICE_EXTENSIONS:
            _forget_failures(path)  # only once the memo is stored (sec-107b N3)
        _emit_secret_notice(result.secret_notice)
        _emit_egress_notice(result.egress_notice)
        outcome, _ = classify(result)
        return _STATUS[outcome]

    return ingest


def _extract(ctx: _Context, path: Path, timeout: float) -> ExtractedDoc:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return extract_image(path, ocr=ctx.ocr, timeout=timeout)
    if suffix in VOICE_EXTENSIONS:
        # Whisper's own timeout, not the run's remainder (RM-38.D1, module docstring).
        return extract_voice(path, whisper=ctx.whisper)
    return extract_path(path)


def _voice_state_dir() -> Path:
    return _brain_home_root() / "state"


def _retry_or_reject(path: Path, exc: VoiceError) -> None:
    """Warn that ``path`` goes back; reject it instead at whisper's third failure on it."""
    failures = None
    if isinstance(exc, WhisperFailedError):
        try:
            failures = voice_failures.record_failure(_voice_state_dir(), path)
        except OSError as ledger_exc:  # best-effort: the memo still goes back (sec-107b N3)
            _warn_ledger("updated", ledger_exc)
        if failures is not None and failures >= voice_failures.MAX_FAILURES:
            raise RejectedFileError("whisper failed on the memo every time") from exc
    _warn_retry(path, exc, failures)


def _forget_failures(path: Path) -> None:
    """Clear a stored memo's whisper-failure count; a ledger error never fails the ingest."""
    try:
        voice_failures.forget(_voice_state_dir(), path)
    except OSError as exc:
        _warn_ledger("cleared", exc)


def _warn_ledger(verb: str, exc: OSError) -> None:
    typer.secho(
        f"warning: the voice-failure count was not {verb} ({describe_failure(exc)})",
        fg="yellow",
        err=True,
    )


def _warn_retry(path: Path, exc: VoiceError, failures: int | None) -> None:
    """Name the memo (its ``imported/`` name carries its hash) and the class, never more."""
    count = "" if failures is None else f", failure {failures} of {voice_failures.MAX_FAILURES}"
    typer.secho(
        f"warning: {one_line(path.name)} was not transcribed ({describe_failure(exc)}{count}); "
        f"it goes back to the folder and is retried next run (see {GUIDE})",
        fg="yellow",
        err=True,
    )


def _ingest(
    ctx: _Context,
    folder: DropFolder,
    conn: psycopg.Connection[Any],
    doc: ExtractedDoc,
    title: str,
) -> IngestResult:
    return ingest_document(
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


def _print_report(report: DropReport) -> None:
    for outcome in report.outcomes:
        line = f"  {outcome.status:<9} {one_line(outcome.name)}"
        if outcome.detail:
            line += f" ({one_line(outcome.detail)})"
        typer.secho(line, fg="red" if outcome.status == "failed" else None)
    counts = report.counts()
    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "nothing new"
    if report.deferred:
        summary += f"; {report.deferred} left for the next run"
    typer.echo(f"{one_line(str(report.folder))}: {summary}")


def register(app: typer.Typer) -> None:
    app.command(name="ingest-folder")(ingest_folder)
