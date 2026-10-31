"""Ingest jobs: the second of the two modules in ``brain.ui`` permitted to mutate.

Exactly two modules here may mutate (Ingest spec §1.3): ``notes_service.py``
(authored notes) and this one (ingest jobs). Both delegate to functions the CLI
already owns; a route module that writes, or a third mutator, is a review
rejection. This module re-implements nothing of the pipeline: a paste becomes an
:class:`~brain.ingest.ExtractedDoc` via ``ingest.stdin.make_doc``, an upload (RM-13,
§5.2) via ``ingest.dispatch.extract_path`` on its staged file, and both go through
``ingest_document``, the function every ``brain ingest*`` uses. An upload is ingested
with ``source_path=None``, so it dedups on the hash of its extracted text, and its
staged file is deleted by the registry's ``on_settled`` whatever the outcome (the
staging area's ``release``), a queued job that never ran included. An inbox file
(RM-40) goes through ``drop_folder.run_one``: moved into ``<inbox>/imported/`` first and
ingested from there, so it keeps that permanent ``source_path`` and dedups by it. Its
refusal codes and messages are the pure leaf ``inbox_errors``.

The call shape is new and is deliberately NOT ``brain ingest-stdin``'s (§5.1): no
external id (so dedup is by content hash, and an identical paste is a skip), no
``sources`` row, never ``force``, ``graph_syncer=None`` (D9: the vault watcher's
``GraphLane`` reconciles the new mirror), no agent attribution (the UI is a
surface, recorded as ``metadata.ingested_via``), and no ``allow_secrets`` switch.

Work runs on the job registry's worker thread, never in a request handler (D1).
The job builds ITS OWN embedder and enricher once per worker thread (§5.3): the
search path's ``ctx.embedder`` belongs to the event-loop thread.

**Logging** carries the job id, kind, outcome and document id only: never the
title, never the body (§7.5). Failures are logged by the registry, class only.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import psycopg.errors

from ..drop_folder import DropFolder, DropFolderError, FileOutcome, run_one
from ..errors import BrainError, EmbedError, SecretGuardError, describe_failure
from ..ingest import Embedder, ExtractedDoc, IngestResult, ingest_document
from ..ingest.dispatch import extract_path
from ..ingest.outcome import IngestOutcome, classify
from ..ingest.stdin import make_doc
from ..near_dup_store import check_document
from ..sensitivity import SensitivityLevel
from ..tags import normalize_tags
from .errors import UiError, UiForbidden, UiUnavailable
from .inbox import resolve_inbox_file
from .inbox_errors import InboxRefusedError, recheck_refused, refused_open
from .ingest_progress import ProgressEmbedder
from .ingest_schemas import FileSpec, InboxSpec, PasteSpec
from .jobs import JobError, JobHandle, JobRegistry, JobSnapshot
from .upload_checks import upload_cap
from .upload_staging import StagedUpload, UploadStaging

if TYPE_CHECKING:  # pragma: no cover — typing only
    from ..config import Config
    from ..enrichment import OllamaEnricher
    from .context import UiContext

logger = logging.getLogger(__name__)

PASTE_KIND = "ingest.paste"
FILE_KIND = "ingest.file"
INBOX_KIND = "ingest.inbox"

#: The source kind of every UI ingest (Ingest spec Q8: no other kind is offered).
UI_SOURCE_KIND = "manual"

#: The only metadata a paste records (§5.1, "Document metadata, decided").
UI_METADATA: Mapping[str, str] = {"ingested_via": "ui"}

#: Fixed per-code messages (§4.5). Never the exception's text, which can quote
#: document content.
_MESSAGES: Mapping[str, str] = {
    "secret_guard_rejected": (
        "The secret guard is set to reject and found a credential-like string. "
        "Nothing was written."
    ),
    "embedder_busy": "The embedding model did not answer. Nothing was written; retry.",
    "embedder_unavailable": (
        "The embedding model is not usable (not pulled, or a bad key). "
        "Nothing was written."
    ),
    "concurrent_ingest": (
        "Another process ingested the same content at the same moment. "
        "Retrying is safe."
    ),
}


class UnreadableFile(BrainError):
    """``extract_path`` could not read a staged upload, for any reason.

    Raised by the file runner so the mapping below can tell it from a ``ValueError``
    raised anywhere else in the pipeline. It carries the extension only.
    """

    def __init__(self, ext: str) -> None:
        super().__init__(f"could not read the file as {ext}")
        self.ext = ext


class IngestRuntime:
    """Builds the job's embedder and enricher once per worker thread.

    The factories are injected (Dependency Inversion), so a test passes fakes and
    production passes :meth:`from_config`. The cache is thread-local: the event
    loop's ``ctx.embedder`` is never shared with the worker.
    """

    def __init__(
        self,
        *,
        make_embedder: Callable[[], Embedder],
        make_enricher: Callable[[], OllamaEnricher | None],
    ) -> None:
        self._make_embedder = make_embedder
        self._make_enricher = make_enricher
        self._local = threading.local()

    @classmethod
    def from_config(cls, cfg: Config) -> IngestRuntime:
        """The production runtime: the same factories the CLI's builders wrap."""
        from ..embeddings import make_embedder
        from ..enrichment import make_enricher

        return cls(
            make_embedder=lambda: make_embedder(cfg),
            make_enricher=lambda: make_enricher(cfg),
        )

    def embedder(self) -> Embedder:
        if not hasattr(self._local, "embedder"):
            self._local.embedder = self._make_embedder()
        embedder: Embedder = self._local.embedder
        return embedder

    def enricher(self) -> OllamaEnricher | None:
        if not hasattr(self._local, "enricher"):
            self._local.enricher = self._make_enricher()
        enricher: OllamaEnricher | None = self._local.enricher
        return enricher


