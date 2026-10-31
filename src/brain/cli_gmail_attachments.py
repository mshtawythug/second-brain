"""`brain ingest-gmail --with-attachments`: the CLI side of receipt attachments (RM-45).

``cli_ingest.py`` is over the 800-line ceiling, so it gets only wiring; the rules for
the flag live here, and the hostile-input work in :mod:`brain.ingest.gmail_attachments`
and :mod:`brain.ingest.attachment_text`.

**Scope (protocol point 5).** The flag needs ``--label`` or ``--query``
(:func:`refuse_unless_scoped`, exit 2 before any ``gws`` call): a date range or a sender
alone is too broad a pull of files.

**Re-runs (protocol point 3).** With the flag, a stored thread whose fingerprint matches
and whose attachments all ended in a final status is skipped before anything is
downloaded. Otherwise every part with a final stored record is carried
(:mod:`brain.ingest.attachment_carry`): not fetched, not parsed, its stored section
reused, so only new and retryable parts spend the run's cap (sec-117 F2). A flagged run
never writes a body that lost a section it stored before: if a stored section cannot
be trusted and its part cannot be parsed again in this run, the stored body is kept
(``kept thread …``). Without the flag, a stored thread that holds extracted receipt
text is skipped with a hint, because rebuilding it from the mail alone would drop that
text.

**Signals (sec-117 F5).** The run holds a confidential copy of each attachment in a
private directory, so SIGTERM and SIGHUP become ``SystemExit`` inside :meth:`session`
(``capped_process.sigterm_as_system_exit``): the parse child is killed and the directory
removed on the way out.

**Sensitivity (protocol point 6).** With the flag every thread is ``confidential``: a new
one is inserted so, a changed one is escalated by ``ingest_document``'s escalate-only
update, and an unchanged one through :func:`brain.cli_sensitivity_rerun.escalate_stored_sensitivity`
(which re-fences the mirror). Nothing here lowers a tier.

**Privacy.** Nothing here logs. Lines name the thread id and an attachment's position,
never its filename; failures are statuses, never exception text.
"""
from __future__ import annotations

import contextlib
import signal
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import psycopg
import typer

from .cli_sensitivity_rerun import escalate_stored_sensitivity
from .ingest import ExtractedDoc, IngestResult
from .ingest.attachment_carry import carried_parts, stored_records
from .ingest.attachment_text import with_sections
from .ingest.capped_process import sigterm_as_system_exit
from .ingest.gmail import rendered_messages, to_extracted_thread
from .ingest.gmail_attachments import (
    FINAL_STATUSES,
    AttachmentLimits,
    AttachmentRecord,
    AttachmentRun,
    Extract,
    attachment_workdir,
    child_extractor,
    extract_attachments,
    gws_stdout_cap,
    plan_thread,
)
from .ingest.volatile_patch import refresh_volatile, stored_document
from .sensitivity import CONFIDENTIAL, DEFAULT_SENSITIVITY
from .vault.derived_links import real_gws_runner
from .vault.sensitivity_propagate import propagate_sensitivity_to_vault

__all__ = [
    "DRY_RUN_NOTE",
    "WITH_ATTACHMENTS_HELP",
    "AttachmentPass",
    "gws_runner",
    "refuse_unless_scoped",
]

WITH_ATTACHMENTS_HELP = (
    "Also extract PDF and image attachments (10 MiB each, 50 per run) into each thread; "
    "the threads are stored confidential. Needs --label or --query."
)
REFUSAL = (
    "--with-attachments needs --label or --query: an attachment pull over a date range "
    "or a sender alone is too broad"
)
DRY_RUN_NOTE = "--with-attachments: --dry-run fetches no attachments"
#: One ``attachments get`` call's time limit; its process group is killed past it
#: (``real_gws_runner``'s capped read runs through ``capped_process.run_capped``).
GWS_TIMEOUT_SECONDS = 60.0

