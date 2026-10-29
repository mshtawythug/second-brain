"""Ingest jobs: the second of the two modules in ``brain.ui`` permitted to mutate.

Exactly two modules here may mutate (Ingest spec §1.3): ``notes_service.py``
(authored notes) and this one (ingest jobs). Both delegate to functions the CLI
already owns; a route module that writes, or a third mutator, is a review
rejection. This module re-implements nothing of the pipeline: a paste becomes an
:class:`~brain.ingest.ExtractedDoc` via ``ingest.stdin.make_doc`` and goes through
``ingest_document``, the function every ``brain ingest*`` uses.

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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import psycopg
import psycopg.errors

from ..errors import EmbedError, SecretGuardError
from ..ingest import Embedder, IngestResult, ingest_document
from ..ingest.outcome import IngestOutcome, classify
from ..ingest.stdin import make_doc
from ..tags import normalize_tags
from .errors import UiForbidden, UiUnavailable
from .ingest_progress import ProgressEmbedder
from .ingest_schemas import PasteSpec
from .jobs import JobError, JobHandle, JobRegistry, JobSnapshot

if TYPE_CHECKING:  # pragma: no cover — typing only
    from ..config import Config
    from ..enrichment import OllamaEnricher
    from .context import UiContext

logger = logging.getLogger(__name__)

PASTE_KIND = "ingest.paste"

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
        embedder = ProgressEmbedder(runtime.embedder(), handle)
        enricher = runtime.enricher() if spec.enrich else None
        handle.check_cancelled()  # point 2: after validation (no extraction for paste)
        # NOT enter_commit() here: that would close the cancel window before the
        # embed. The progress embedder calls it after its last batch.
        with ctx.connect() as conn:
            result = ingest_document(
                conn,
                embedder=embedder,
                doc=doc,
                source_kind=UI_SOURCE_KIND,
                source_external_id=None,
                # Idempotent; the write boundary holds even for a spec built
                # without parse_paste.
                tags=normalize_tags(spec.tags),
                vault_root=ctx.cfg.vault_path,
                enricher=enricher,
                enrich=spec.enrich,
                enrich_min_tokens=ctx.cfg.enrich_min_tokens,
                graph_syncer=None,
                secret_guard=ctx.cfg.secret_guard,
                allow_secrets=False,
                sensitivity=spec.sensitivity,
            )
            if result.secret_findings:
                handle.note("secret_findings")
            payload = _result_payload(ctx, conn, spec, result, enricher)
        logger.info(
            "ui %s job %s: %s document %s",
            PASTE_KIND, handle.job_id, payload["outcome"], result.document_id,
        )
        return payload

    return run


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
    spec: PasteSpec,
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
        "title": stored.title if stored else spec.title,
        "vault_path": stored.vault_path if stored else None,
        "chunk_count": stored.chunk_count if stored else 0,
        # The STORED level, read back: a dedup skip can land on an existing
        # confidential document whatever this paste asked for. The routes' lens
        # (routes_jobs.lensed_result) withholds title and vault_path on it.
        "sensitivity": stored.sensitivity if stored else spec.sensitivity,
        "summary_status": _summary_status(ctx, spec, outcome, stored, enricher),
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
        # Always present so the front end's shape is fixed now; RM-19 fills it.
        "possible_duplicates": [],
    }


def _summary_status(
    ctx: UiContext,
    spec: PasteSpec,
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
    if stored is None or not spec.enrich or enricher is None:
        return "skipped"
    fresh = outcome in (IngestOutcome.INGESTED, IngestOutcome.UPDATED)
    if fresh and enricher.count_tokens(spec.body) < ctx.cfg.enrich_min_tokens:
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