def ingest_failure(exc: Exception) -> JobError:
    """Map a runner's exception to its fixed envelope (Ingest spec §4.5)."""
    if isinstance(exc, UnreadableFile):
        # The extension is from the allow-list, never the user's filename.
        return JobError(
            "unreadable_file", f"The file could not be read as {exc.ext}. Nothing was written."
        )
    if isinstance(exc, SecretGuardError):
        code = "secret_guard_rejected"
    elif isinstance(exc, EmbedError):
        code = "embedder_busy" if exc.transient else "embedder_unavailable"
    elif isinstance(exc, psycopg.errors.UniqueViolation):
        code = "concurrent_ingest"
    elif isinstance(exc, psycopg.Error):
        # Makes no "nothing was written" claim: a database error can follow the
        # commit (the post-commit summary UPDATE).
        return JobError("database_error", f"Database error ({type(exc).__name__}).")
    else:
        # Never says nothing was written: an error after the commit (for example
        # from the post-commit summary) lands here with the row already saved.
        return JobError(
            "internal_error",
            f"Internal error ({type(exc).__name__}). The document may already have "
            "been saved; re-submit to check (identical content is a no-op).",
        )
    return JobError(code, _MESSAGES[code])


def submit_paste(
    ctx: UiContext, jobs: JobRegistry, runtime: IngestRuntime, spec: PasteSpec
) -> JobSnapshot:
    """Queue a paste job. Raises ``QueueFull`` / ``ShuttingDown`` from the registry."""
    return jobs.submit(PASTE_KIND, paste_runner(ctx, runtime, spec), on_error=ingest_failure)


def paste_runner(
    ctx: UiContext, runtime: IngestRuntime, spec: PasteSpec
) -> Callable[[JobHandle], Mapping[str, Any]]:
    """The runner for one paste. Cancel points 1 and 2 precede the embed (§4.4)."""

    def run(handle: JobHandle) -> Mapping[str, Any]:
        handle.check_cancelled()  # point 1: on pick-up
        handle.progress("preparing")
        doc = make_doc(
            content=spec.body,
            title=spec.title,
            content_type=spec.content_type,
            metadata=dict(UI_METADATA),
        )
        handle.check_cancelled()  # point 2: after validation (no extraction for paste)
        request = _Request(
            kind=PASTE_KIND,
            tags=spec.tags,
            sensitivity=spec.sensitivity,
            enrich=spec.enrich,
            original_filename=None,
        )
        return _ingest(handle, ctx, runtime, doc, request)

    return run