#: The report's order; ``extracted`` leads and ``failed`` ends it, both always shown.
_REPORT_ORDER = (
    "kept", "empty", "too large", "too many pages", "encrypted", "unsupported", "unreadable",
    "no ocr", "too many",
    "malformed", "truncated", "over cap", "deadline",
)
_SOURCE_KIND = "gmail"
#: The metadata keys this pass owns on a thread document.
_ATTACHMENT_KEYS = ("attachments", "attachments_fingerprint")


def gws_runner(args: list[str]) -> str:
    """The production ``gws`` runner, with this command's timeout and stdout cap."""
    return real_gws_runner(
        args, timeout=GWS_TIMEOUT_SECONDS, max_stdout_bytes=gws_stdout_cap(AttachmentLimits())
    )


def refuse_unless_scoped(with_attachments: bool, *, label: str | None, query: str | None) -> None:
    """Exit 2 when ``--with-attachments`` comes without ``--label`` or ``--query``."""
    if with_attachments and not (label or query):
        typer.secho(REFUSAL, fg="red", err=True)
        raise typer.Exit(code=2)


def _stored_content(conn: psycopg.Connection[Any], document_id: str) -> str:
    row = conn.execute("SELECT content FROM documents WHERE id = %s", (document_id,)).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else ""


def _records(metadata: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    records = metadata.get("attachments")
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, Mapping)]


def _holds_receipt_text(metadata: Mapping[str, Any]) -> bool:
    return any(r.get("status") == "extracted" for r in _records(metadata))


def _unchanged(metadata: Mapping[str, Any], fingerprint: str) -> bool:
    if metadata.get("attachments_fingerprint") != fingerprint:
        return False
    return all(r.get("status") in FINAL_STATUSES for r in _records(metadata))