def submit_file(
    ctx: UiContext,
    jobs: JobRegistry,
    runtime: IngestRuntime,
    staging: UploadStaging,
    spec: FileSpec,
) -> JobSnapshot:
    """Claim the staged upload and queue its job (Ingest spec §5.2).

    Raises ``upload_unknown`` (404) from the staging area, and ``QueueFull`` /
    ``ShuttingDown`` from the registry, in which case the claim is undone so the
    user can retry with the same upload. Once queued, the registry's
    ``on_settled`` deletes the staged file whatever the outcome.
    """
    upload = staging.claim(spec.upload_id)
    try:
        return jobs.submit(
            FILE_KIND,
            file_runner(ctx, runtime, spec, upload),
            on_error=ingest_failure,
            on_settled=lambda: staging.release(upload.upload_id),
        )
    except BaseException:
        staging.unclaim(upload.upload_id)
        raise


def file_runner(
    ctx: UiContext, runtime: IngestRuntime, spec: FileSpec, upload: StagedUpload
) -> Callable[[JobHandle], Mapping[str, Any]]:
    """The runner for one staged upload. Cancel points 1–3 precede the embed (§4.4).

    The upload was staged and sniffed before the job existed, so point 2 ("after
    staging") is the same check as point 1. Extraction itself is not interruptible.
    """

    def run(handle: JobHandle) -> Mapping[str, Any]:
        handle.check_cancelled()  # points 1 and 2: on pick-up, after staging
        handle.progress("extracting")
        try:
            extracted = extract_path(upload.path)
        except Exception as exc:
            # Broad on purpose, and ONLY around extraction: a file that passed the
            # sniff can still fail in the parser in ways extract_path does not wrap
            # (lxml's XMLSyntaxError for a malformed document.xml, a KeyError for a
            # zip with no [Content_Types].xml). Nothing has been written yet, so
            # "unreadable, nothing was written" is true; internal_error's "may
            # already have been saved" would not be (rev-77 R1).
            raise UnreadableFile(upload.name.ext) from exc
        handle.check_cancelled()  # point 3: after extraction
        # A NEW document, never the extractor's mutated: the staged path means
        # nothing after the job (and would defeat dedup), so it is dropped.
        doc = replace(
            extracted,
            title=spec.title or extracted.title,
            source_path=None,
            metadata={
                **extracted.metadata,
                **UI_METADATA,
                "original_filename": upload.name.basename,
            },
        )
        request = _Request(
            kind=FILE_KIND,
            tags=spec.tags,
            sensitivity=spec.sensitivity,
            enrich=spec.enrich,
            original_filename=upload.name.basename,
        )
        return _ingest(handle, ctx, runtime, doc, request)

    return run


#: :func:`brain.drop_folder.run_one`'s signature, as :func:`inbox_runner` calls it.
RunOne = Callable[..., FileOutcome]


def inbox_failure(exc: Exception) -> JobError:
    """Map an inbox job's exception (RM-40); the rest are :func:`ingest_failure`'s."""
    if isinstance(exc, InboxRefusedError):
        return JobError(exc.code, exc.message)
    if isinstance(exc, DropFolderError):
        return JobError(
            "inbox_unusable",
            "The inbox's imported/ folder is not a real directory. Nothing was moved.",
        )
    return ingest_failure(exc)


def submit_inbox(
    ctx: UiContext,
    jobs: JobRegistry,
    runtime: IngestRuntime,
    folder: DropFolder,
    spec: InboxSpec,
    identity: tuple[int, int],
) -> JobSnapshot:
    """Queue the job for one inbox file (RM-40). Raises ``QueueFull`` / ``ShuttingDown``.

    ``identity`` is the ``(st_dev, st_ino)`` the submit checked: the job ingests that
    file or nothing.
    """
    runner = inbox_runner(ctx, runtime, folder, spec, identity=identity)
    return jobs.submit(INBOX_KIND, runner, on_error=inbox_failure)


def inbox_runner(
    ctx: UiContext,
    runtime: IngestRuntime,
    folder: DropFolder,
    spec: InboxSpec,
    *,
    identity: tuple[int, int],
    run: RunOne = run_one,
) -> Callable[[JobHandle], Mapping[str, Any]]:
    """The runner for one inbox file: re-validate, then RM-33's move-first step.

    The name is checked again here, on the worker thread, because the file may have
    been swapped since the submit (RM-40 AC2); ``run_one`` then opens it ``O_NOFOLLOW``
    against the folder's fd. Every failure moves the file back to the inbox
    (RM-40.D4): the step never raises ``RejectedFileError``, and the captured
    exception is re-raised here so the job reports it by class. ``empty`` stays in
    ``imported/``, as RM-33 rules. ``run`` is a seam for the outcomes no real file
    produces on demand (a race between the re-check and the open).

    The file must still be the one the submit checked (``identity``), and ``run_one``
    admits the opened fd against that identity and the upload cap, so a file that grew
    or was replaced at any point after the submit is refused unread (review round 1).
    """

    def job(handle: JobHandle) -> Mapping[str, Any]:
        handle.check_cancelled()  # point 1: on pick-up, before anything moves
        handle.progress("preparing")
        try:
            checked = resolve_inbox_file(folder, spec.name)
        except UiError as exc:
            raise recheck_refused(exc.code) from None
        if checked.identity != identity:  # a different file now holds the name
            raise refused_open("FileIdentityError")
        cap = upload_cap(Path(spec.name).suffix.lower())
        step = _InboxStep(handle, ctx, runtime, spec, cap)
        with ctx.connect() as conn:
            try:
                # The cap and the identity are admitted on the OPENED fd, so a file that
                # grew or was swapped after the check is refused unread (review round 1, M1).
                outcome = run(conn, folder, spec.name, ingest=step, max_bytes=cap,
                              expect_identity=identity)
            except OSError as exc:  # the folder itself: gone, or unreadable
                raise InboxRefusedError(
                    "inbox_move_failed",
                    f"The inbox could not be opened ({describe_failure(exc)}). Nothing was moved.",
                ) from None
            if step.error is None and step.ingested is not None:
                return _inbox_payload(conn, handle, ctx, step.ingested)
        if step.error is not None:
            raise step.error  # run_one moved it back, unless its name was retaken (D4)
        if outcome.status == "vanished":
            raise InboxRefusedError(
                "inbox_file_gone", "The file is no longer in the inbox. Nothing was written."
            )
        raise refused_open(outcome.detail)

    return job


class _InboxStep:
    """The :class:`~brain.drop_folder.IngestFn` an inbox job hands ``run_one``.

    It records what the ingest returned or raised, because ``run_one`` reports a
    failure as a class-only string and the job needs the exception to map it. It
    returns as soon as ``ingest_document`` has committed: the payload's reads run
    after ``run_one``, so a failed read can never move a committed file back.
    """

    def __init__(
        self,
        handle: JobHandle,
        ctx: UiContext,
        runtime: IngestRuntime,
        spec: InboxSpec,
        cap: int,
    ) -> None:
        self._handle, self._ctx, self._runtime, self._spec = handle, ctx, runtime, spec
        self._cap = cap
        self.ingested: _Ingested | None = None
        self.error: Exception | None = None

    def __call__(
        self, conn: psycopg.Connection[Any], path: Path, *, title: str, timeout: float
    ) -> str:
        try:
            ingested = self._ingest(conn, path, title)
        except Exception as exc:
            # Kept only to re-raise after run_one, where on_error maps it class-only; never
            # logged (tests/log_privacy_allowlists.py's STORE_ALLOWLIST says why).
            self.error = exc
            raise  # run_one moves the file back
        self.ingested = ingested
        return _DROP_STATUS[classify(ingested.result)[0]]

    def _ingest(self, conn: psycopg.Connection[Any], path: Path, title: str) -> _Ingested:
        handle, spec = self._handle, self._spec
        handle.check_cancelled()  # point 2: after the move
        # The bytes were hashed under the cap; a writer still holding the file open
        # could append before the extractor reads it, so the size is checked once more.
        if path.stat().st_size > self._cap:
            raise refused_open("FileTooLargeError")
        handle.progress("extracting")
        try:
            extracted = extract_path(path)
        except Exception as exc:  # broad, and only around extraction, as file_runner's
            raise UnreadableFile(path.suffix.lower()) from exc
        handle.check_cancelled()  # point 3: after extraction
        # ``source_path`` stays: the imported/ path is the file's permanent home, so
        # dedup is by path, as for every drop folder (RM-40.D7).
        doc = replace(
            extracted,
            title=spec.title or title,
            metadata={**extracted.metadata, **UI_METADATA, "original_filename": spec.name},
        )
        request = _Request(
            kind=INBOX_KIND,
            tags=spec.tags,
            sensitivity=spec.sensitivity,
            enrich=spec.enrich,
            original_filename=spec.name,
        )
        return _write(conn, handle, self._ctx, self._runtime, doc, request)