class AttachmentPass:
    """One ``ingest-gmail`` run's attachment work; a no-op shell without the flag."""

    def __init__(
        self,
        enabled: bool,
        *,
        vault_root: Path,
        limits: AttachmentLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = enabled
        self._vault_root = vault_root
        self._run = AttachmentRun(limits or AttachmentLimits(), clock=clock)
        self._workdir: Path | None = None
        self._extract: Extract | None = None
        #: The last augmented thread: its id, its stored row (or ``None``) and the
        #: attachment metadata, for :meth:`after_ingest` when the body did not change.
        self._pending: tuple[str, tuple[str, Mapping[str, Any]] | None, dict[str, Any]] | None = (
            None
        )

    @property
    def sensitivity(self) -> str:
        """The tier this run writes: ``confidential`` with the flag."""
        return CONFIDENTIAL if self.enabled else DEFAULT_SENSITIVITY

    @contextlib.contextmanager
    def session(self) -> Iterator[None]:
        """Hold the private work directory for the run (nothing without the flag)."""
        if not self.enabled:
            yield
            return
        limits = self._run.limits
        with sigterm_as_system_exit(also=(signal.SIGHUP,)), attachment_workdir() as workdir:
            self._workdir = workdir
            self._extract = child_extractor(limits)
            try:
                yield
            finally:
                self._workdir = None
                self._extract = None

    def dry_run_note(self) -> None:
        """Say that ``--dry-run`` fetches nothing (nothing without the flag)."""
        if self.enabled:
            typer.echo(DRY_RUN_NOTE)

    def augment(
        self, conn: psycopg.Connection[Any], tid: str, messages: list[dict[str, Any]]
    ) -> ExtractedDoc | None:
        """The thread's document with its attachment sections; ``None`` when it is skipped."""
        doc = to_extracted_thread(messages)
        stored = stored_document(conn, _SOURCE_KIND, tid)
        if not self.enabled:
            if stored is not None and _holds_receipt_text(stored[1]):
                typer.echo(
                    f"  skipped thread {tid} (has attachments: re-run with --with-attachments)"
                )
                return None
            return doc
        plan = plan_thread(rendered_messages(messages), doc.content, limits=self._run.limits)
        if stored is not None and _unchanged(stored[1], plan.fingerprint):
            typer.echo(f"  skipped thread {tid} (unchanged)")
            self._escalate(conn, stored[0], tid)
            return None
        if self._workdir is None or self._extract is None:
            raise RuntimeError("AttachmentPass.augment called outside session()")
        self._run.counts["malformed"] += plan.malformed
        self._run.counts["too many"] += plan.dropped
        stored_meta = stored[1] if stored is not None else {}
        carried = (
            carried_parts(_stored_content(conn, stored[0]), stored_meta.get("attachments"))
            if stored is not None
            else {}
        )
        out = extract_attachments(
            plan.candidates, run=self._run, runner=gws_runner, workdir=self._workdir,
            extract=self._extract, carried=carried,
        )
        for position, record in enumerate(out.records, start=1):
            if record.status != "extracted" and record.key not in carried:
                typer.echo(f"  attachment {position} of thread {tid}: {record.status}")
        if stored is not None and self._would_drop_text(stored_meta, out.records):
            typer.echo(
                f"  kept thread {tid} (its stored attachment text could not be rebuilt "
                "this run; re-run to finish)"
            )
            self._escalate(conn, stored[0], tid)
            return None
        metadata = {
            "attachments": [record.as_metadata() for record in out.records],
            "attachments_fingerprint": plan.fingerprint,
        }
        # A draft-only window is not the thread (``ingest_document`` refuses it), so its
        # records are never patched onto the stored row.
        self._pending = None if doc.metadata.get("_is_draft") else (tid, stored, metadata)
        return with_sections(doc, out.sections, metadata)

    @staticmethod
    def _would_drop_text(
        stored_meta: Mapping[str, Any], records: list[AttachmentRecord]
    ) -> bool:
        """Whether a part extracted before, and still in the mail, has no section now."""
        present = {record.key for record in records}
        now_extracted = {record.key for record in records if record.status == "extracted"}
        before = {
            record.key
            for record in stored_records(stored_meta.get("attachments"))
            if record.status == "extracted"
        }
        return bool((before & present) - now_extracted)

    def after_ingest(self, conn: psycopg.Connection[Any], tid: str, result: IngestResult) -> None:
        """For a thread whose body did not change: store its attachment records, escalate it.

        Attachments that gave no text leave the body as it was, so ``ingest_document``
        skips the row and would drop their records; without them every run downloads the
        same files again. They are patched in metadata only (no re-chunk, no embed).
        """
        pending, self._pending = self._pending, None
        if not self.enabled or result.created or result.body_changed:
            return
        if result.document_id is None:
            return
        if pending is not None and pending[0] == tid and pending[1] is not None:
            stored_id, stored_metadata = pending[1]
            if stored_id == result.document_id:
                refresh_volatile(
                    conn,
                    document_id=stored_id,
                    stored=stored_metadata,
                    metadata=pending[2],
                    keys=_ATTACHMENT_KEYS,
                    vault_root=self._vault_root,
                    graph_syncer=None,
                )
        self._escalate(conn, result.document_id, tid)

    def _escalate(self, conn: psycopg.Connection[Any], document_id: str, tid: str) -> None:
        escalate_stored_sensitivity(
            conn,
            document_id,
            level=CONFIDENTIAL,
            explicit=True,
            vault_root=self._vault_root,
            label=f"thread {tid}",
            propagate=propagate_sensitivity_to_vault,
        )

    def report(self) -> None:
        """Print the run's attachment counts, and the OCR hint once (nothing without the flag)."""
        if not self.enabled:
            return
        counts = self._run.counts
        parts = [f"{counts['extracted']} extracted"]
        parts.extend(f"{counts[s]} {s}" for s in _REPORT_ORDER if counts[s])
        parts.append(f"{counts['failed']} failed")
        typer.echo(f"attachments: {', '.join(parts)}")
        if self._run.ocr_hint is not None:
            typer.secho(self._run.ocr_hint, fg="yellow", err=True)