#: An ingest outcome as ``drop_folder``'s ``IngestFn`` reports it; the CLI's twin is
#: ``cli_ingest_folder._STATUS``, which a ``brain.ui`` module may not import.
_DROP_STATUS: Mapping[IngestOutcome, str] = {
    IngestOutcome.INGESTED: "ingested",
    IngestOutcome.UPDATED: "updated",
    IngestOutcome.MIRROR_REPAIRED: "updated",
    IngestOutcome.SKIPPED: "unchanged",
    IngestOutcome.EMPTY: "empty",
}


@dataclass(frozen=True)
class _Request:
    """What a runner asked for, beyond the document itself."""

    kind: str
    tags: tuple[str, ...]
    sensitivity: SensitivityLevel
    enrich: bool
    original_filename: str | None


def _ingest(
    handle: JobHandle,
    ctx: UiContext,
    runtime: IngestRuntime,
    doc: ExtractedDoc,
    request: _Request,
) -> Mapping[str, Any]:
    """``ingest_document`` with the UI's call shape (§5.1), then the result payload."""
    with ctx.connect() as conn:
        return _ingest_on(conn, handle, ctx, runtime, doc, request)


def _ingest_on(
    conn: psycopg.Connection[Any],
    handle: JobHandle,
    ctx: UiContext,
    runtime: IngestRuntime,
    doc: ExtractedDoc,
    request: _Request,
) -> Mapping[str, Any]:
    """:func:`_ingest` on ``conn``: the write, then the payload read back from it."""
    return _payload(conn, handle, ctx, _write(conn, handle, ctx, runtime, doc, request))


@dataclass(frozen=True)
class _Ingested:
    """What ``ingest_document`` returned, with what the payload needs beside it."""

    doc: ExtractedDoc
    request: _Request
    result: IngestResult
    enricher: OllamaEnricher | None


def _write(
    conn: psycopg.Connection[Any],
    handle: JobHandle,
    ctx: UiContext,
    runtime: IngestRuntime,
    doc: ExtractedDoc,
    request: _Request,
) -> _Ingested:
    """``ingest_document`` with the UI's call shape (§5.1). It commits; nothing after it."""
    embedder = ProgressEmbedder(runtime.embedder(), handle)
    enricher = runtime.enricher() if request.enrich else None
    # NOT enter_commit() here: that would close the cancel window before the
    # embed. The progress embedder calls it after its last batch.
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=doc,
        source_kind=UI_SOURCE_KIND,
        source_external_id=None,
        # Idempotent; the write boundary holds even for a spec built
        # without parse_paste / parse_file.
        tags=normalize_tags(request.tags),
        vault_root=ctx.cfg.vault_path,
        enricher=enricher,
        enrich=request.enrich,
        enrich_min_tokens=ctx.cfg.enrich_min_tokens,
        graph_syncer=None,
        secret_guard=ctx.cfg.secret_guard,
        allow_secrets=False,
        sensitivity=request.sensitivity,
    )
    if result.secret_findings:
        handle.note("secret_findings")
    return _Ingested(doc, request, result, enricher)


def _payload(
    conn: psycopg.Connection[Any], handle: JobHandle, ctx: UiContext, ingested: _Ingested
) -> Mapping[str, Any]:
    """The result payload, read back after the commit, and the job's INFO line."""
    result, request = ingested.result, ingested.request
    payload = _result_payload(ctx, conn, ingested.doc, request, result, ingested.enricher)
    logger.info(
        "ui %s job %s: %s document %s",
        request.kind, handle.job_id, payload["outcome"], result.document_id,
    )
    return payload


def _inbox_payload(
    conn: psycopg.Connection[Any], handle: JobHandle, ctx: UiContext, ingested: _Ingested
) -> Mapping[str, Any]:
    """:func:`_payload`, built AFTER ``run_one`` returned (#110 round 2, rev F1).

    The document is committed and the file is in ``imported/`` for good, so a failed
    read-back is a success with fewer details, never a failure that would claim the
    file went back. The fallback names the level as unknown, so the lens withholds the
    title (fail-closed), and the error is named by class only.
    """
    try:
        return _payload(conn, handle, ctx, ingested)
    except psycopg.Error as exc:
        unavailable = describe_failure(exc)
        logger.warning(
            "ui %s job %s: committed document %s; its details could not be read back (%s)",
            INBOX_KIND, handle.job_id, ingested.result.document_id, unavailable,
        )
        handle.note("details_unavailable")
        outcome, repaired = classify(ingested.result)
        return {
            "outcome": outcome.value,
            "mirror_repaired": repaired,
            "document_id": ingested.result.document_id,
            "title": ingested.doc.title,
            "vault_path": None,
            "original_filename": ingested.request.original_filename,
            "chunk_count": None,
            "sensitivity": None,  # unknown: the lens withholds the naming keys
            "summary_status": None,
            "secret_guard": ctx.cfg.secret_guard,
            "secret_findings": [],
            "egress_notice": ingested.result.egress_notice,
            "graph": _graph_state(ctx),
            "possible_duplicates": [],
            "details_unavailable": unavailable,
        }


@dataclass(frozen=True)
class _StoredDoc:
    title: str
    vault_path: str | None
    summary: str | None
    chunk_count: int
    sensitivity: str


def _read_back(conn: psycopg.Connection[Any], document_id: str) -> _StoredDoc | None:
    row = conn.execute(
        "SELECT d.title, d.vault_path, d.summary,"
        " (SELECT count(*) FROM chunks c WHERE c.document_id = d.id), d.sensitivity"
        " FROM documents d WHERE d.id = %s",
        (document_id,),
    ).fetchone()
    if row is None:
        return None
    return _StoredDoc(
        title=row[0], vault_path=row[1], summary=row[2], chunk_count=row[3], sensitivity=row[4]
    )


def _result_payload(
    ctx: UiContext,
    conn: psycopg.Connection[Any],
    doc: ExtractedDoc,
    request: _Request,
    result: IngestResult,
    enricher: OllamaEnricher | None,
) -> dict[str, Any]:
    """The success payload the job panel renders (Ingest spec §5.4)."""
    outcome, repaired = classify(result)
    stored = _read_back(conn, result.document_id) if result.document_id else None
    return {
        "outcome": outcome.value,
        "mirror_repaired": repaired,
        "document_id": result.document_id,
        "title": stored.title if stored else doc.title,
        "vault_path": stored.vault_path if stored else None,
        # Names the document as surely as its title does (often more), so it is
        # one of the lens's naming keys (routes_jobs._NAMING_KEYS). None for a paste.
        "original_filename": request.original_filename,
        "chunk_count": stored.chunk_count if stored else 0,
        # The STORED level, read back: a dedup skip can land on an existing
        # confidential document whatever this request asked for. The routes' lens
        # (routes_jobs.lensed_result) withholds every naming key on it.
        "sensitivity": stored.sensitivity if stored else request.sensitivity,
        "summary_status": _summary_status(ctx, doc, request, outcome, stored, enricher),
        "secret_guard": ctx.cfg.secret_guard,
        "secret_findings": [
            {
                "kind": f.kind,
                "label": f.label,
                "line": f.line,
                "col_start": f.col_start,
                "col_end": f.col_end,
                "preview": f.preview,  # masked; the matched text never leaves guard.py
            }
            for f in result.secret_findings
        ],
        "egress_notice": result.egress_notice,
        "graph": _graph_state(ctx),
        # RM-19: the near-copies of this document (lensed per entry by the route).
        "possible_duplicates": _possible_duplicates(conn, result.document_id),
    }


def _possible_duplicates(
    conn: psycopg.Connection[Any], document_id: str | None
) -> list[dict[str, Any]]:
    """The document's near-duplicates; a failed check is one class-only WARNING (RM-19 R7).

    Runs after ``ingest_document`` has committed, so it can never refuse or undo an ingest.
    """
    if document_id is None:
        return []
    try:
        return [ref.to_dict() for ref in check_document(conn, document_id)]
    except psycopg.Error as exc:
        logger.warning(
            "ui ingest: near-duplicate check failed (%s); none listed", describe_failure(exc)
        )
        return []


def _summary_status(
    ctx: UiContext,
    doc: ExtractedDoc,
    request: _Request,
    outcome: IngestOutcome,
    stored: _StoredDoc | None,
    enricher: OllamaEnricher | None,
) -> str:
    """``written`` / ``skipped`` (short or disabled) / ``pending`` (backfill owed).

    Read back from ``documents.summary``: ``IngestResult`` does not report
    enrichment, and a post-commit enrichment failure is deliberately non-fatal.
    A dedup skip never enriches, so an existing row without a summary is pending.
    """
    if stored is not None and stored.summary:
        return "written"
    if stored is None or not request.enrich or enricher is None:
        return "skipped"
    fresh = outcome in (IngestOutcome.INGESTED, IngestOutcome.UPDATED)
    if fresh and enricher.count_tokens(doc.content) < ctx.cfg.enrich_min_tokens:
        return "skipped"
    return "pending"


def _graph_state(ctx: UiContext) -> str:
    """D9: the job never syncs the graph; say who will (Ingest spec §5.6)."""
    if not ctx.cfg.graph_enabled:
        return "disabled"
    return "queued" if ctx.watcher_probe.state() == "running" else "not_synced"


def unavailable_reason(ctx: UiContext) -> str | None:
    """Why this server may not ingest, or ``None`` (Ingest spec §6, §7.1).

    One of ``read_only``, ``requires_loopback`` or ``jobs_unavailable``, in that
    order. ``GET /api/ingest/config`` reports it and the tab renders from it.
    """
    if ctx.read_only:
        return "read_only"
    if not ctx.ingest_enabled:
        return "requires_loopback"
    if ctx.jobs is None or ctx.ingest_runtime is None:
        return "jobs_unavailable"
    return None


def require_ingest(ctx: UiContext) -> tuple[JobRegistry, IngestRuntime]:
    """Gate an ingest MUTATION route; return the registry and runtime.

    The loopback check lives in the route, not the middleware, because it is
    about which routes exist on this bind, not about a request's shape (§7.1).
    ``--read-only`` never reaches here: the middleware refuses it first.
    """
    if not ctx.ingest_enabled:
        raise UiForbidden(
            "ingest is available only when brain ui is bound to this machine; "
            "use brain ingest",
            code="ingest_requires_loopback",
        )
    return require_jobs(ctx), require_runtime(ctx)


def require_jobs(ctx: UiContext) -> JobRegistry:
    """The registry, or 503 ``jobs_unavailable``."""
    if ctx.jobs is None:
        raise UiUnavailable("the job runner is not available", code="jobs_unavailable")
    return ctx.jobs


def require_runtime(ctx: UiContext) -> IngestRuntime:
    """The ingest runtime, or 503 ``jobs_unavailable``."""
    if ctx.ingest_runtime is None:
        raise UiUnavailable("the job runner is not available", code="jobs_unavailable")
    return ctx.ingest_runtime


def require_uploads(ctx: UiContext) -> UploadStaging:
    """The staging area, or 503 ``jobs_unavailable`` (it is built with the registry)."""
    if ctx.uploads is None:
        raise UiUnavailable("the job runner is not available", code="jobs_unavailable")
    return ctx.uploads
