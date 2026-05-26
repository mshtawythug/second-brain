"""MCP server exposing the second brain's tools over stdio."""
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import yaml
from mcp import McpError
from mcp.server.fastmcp import FastMCP
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData

from .config import Config
from .db import age_extension_available, connect, connect_age
from .embeddings import OllamaEmbedError, make_embedder
from .enrichment import OllamaEnricher, make_enricher
from .errors import (
    BrainError,
    GraphBackendError,
    GraphReconcileError,
    GraphTenantError,
    IdPrefixAmbiguous,
    IdPrefixNotFound,
    IdPrefixNotHex,
    IdPrefixTooShort,
    InteractionError,
    PersonAmbiguous,
    PersonNotFound,
)
from .format import (
    community_record_json,
    entity_summaries_json,
    graph_context_json,
    graph_stats_json,
)
from .graph_rag.sync import GraphSyncer, make_graph_syncer
from .ingest import (
    Embedder,
    apply_tags,
    ingest_document,
    update_document,
)
from .ingest.stdin import make_doc as _stdin_make_doc
from .interactions import (
    InteractionAction,
    InteractionTargetType,
    record_interaction,
)
from .queries import (
    fetch_document,
    iter_all_document_ids,
    list_documents,
    resolve_document_prefix,
    resolve_person_to_keys,
    summary_counts,
)
from .search import hybrid_search

if TYPE_CHECKING:
    from .graph_rag.reconcile import ReconcileConfig
    from .graph_rag.schema import GraphContext
from .vault.frontmatter import (
    dump_frontmatter,
    parse_frontmatter,
    rewrite_tags,
)
from .vault.graph import backlinks_for, orphans, outgoing_links_for
from .vault.slug import slugify
from .vault.sync import sync_one_file
from .vault.templates import render_template

logger = logging.getLogger("brain.mcp")

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# Tag automatically added to every document ingested via ``brain_ingest_stdin``
# so MCP-saved snippets are distinguishable from CLI-ingested ones.
_MCP_AUTO_TAG = "source-mcp"

# Cap on the body argument to ``brain_note_new`` — protects the disk + DB
# from a runaway LLM that submits a multi-megabyte body. 256 KB is well
# above any human-authored note size and safely under FastMCP's transport
# limits. Documented in the tool docstring + spec's Risks section.
_MAX_NOTE_BODY_BYTES = 256 * 1024

# Hex-only id-prefix detector for ``brain_link_proposal`` dst resolution.
# Matches the resolver's threshold + character set so behavior across the
# CLI, sync, and MCP is consistent.
_ID_PREFIX_MIN_LEN = 6
_HEX_ONLY_RE = re.compile(r"^[0-9a-f-]+$")

# Pause between the initial warmup embed and its single bounded retry. Covers
# the cold-boot race where launchd has started the Ollama daemon but the
# embedding model hasn't finished loading yet (observed 2026-05-13 41 s after
# system boot). Kept small so a sustained Ollama outage still falls through to
# the warning path quickly.
_WARMUP_RETRY_DELAY_SECONDS = 1.0


@dataclass
class _State:
    """Shared server state initialized once in :func:`main`.

    Tests build this directly and assign to ``_state`` (via
    ``monkeypatch.setattr``) instead of calling :func:`main`.

    Wave Q2-SUMMARY-WIKI (2026-05-11): added ``enricher`` so the
    ``brain_edit`` MCP tool can refresh ``documents.summary`` on
    body-changing edits — without it the post-ingest hook hits the
    "no enricher supplied" skip and the Q2 wiki lede shows the
    pre-edit summary above the new body. ``None`` is an allowed
    value so tests can opt out of the LLM round-trip; production
    :func:`main` always populates it via :func:`make_enricher`.
    """

    cfg: Config
    embedder: Embedder
    enricher: OllamaEnricher | None = None
    # Wave G1-c: people-aspect graph syncer wired through ``brain_ingest`` /
    # ``brain_edit`` so MCP-driven writes keep the graph in lock-step. ``None``
    # (tests that build ``_State`` directly) skips graph sync; production
    # :func:`main` always populates it via :func:`make_graph_syncer`.
    graph_syncer: GraphSyncer | None = None


_state: _State | None = None


def _get_state() -> _State:
    """Return the initialized server state.

    Raises ``AssertionError`` if called before :func:`main` has run (or before
    a test has explicitly populated ``_state``)."""
    assert _state is not None, "mcp_server not initialized — call main() first"
    return _state


def _mcp_error(code: int, message: str) -> McpError:
    """Construct an :class:`McpError` with ``code`` and ``message``."""
    return McpError(ErrorData(code=code, message=message))


def _wrap_db_error(e: psycopg.Error) -> McpError:
    """Wrap a Postgres failure as an MCP error.

    The user-facing message intentionally omits ``str(e)`` (which can include
    SQL fragments + connection details) and exposes only the exception class
    name. The full exception is logged to stderr so we can still debug.
    """
    logger.error("database error", exc_info=e)
    return _mcp_error(INTERNAL_ERROR, f"database error: {type(e).__name__}")


def _wrap_embed_error(e: OllamaEmbedError) -> McpError:
    """Wrap a Qwen3/Ollama embedding failure as an MCP error.

    Mirrors :func:`_wrap_db_error`: the user-facing message exposes only the
    exception class name; the full exception is logged to stderr.
    """
    logger.error("embedding failed", exc_info=e)
    return _mcp_error(INTERNAL_ERROR, f"embedding failed: {type(e).__name__}")


def _resolve_id(conn: psycopg.Connection[Any], prefix: str) -> str:
    """Resolve a UUID prefix (min 6 chars) to a full document id.

    Thin wrapper around :func:`brain.queries.resolve_document_prefix` that
    maps its plain exceptions to ``McpError`` so the MCP runtime can surface
    the failure to the caller.
    """
    try:
        return resolve_document_prefix(conn, prefix)
    except (
        IdPrefixTooShort,
        IdPrefixNotHex,
        IdPrefixNotFound,
        IdPrefixAmbiguous,
    ) as e:
        raise _mcp_error(INVALID_PARAMS, str(e)) from e


def _assert_within_vault(target: Path, vault_path: Path, *, label: str) -> None:
    """Reject ``target`` if it resolves outside ``vault_path``.

    Mirror of the CLI's :func:`brain.cli._assert_within_vault` — copied
    here rather than imported so the MCP server has no dependency on a
    Typer-based module. The check is the same: resolve both sides through
    symlinks, then assert the target sits under the vault root. Anything
    that escapes the vault (``--folder ../../etc``, ``daily/../../...``)
    raises an :class:`McpError` with ``INVALID_PARAMS``.
    """
    try:
        target.resolve().relative_to(vault_path.resolve())
    except ValueError as e:
        raise _mcp_error(
            INVALID_PARAMS,
            f"{label} must stay within the vault; "
            f"got a path that resolves outside {vault_path}",
        ) from e


def _ensure_template_path(vault_path: Path, name: str) -> Path:
    """Resolve ``<vault>/_templates/<name>.md`` or raise ``McpError``.

    Mirror of the CLI's :func:`brain.cli._ensure_template` — same recovery
    semantics (point the user at ``brain vault init`` when the directory
    is missing) but raises an MCP-flavoured error so the runtime can
    surface it cleanly instead of as a Typer crash.
    """
    templates_dir = vault_path / "_templates"
    if not templates_dir.is_dir():
        raise _mcp_error(
            INVALID_PARAMS,
            f"vault has no _templates/ directory at {templates_dir} — "
            "run `brain vault init` first",
        )
    target = templates_dir / f"{name}.md"
    if not target.is_file():
        raise _mcp_error(
            INVALID_PARAMS, f"template {name!r} not found at {target}"
        )
    return target


mcp_app: FastMCP = FastMCP(name="brain")


def _parse_iso_datetime(value: str, *, field: str) -> datetime:
    """Parse an ISO date/datetime string from an MCP arg or raise INVALID_PARAMS.

    Accepts ``YYYY-MM-DD`` and ``YYYY-MM-DDTHH:MM:SS`` (the CLI takes the
    same two formats — kept in sync per plan D5 / D8 CLI↔MCP parity).
    A bad string surfaces to the MCP caller as ``INVALID_PARAMS`` with
    the underlying error appended for debuggability.
    """
    try:
        return datetime.fromisoformat(value)
    except ValueError as e:
        raise _mcp_error(
            INVALID_PARAMS,
            f"{field} must be an ISO date (YYYY-MM-DD or "
            f"YYYY-MM-DDTHH:MM:SS); got {value!r}: {e}",
        ) from e


@mcp_app.tool()
def brain_search(
    query: str,
    limit: int = 5,
    source: str | None = None,
    tag: str | None = None,
    since_days: int | None = None,
    fts_only: bool = False,
    # — Q1-C metadata filters — names mirror the CLI flags 1:1.
    person: str | None = None,
    after: str | None = None,
    before: str | None = None,
    kind: str | None = None,
    thread: str | None = None,
    draft: bool | None = None,
    has_tag: str | None = None,
    without_tag: str | None = None,
) -> dict[str, Any]:
    """Hybrid search across the second brain.

    Returns a dict with two keys:

    - ``session_id``: a fresh ``uuid.uuid4()`` minted on every call. Pass
      it back via ``brain_show(..., session_id=...)`` to record the open
      as part of the same search session (powers Q1-C interaction logging).
    - ``results``: up to ``limit`` matching documents ranked by RRF over
      FTS + vector cosine similarity. Each entry is shaped
      ``{id, title, source_kind, snippet, score, content_type, tags}``.

    **Breaking shape change in Q1-C:** prior versions returned the
    results list directly; the new top-level dict carries ``session_id``
    alongside ``results``. Update callers that assume a top-level list.

    Filters (all optional, default = no filter):

    - ``source``: source kind (``manual``, ``gmail``, ``krisp``, ...).
    - ``tag`` / ``has_tag``: ``has_tag`` is a strict alias of ``tag``;
      conflicting values raise ``INVALID_PARAMS``.
    - ``without_tag``: exclude docs carrying this tag.
    - ``since_days``: relative-window recency (N days lookback).
    - ``person``: match docs where this person participated. Resolved
      via the directory (same logic as ``brain people``).
    - ``after`` / ``before``: ISO date strings (``YYYY-MM-DD`` or
      ``YYYY-MM-DDTHH:MM:SS``). ``after`` is inclusive; ``before`` is
      exclusive.
    - ``kind``: filter by ``documents.content_type`` (``email``,
      ``email_thread``, ``note``, ``transcript``, ...).
    - ``thread``: filter by Gmail thread id.
    - ``draft``: ``True`` → drafts only; ``False`` → published only;
      ``None`` (default) → both.
    - ``fts_only``: skip the local Ollama embed call (FTS-only mode).
    """
    state = _get_state()
    logger.debug("brain_search: query=%r limit=%d", query, limit)

    if tag is not None and has_tag is not None and tag != has_tag:
        raise _mcp_error(
            INVALID_PARAMS,
            "tag and has_tag both given with different values",
        )
    effective_tag = tag if tag is not None else has_tag

    after_dt = _parse_iso_datetime(after, field="after") if after else None
    before_dt = _parse_iso_datetime(before, field="before") if before else None

    session_id = str(uuid.uuid4())

    try:
        with connect(state.cfg.database_url) as conn:
            person_match = None
            if person is not None:
                try:
                    person_match = resolve_person_to_keys(conn, person)
                except (PersonNotFound, PersonAmbiguous) as e:
                    raise _mcp_error(INVALID_PARAMS, str(e)) from e
            results = hybrid_search(
                conn,
                embedder=state.embedder,
                query=query,
                limit=limit,
                source_kind=source,
                tag=effective_tag,
                since_days=since_days,
                fts_only=fts_only,
                vector_sim_floor=state.cfg.vector_sim_floor,
                recency_halflife_days=state.cfg.recency_halflife_days,
                snippet_context_tokens=state.cfg.snippet_context_tokens,
                person_keys=person_match.keys if person_match else None,
                person_display_name=(
                    person_match.display_name if person_match else None
                ),
                after=after_dt,
                before=before_dt,
                content_type=kind,
                thread_id=thread,
                draft=draft,
                without_tag=without_tag,
            )
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    except OllamaEmbedError as e:
        raise _wrap_embed_error(e) from e
    return {
        "session_id": session_id,
        "results": [
            {
                "id": r.document_id,
                "title": r.title,
                "source_kind": r.source_kind,
                "snippet": r.snippet,
                "score": r.score,
                "content_type": r.content_type,
                "tags": r.tags,
            }
            for r in results
        ],
    }


@mcp_app.tool()
def brain_show(
    id_prefix: str,
    originating_query: str | None = None,
    session_id: str | None = None,
    graph_retrieved: bool = False,
) -> dict[str, Any]:
    """Return the full body and metadata of a single document by id prefix.

    The prefix must be at least 6 hex characters and must uniquely identify
    a document. Raises :class:`McpError` (``INVALID_PARAMS``) if the prefix
    is too short, non-hex, unknown, or ambiguous.

    Q1-C interaction logging: when ``originating_query`` is provided, also
    records an ``opened`` row in the ``interactions`` table (source='mcp',
    query=originating_query, session_id=parsed UUID or NULL). Supplying
    ``session_id`` without ``originating_query`` is rejected — a session
    id alone carries no useful signal. ``session_id`` must be a valid UUID
    string (the one returned from a prior ``brain_search`` *or*
    ``brain_graphrag_*`` call); a malformed value raises ``INVALID_PARAMS``.

    G4-b graph provenance (spec §17d Q2): pass ``graph_retrieved=true`` when
    this open came from a graph surface (``brain_graphrag_search`` /
    ``…_themes`` / ``…_entity``). It stamps ``graph_retrieved=TRUE`` on the
    logged ``opened`` row — a provenance flag only; the row is still a
    document row (``document_id`` set). Default ``false`` preserves the
    pre-G4 behavior. The graph retrieval tools never log at retrieval time;
    only this user-action open does. Interaction logging is best-effort — a
    logging failure is warned and swallowed so the document still returns
    (this supersedes the Q1-C D13 "propagate logging errors" note for the
    locked never-raise discipline). The return shape is unchanged.
    """
    state = _get_state()
    logger.debug(
        "brain_show: id_prefix=%s originating_query?=%s session_id?=%s",
        id_prefix,
        originating_query is not None,
        session_id is not None,
    )

    # D15: session_id without originating_query is rejected outright —
    # there is no useful signal to log.
    if session_id is not None and originating_query is None:
        raise _mcp_error(
            INVALID_PARAMS,
            "session_id requires originating_query (the query that "
            "produced this session). Pass originating_query alongside, "
            "or omit session_id.",
        )

    # Parse session_id eagerly so a bad UUID surfaces as INVALID_PARAMS
    # before we hit the DB. Done outside the connect() block so we don't
    # mask a parse error behind a connection cost.
    session_uuid: uuid.UUID | None = None
    if session_id is not None:
        try:
            session_uuid = uuid.UUID(session_id)
        except ValueError as e:
            raise _mcp_error(
                INVALID_PARAMS,
                f"session_id is not a valid UUID: {session_id!r} ({e})",
            ) from e

    try:
        with connect(state.cfg.database_url) as conn:
            conn.autocommit = True
            doc_id = _resolve_id(conn, id_prefix)
            doc = fetch_document(conn, doc_id)
            # Log the open AFTER the fetch succeeded, gated on
            # originating_query. ``graph_retrieved`` is provenance on the
            # document row (G4-b). Best-effort (never-raise): a logging
            # failure is warned + swallowed so the document still returns —
            # this supersedes the Q1-C D13 propagate-the-error note.
            if originating_query is not None:
                try:
                    record_interaction(
                        conn,
                        document_id=doc_id,
                        action="opened",
                        source="mcp",
                        query=originating_query,
                        session_id=session_uuid,
                        graph_retrieved=graph_retrieved,
                    )
                except (psycopg.Error, InteractionError) as log_exc:
                    logger.warning(
                        "brain_show: interaction logging failed: %s",
                        type(log_exc).__name__,
                    )
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    assert doc is not None  # _resolve_id confirmed the doc exists
    payload: dict[str, Any] = {
        "id": doc.id,
        "title": doc.title,
        "content": doc.content,
        "content_type": doc.content_type,
        "tags": doc.tags,
        "source_path": doc.source_path,
        "ingested_at": doc.ingested_at.isoformat() if doc.ingested_at else None,
        "source_kind": doc.source_kind,
    }
    # Wave Q1-D — additive ``summary`` key (D16). Omitted when NULL so
    # existing consumers that don't expect the key still parse cleanly.
    # Reachable only via brain_show, never brain_search — per the wave plan
    # we explicitly do NOT change the brain_search return shape this wave.
    if doc.summary is not None:
        payload["summary"] = doc.summary
    return payload


@mcp_app.tool()
def brain_list(
    source: str | None = None,
    tag: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List documents in the brain, optionally filtered by source kind and/or tag.

    Returns up to ``limit`` rows ordered most-recently-ingested first. Mirrors
    the JSON output of ``brain list --json``.
    """
    state = _get_state()
    logger.debug("brain_list: source=%s tag=%s limit=%d", source, tag, limit)
    try:
        with connect(state.cfg.database_url) as conn:
            rows = list_documents(conn, source=source, tag=tag, limit=limit)
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    return [
        {
            "id": r.id,
            "title": r.title,
            "content_type": r.content_type,
            "tags": r.tags,
            "source_kind": r.source_kind,
            "ingested_at": r.ingested_at.isoformat() if r.ingested_at else None,
        }
        for r in rows
    ]


@mcp_app.tool()
def brain_status() -> dict[str, Any]:
    """Return summary counts for the brain.

    ``documents`` / ``chunks`` / ``sources`` are total row counts;
    ``last_ingest`` is the most recent ``documents.ingested_at`` (ISO 8601, or
    ``None`` if the brain is empty); ``by_kind`` is a list of
    ``{kind, count}`` pairs.
    """
    state = _get_state()
    logger.debug("brain_status: called")
    try:
        with connect(state.cfg.database_url) as conn:
            counts = summary_counts(conn)
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    return {
        "documents": counts.documents,
        "chunks": counts.chunks,
        "sources": counts.sources,
        "last_ingest": (
            counts.last_ingest.isoformat()
            if counts.last_ingest is not None
            else None
        ),
        "by_kind": [{"kind": k, "count": c} for k, c in counts.by_kind],
    }


@mcp_app.tool()
def brain_ingest_stdin(
    content: str,
    source: str,
    external_id: str,
    title: str,
    content_type: str = "note",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """Save a snippet from the conversation into the brain.

    Same code path as ``brain ingest-stdin`` on the CLI: chunk → embed → store
    with ``content_hash`` dedup and ``(source, external_id)`` source dedup.
    The ``source-mcp`` tag is auto-added to every document ingested through
    this tool so MCP-saved snippets are distinguishable from CLI ingests.
    """
    if not content.strip():
        raise _mcp_error(INVALID_PARAMS, "content is empty")
    state = _get_state()
    user_tags = list(tags or [])
    # Set-union with the auto tag, then back to a list for storage. Sorted so
    # the resulting tag list is deterministic for tests / users.
    merged_tags = sorted({*user_tags, _MCP_AUTO_TAG})
    meta: dict[str, Any] = dict(metadata or {})
    if date:
        meta.setdefault("date", date)
    doc = _stdin_make_doc(
        content=content,
        title=title,
        content_type=content_type,
        metadata=meta,
    )
    logger.debug(
        "brain_ingest_stdin: source=%s external_id=%s title=%s",
        source,
        external_id,
        title,
    )
    try:
        with connect(state.cfg.database_url) as conn:
            conn.autocommit = True
            result = ingest_document(
                conn,
                embedder=state.embedder,
                doc=doc,
                source_kind=source,
                source_external_id=external_id,
                source_metadata=meta,
                tags=merged_tags,
                vault_root=state.cfg.vault_path,
                graph_syncer=state.graph_syncer,
            )
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    except OllamaEmbedError as e:
        raise _wrap_embed_error(e) from e
    return {
        "document_id": result.document_id,
        "created": result.created,
    }


@mcp_app.tool()
def brain_tag(
    id_prefix: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    """Add and/or remove tags on an existing document.

    At least one of ``add`` / ``remove`` must be non-empty. Returns the
    document's full tag list after the mutation. Re-adding an existing tag is
    a no-op.

    File-writeback parity with ``brain tag`` on the CLI: when the document
    has a populated ``vault_path`` and the on-disk mirror exists, the file's
    frontmatter ``tags:`` field is rewritten via :func:`rewrite_tags` so the
    next ``brain vault sync`` does not re-read stale ``tags: []`` from disk
    and overwrite the DB. A populated ``vault_path`` whose mirror is missing
    on disk emits a WARNING (the MCP caller — Claude — can decide whether
    to re-ingest or invoke a future regenerate tool). A NULL ``vault_path``
    is silently DB-only. The MCP tool does not expose a
    ``--regenerate-file`` equivalent — recovery there is reserved for the
    CLI command.
    """
    add = add or []
    remove = remove or []
    if not add and not remove:
        raise _mcp_error(INVALID_PARAMS, "expected add or remove tags")
    state = _get_state()
    logger.debug(
        "brain_tag: id_prefix=%s add=%s remove=%s", id_prefix, add, remove
    )
    try:
        with connect(state.cfg.database_url) as conn:
            conn.autocommit = True
            doc_id = _resolve_id(conn, id_prefix)
            row = conn.execute(
                "SELECT vault_path FROM documents WHERE id = %s", (doc_id,)
            ).fetchone()
            if row is None:
                # _resolve_id just confirmed the row exists; a None here means
                # someone deleted it between the two queries. Surface explicitly
                # so the failure mode survives `python -O`.
                raise BrainError(f"document vanished mid-transaction: {doc_id}")
            vault_path_rel: str | None = row[0]
            tags = apply_tags(conn, doc_id, add=add, remove=remove)
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    if vault_path_rel is not None:
        abs_path = state.cfg.vault_path / vault_path_rel
        if abs_path.exists():
            rewrite_tags(abs_path, tags)
            logger.debug("brain_tag: rewrote tags in %s", abs_path)
        else:
            logger.warning(
                "brain_tag: mirror missing for %s "
                "(expected %s); db updated only",
                doc_id,
                abs_path,
            )
    return {"document_id": doc_id, "tags": tags}


@mcp_app.tool()
def brain_edit(
    id_prefix: str,
    title: str | None = None,
    content_type: str | None = None,
    content: str | None = None,
    metadata: dict[str, Any] | None = None,
    replace_metadata: bool = False,
) -> dict[str, Any]:
    """Update one of: title, content_type, content, or metadata of a document.

    Body changes (``content``) re-chunk + re-embed; field-only changes are a
    single SQL UPDATE. Metadata defaults to a shallow merge — set
    ``replace_metadata=True`` (with a non-None ``metadata``) to swap the JSONB
    blob entirely. Tag mutations live in :func:`brain_tag` for clean
    separation.
    """
    has_field = any(
        v is not None for v in (title, content_type, content, metadata)
    ) or replace_metadata
    if not has_field:
        raise _mcp_error(INVALID_PARAMS, "no edit fields specified")
    if replace_metadata and metadata is None:
        raise _mcp_error(
            INVALID_PARAMS, "replace_metadata requires metadata"
        )
    state = _get_state()
    logger.debug(
        "brain_edit: id_prefix=%s title?=%s ct?=%s content?=%s meta?=%s replace=%s",
        id_prefix,
        title is not None,
        content_type is not None,
        content is not None,
        metadata is not None,
        replace_metadata,
    )
    embedder = state.embedder if content is not None else None
    # Wave Q2-SUMMARY-WIKI smoke gap (Codex finding 1 follow-up,
    # 2026-05-11): wire ``state.enricher`` on body-changing edits so the
    # auto-summary refreshes alongside the new body. Without this the
    # Q2 wiki lede shows the pre-edit summary above a freshly-edited
    # body when the user (or Claude via MCP) edits via ``brain_edit``.
    # Reuses the long-lived enricher built in :func:`main`; no new
    # Ollama probe per request. Ollama failures inside the hook
    # degrade soft — the row keeps its prior summary and
    # ``brain enrich --backfill`` recovers it later.
    enricher = state.enricher if content is not None else None
    try:
        with connect(state.cfg.database_url) as conn:
            conn.autocommit = True
            doc_id = _resolve_id(conn, id_prefix)
            try:
                result = update_document(
                    conn,
                    document_id=doc_id,
                    embedder=embedder,
                    new_title=title,
                    new_content_type=content_type,
                    new_content=content,
                    metadata_patch=metadata,
                    replace_metadata=replace_metadata,
                    vault_root=state.cfg.vault_path,
                    enricher=enricher,
                    graph_syncer=state.graph_syncer,
                )
            except ValueError as e:
                raise _mcp_error(INVALID_PARAMS, str(e)) from e
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    except OllamaEmbedError as e:
        raise _wrap_embed_error(e) from e
    return {
        "document_id": result.document_id,
        "fields_changed": result.fields_changed,
        "rechunked": result.rechunked,
    }


@mcp_app.tool()
def brain_backlinks(id_prefix: str) -> list[dict[str, Any]]:
    """List documents that link TO ``id_prefix`` (a vault or ingested doc).

    Returns one entry per inbound link with the source document's id, title,
    kind, and the literal ``[[link-text]]`` that carried the reference. An
    empty list means the document has no backlinks yet — not an error.
    """
    state = _get_state()
    logger.debug("brain_backlinks: id_prefix=%s", id_prefix)
    try:
        with connect(state.cfg.database_url) as conn:
            doc_id = _resolve_id(conn, id_prefix)
            rows = backlinks_for(conn, doc_id)
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    return [
        {
            "src_document_id": r.src_document_id,
            "src_title": r.src_title,
            "src_kind": r.src_kind,
            "link_text": r.link_text,
            "link_kind": r.link_kind,
        }
        for r in rows
    ]


@mcp_app.tool()
def brain_links(
    id_prefix: str,
    include_unresolved: bool = False,
) -> list[dict[str, Any]]:
    """List documents that ``id_prefix`` links TO.

    With ``include_unresolved=True``, also returns dangling ``[[refs]]``
    from ``unresolved_links`` — each unresolved entry has
    ``dst_document_id=null`` / ``dst_title=null`` / ``dst_kind=null`` and
    ``resolved=false``. Resolved rows always come first.
    """
    state = _get_state()
    logger.debug(
        "brain_links: id_prefix=%s include_unresolved=%s",
        id_prefix,
        include_unresolved,
    )
    try:
        with connect(state.cfg.database_url) as conn:
            doc_id = _resolve_id(conn, id_prefix)
            rows = outgoing_links_for(
                conn, doc_id, include_unresolved=include_unresolved
            )
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    return [
        {
            "dst_document_id": r.dst_document_id,
            "dst_title": r.dst_title,
            "dst_kind": r.dst_kind,
            "link_text": r.link_text,
            "link_kind": r.link_kind,
            "resolved": r.resolved,
        }
        for r in rows
    ]


@mcp_app.tool()
def brain_orphans(vault_only: bool = True) -> list[dict[str, Any]]:
    """List documents with no incoming and no outgoing links.

    Defaults to vault-tier only (``vault_only=True``); pass
    ``vault_only=False`` to include ingested-tier docs (Krisp / Slack /
    Gmail mirrors), which are usually noise — most ingested artifacts
    carry no ``[[refs]]`` yet.
    """
    state = _get_state()
    logger.debug("brain_orphans: vault_only=%s", vault_only)
    try:
        with connect(state.cfg.database_url) as conn:
            rows = orphans(conn, vault_only=vault_only)
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    return [
        {"document_id": n.document_id, "title": n.title, "kind": n.kind}
        for n in rows
    ]


@mcp_app.tool()
def brain_note_new(
    title: str,
    body: str,
    folder: str | None = None,
    tags: list[str] | None = None,
    template: str | None = None,
) -> dict[str, Any]:
    """Create a new vault note (no ``$EDITOR`` — Claude can't drive one).

    ``title`` becomes the frontmatter ``title`` and the slug-based
    filename. ``body`` is the markdown body (no frontmatter — brain
    assembles the frontmatter automatically). ``folder`` is a
    vault-relative subfolder (cannot escape the vault). ``tags``
    populate the frontmatter. ``template`` (default ``"note"``) names a
    file under ``_templates/`` whose body is appended after the
    user-supplied body — pass ``""`` (empty string) to skip the template
    entirely.

    The note is auto-tagged with ``source-mcp`` so MCP-created notes
    are distinguishable from CLI-created ones (mirrors the
    ``brain_ingest_stdin`` contract). User-supplied ``source-mcp`` is
    deduped via set-union.

    Refuses to overwrite an existing path. Bodies larger than 256 KB are
    rejected with ``INVALID_PARAMS``.

    Returns ``{document_id, vault_path, source_path}`` — the synced doc
    id, the relative path inside the vault, and the absolute path on
    disk for callers that need to reference the file later.
    """
    if not title.strip():
        raise _mcp_error(INVALID_PARAMS, "title is empty")
    body_bytes = body.encode("utf-8")
    if len(body_bytes) > _MAX_NOTE_BODY_BYTES:
        raise _mcp_error(
            INVALID_PARAMS,
            f"body is {len(body_bytes)} bytes (max {_MAX_NOTE_BODY_BYTES})",
        )

    state = _get_state()
    vault_path = state.cfg.vault_path
    if not vault_path.is_dir():
        raise _mcp_error(
            INVALID_PARAMS,
            f"vault path does not exist: {vault_path} — run `brain vault init`",
        )

    template_name = "note" if template is None else template
    template_text = ""
    if template_name:
        template_path = _ensure_template_path(vault_path, template_name)
        template_text = template_path.read_text(encoding="utf-8")

    slug = slugify(title)
    folder_part = (folder or "").strip()
    target_relative = (
        Path(folder_part) / f"{slug}.md" if folder_part else Path(f"{slug}.md")
    )
    target = vault_path / target_relative
    _assert_within_vault(target, vault_path, label="folder")
    if target.exists():
        raise _mcp_error(
            INVALID_PARAMS,
            f"note already exists at {target_relative.as_posix()}",
        )

    user_tags = list(tags or [])
    merged_tags = sorted({*user_tags, _MCP_AUTO_TAG})
    now = datetime.now()
    iso_now = now.isoformat(timespec="seconds")
    document_id = str(uuid.uuid4())

    file_text = _build_note_file_text(
        body=body,
        template_text=template_text,
        title=title,
        document_id=document_id,
        tags=merged_tags,
        iso_now=iso_now,
        date_iso=now.date().isoformat(),
        slug=slug,
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(file_text, encoding="utf-8")

    logger.debug(
        "brain_note_new: title=%r path=%s id=%s",
        title,
        target_relative.as_posix(),
        document_id[:8],
    )
    try:
        with connect(state.cfg.database_url) as conn:
            conn.autocommit = True
            report = sync_one_file(
                conn,
                embedder=state.embedder,
                vault_path=vault_path,
                file_path=target,
            )
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    except OllamaEmbedError as e:
        raise _wrap_embed_error(e) from e
    if report.errors:
        # The on-disk file is intact; surface the first error so the
        # caller knows the index didn't update. Subsequent
        # ``brain vault sync`` will pick it up once the user fixes the
        # underlying issue.
        _path, reason = report.errors[0]
        raise _mcp_error(INTERNAL_ERROR, f"sync failed: {reason}")

    return {
        "document_id": document_id,
        "vault_path": target_relative.as_posix(),
        "source_path": str(target),
    }


@mcp_app.tool()
def brain_daily(date: str | None = None) -> dict[str, Any]:
    """Resolve or create the daily note for ``date`` (default: today).

    ``date`` accepts ISO 8601 (``YYYY-MM-DD``); ``None`` falls back to
    today's local date. The path is
    ``<vault>/daily/<YYYY>/<YYYY-MM-DD>.md``. Idempotent: if the file
    already exists, returns the existing ``document_id`` with
    ``created=false``; otherwise renders ``_templates/daily.md``, writes
    the file (auto-tagged with ``source-mcp``), syncs, and returns
    ``created=true``.
    """
    if date is not None:
        try:
            target_date = date_cls.fromisoformat(date)
        except ValueError as e:
            raise _mcp_error(
                INVALID_PARAMS, f"date must be YYYY-MM-DD ({e})"
            ) from e
    else:
        target_date = date_cls.today()

    state = _get_state()
    vault_path = state.cfg.vault_path
    if not vault_path.is_dir():
        raise _mcp_error(
            INVALID_PARAMS,
            f"vault path does not exist: {vault_path} — run `brain vault init`",
        )

    iso_date = target_date.isoformat()
    year_folder = f"{target_date.year:04d}"
    target_relative = Path("daily") / year_folder / f"{iso_date}.md"
    target = vault_path / target_relative
    _assert_within_vault(target, vault_path, label="date")

    if target.is_file():
        # Recover the existing document_id from the file's frontmatter so
        # callers can chain ``brain_show`` / ``brain_link_proposal``
        # without a second round-trip.
        try:
            existing_text = target.read_text(encoding="utf-8")
        except OSError as e:
            raise _mcp_error(
                INTERNAL_ERROR, f"could not read daily note: {e}"
            ) from e
        try:
            fields, _ = parse_frontmatter(existing_text)
        except (ValueError, yaml.YAMLError) as e:
            raise _mcp_error(
                INTERNAL_ERROR, f"daily note has malformed frontmatter: {e}"
            ) from e
        existing_id = fields.get("id")
        if not isinstance(existing_id, str) or not existing_id:
            # File on disk but no id — extremely unusual; surface clearly.
            raise _mcp_error(
                INTERNAL_ERROR,
                "daily note exists but has no id in frontmatter — "
                "run `brain vault sync` to repair",
            )
        return {
            "document_id": existing_id,
            "vault_path": target_relative.as_posix(),
            "source_path": str(target),
            "created": False,
        }

    template_path = _ensure_template_path(vault_path, "daily")
    template_text = template_path.read_text(encoding="utf-8")

    now = datetime.now()
    iso_now = now.isoformat(timespec="seconds")
    document_id = str(uuid.uuid4())
    file_text = _build_note_file_text(
        body="",
        template_text=template_text,
        title=iso_date,
        document_id=document_id,
        tags=[_MCP_AUTO_TAG],
        iso_now=iso_now,
        date_iso=iso_date,
        slug=iso_date,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(file_text, encoding="utf-8")

    logger.debug(
        "brain_daily: date=%s path=%s id=%s",
        iso_date,
        target_relative.as_posix(),
        document_id[:8],
    )
    try:
        with connect(state.cfg.database_url) as conn:
            conn.autocommit = True
            report = sync_one_file(
                conn,
                embedder=state.embedder,
                vault_path=vault_path,
                file_path=target,
            )
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    except OllamaEmbedError as e:
        raise _wrap_embed_error(e) from e
    if report.errors:
        _path, reason = report.errors[0]
        raise _mcp_error(INTERNAL_ERROR, f"sync failed: {reason}")

    return {
        "document_id": document_id,
        "vault_path": target_relative.as_posix(),
        "source_path": str(target),
        "created": True,
    }


@mcp_app.tool()
def brain_link_proposal(
    src_id_prefix: str,
    dst_id_or_title: str,
) -> dict[str, Any]:
    """Propose adding a ``[[link]]`` from ``src`` to ``dst``. Writes nothing.

    Resolves both endpoints, builds the line that would append the link
    to the source's body, and returns the diff data — the source's vault
    path, the line number where the link would land, the proposed text,
    and the link surface text. The on-disk file is not modified.

    ``src`` must be a vault-tier document (proposals only make sense for
    user-authored notes). ``dst`` is resolved as an id prefix when it
    looks like one (6+ hex chars), otherwise via case-insensitive title
    match. Ambiguous title lookups return ``INVALID_PARAMS`` with the
    candidate ids so the caller (or user) can disambiguate.
    """
    if not src_id_prefix.strip():
        raise _mcp_error(INVALID_PARAMS, "src_id_prefix is empty")
    if not dst_id_or_title.strip():
        raise _mcp_error(INVALID_PARAMS, "dst_id_or_title is empty")

    state = _get_state()
    logger.debug(
        "brain_link_proposal: src=%s dst=%r",
        src_id_prefix,
        dst_id_or_title,
    )
    try:
        with connect(state.cfg.database_url) as conn:
            src_id = _resolve_id(conn, src_id_prefix)
            src_row = conn.execute(
                "SELECT kind, title, vault_path FROM documents WHERE id = %s",
                (src_id,),
            ).fetchone()
            assert src_row is not None  # _resolve_id confirmed existence
            src_kind, _src_title, src_vault_path = src_row
            if src_kind != "vault":
                raise _mcp_error(
                    INVALID_PARAMS,
                    f"src must be a vault-tier document (got kind={src_kind!r})",
                )
            if not isinstance(src_vault_path, str) or not src_vault_path:
                raise _mcp_error(
                    INVALID_PARAMS,
                    "src has no vault_path on disk — cannot propose a link",
                )

            dst_id, dst_title, link_text = _resolve_proposal_dst(
                conn, dst_id_or_title
            )
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e

    abs_src = state.cfg.vault_path / src_vault_path
    if not abs_src.is_file():
        raise _mcp_error(
            INTERNAL_ERROR,
            f"src vault file is missing on disk: {src_vault_path}",
        )
    try:
        text = abs_src.read_text(encoding="utf-8")
    except OSError as e:
        raise _mcp_error(
            INTERNAL_ERROR, f"could not read src vault file: {e}"
        ) from e
    try:
        _, body = parse_frontmatter(text)
    except (ValueError, yaml.YAMLError) as e:
        raise _mcp_error(
            INTERNAL_ERROR, f"src has malformed frontmatter: {e}"
        ) from e

    # The proposed snippet appends a "See also" line at the end of the
    # body — the simplest correct place to land a new link without
    # disturbing existing text. Documented in the tool's docstring.
    needs_blank_line = bool(body) and not body.endswith("\n\n")
    separator = "\n" if not body.endswith("\n") else ""
    if needs_blank_line:
        separator += "\n"
    proposed_text = f"{separator}See also [[{link_text}]].\n"
    # Line number of the appended snippet's first content line: the
    # number of body lines before the append, plus the separator's
    # blank-line count + 1. We count lines in the existing body
    # (treating an empty body as 0 lines) and add the count of newlines
    # introduced before the new "See also" line.
    body_line_count = body.count("\n") + (
        1 if body and not body.endswith("\n") else 0
    )
    blank_lines_inserted = separator.count("\n")
    line_no = body_line_count + blank_lines_inserted + 1

    return {
        "src_vault_path": src_vault_path,
        "src_document_id": src_id,
        "dst_document_id": dst_id,
        "dst_title": dst_title,
        "line_no": line_no,
        "proposed_text": proposed_text,
        "link_text": link_text,
    }


# ---------------------------------------------------------------------------
# Wave G2-i — GraphRAG retrieval surfaces (MCP parity with the G2-h CLI).
# Full CLI↔MCP parity (spec §9): every `brain graphrag …` capability/param/
# semantic is reachable here and behaves identically — same `graph_rag_search`
# core, same router, same degradation signalling on the returned JSON, same
# tenant scoping. Structured params only; the backend injects tenant_id + caps;
# NEVER raw Cypher (spec §4 D9). The wire shape REUSES
# :func:`brain.format.graph_context_json` (the exact `--json` CLI shape, which
# already carries ``session_id`` like ``brain_search``).
# ---------------------------------------------------------------------------


def _wrap_graph_backend_error(e: GraphBackendError) -> McpError:
    """Wrap an Apache AGE backend failure as an MCP error.

    Mirrors :func:`_wrap_db_error`: the user-facing message exposes only the
    exception class name (a :class:`GraphBackendError` can wrap a generated
    Cypher / catalog statement, which must NEVER reach the wire — spec §4 D9
    no-raw-Cypher), and the full exception is logged to stderr for debugging.
    """
    logger.error("graph backend error", exc_info=e)
    return _mcp_error(INTERNAL_ERROR, f"graph backend error: {type(e).__name__}")


def _require_age_or_mcp_error(conn: psycopg.Connection[Any]) -> None:
    """Raise ``McpError`` when this DB image lacks Apache AGE.

    The MCP analogue of the CLI's :func:`brain.cli._require_age_or_exit`: the
    graphrag tools exist solely to query / maintain the AGE graph, so an
    AGE-absent image is an unrecoverable server-side condition (the caller
    cannot fix the DB image), surfaced as ``INTERNAL_ERROR`` — matching how the
    other tools surface unavailable subsystems (DB / embedder failures).
    """
    if not age_extension_available(conn):
        raise _mcp_error(
            INTERNAL_ERROR,
            "graphrag: Apache AGE is not available in this database image — "
            "cut over to the AGE image and run `brain init` first",
        )


def _graphrag_reconcile_config(
    cfg: Config, tenant: str | None
) -> "ReconcileConfig":
    """Resolve the shared :class:`ReconcileConfig`, applying a ``tenant`` override.

    The MCP twin of the CLI's :func:`brain.cli._graphrag_config`: starts from the
    single :func:`brain.graph_rag.sync.build_reconcile_config` (so a build uses
    the SAME co-occurrence window / per-doc cap / generic ratio / owner keys as
    the incremental sync hook) and overrides only the tenant id when a non-blank
    ``tenant`` is supplied — leaving every weighting knob identical so an MCP
    build cannot diverge from the CLI / incremental path.
    """
    from .graph_rag.sync import build_reconcile_config

    base = build_reconcile_config(cfg)
    if tenant is not None and tenant.strip():
        return replace(base, tenant_id=tenant.strip())
    return base


def _graphrag_search_or_mcp_error(
    cfg: Config,
    query: str,
    *,
    mode: str,
    tenant: str | None,
    person: str | None,
    depth: int | None,
    limit: int | None,
    synthesize: bool,
    enricher: OllamaEnricher | None,
    embedder: Embedder | None = None,
) -> "GraphContext":
    """Open an AGE connection, run :func:`graph_rag_search`, map core errors.

    The single construction + error-mapping seam shared by the graphrag
    retrieval tools (the MCP twin of the CLI's
    :func:`brain.cli._graphrag_search_or_exit`): opens an AGE-capable autocommit
    connection, bootstraps the backend, and runs the SAME ``graph_rag_search``
    core the CLI calls — so identical inputs yield an identical
    :class:`GraphContext` (the parity guarantee). Local seed resolution + the
    snippet path are FTS-only, so an embedder is needed ONLY for the ``global``
    (community) path's vector leg AND the ``fuse`` hybrid leg (spec §17c Q9;
    perf-T4 G5): the caller passes the long-lived ``state.embedder`` instance
    directly (no per-call construction); local / themes / entity ignore it.
    The enricher is the opt-in ``synthesize`` group-summary seam.

    Error → ``McpError`` mapping (spec §17b decision 4 + repo error contract;
    mirrors the CLI's exit-code mapping; the G3-e flip means explicit
    ``mode='global'`` now EXECUTES so the former ``GraphModeUnavailable`` reject
    is gone — §17c Q6):

    * :class:`PersonNotFound` / :class:`PersonAmbiguous` (themes resolver) →
      ``INVALID_PARAMS`` (caller-fixable — pick / disambiguate a real person).
    * ``ValueError`` (themes mode with no resolvable person, or an unknown mode
      surfaced by the router) → ``INVALID_PARAMS`` (usage error).
    * :class:`GraphTenantError` / :class:`GraphBackendError` → ``INTERNAL_ERROR``
      (the code the other tools use for unavailable subsystems; the backend
      error's message is class-name-only so no Cypher reaches the wire).
    * :class:`psycopg.Error` → ``INTERNAL_ERROR`` (class-name only, via
      :func:`_wrap_db_error`).

    AGE-absent is handled before retrieval by :func:`_require_age_or_mcp_error`.
    """
    from .graph_rag import graph_rag_search
    from .graph_rag.backends import AgeBackend

    try:
        with connect_age(cfg.database_url) as conn:
            conn.autocommit = True
            _require_age_or_mcp_error(conn)
            backend = AgeBackend()
            backend.bootstrap(conn)
            return graph_rag_search(
                conn,
                cfg,
                query,
                backend=backend,
                tenant=tenant,
                depth=depth,
                limit=limit,
                mode=mode,
                person=person,
                synthesize=synthesize,
                enricher=enricher,
                embedder=embedder,
            )
    except (PersonNotFound, PersonAmbiguous) as exc:
        raise _mcp_error(INVALID_PARAMS, str(exc)) from exc
    except GraphTenantError as exc:
        # Empty effective tenant — a degenerate config bug, not caller-fixable
        # via params; surface as INTERNAL_ERROR (no Cypher in the message).
        raise _mcp_error(INTERNAL_ERROR, str(exc)) from exc
    except GraphBackendError as exc:
        raise _wrap_graph_backend_error(exc) from exc
    except ValueError as exc:
        # Router caller-bug surface: themes mode with no resolvable person, or an
        # unrecognized mode value. Both are usage errors → INVALID_PARAMS.
        raise _mcp_error(INVALID_PARAMS, str(exc)) from exc
    except psycopg.Error as exc:
        raise _wrap_db_error(exc) from exc


@mcp_app.tool()
def brain_graphrag_search(
    query: str,
    mode: str = "auto",
    person: str | None = None,
    depth: int | None = None,
    limit: int | None = None,
    tenant: str | None = None,
    synthesize: bool = False,
) -> dict[str, Any]:
    """Graph retrieval — THEMES, PATTERNS, and CONNECTIONS across interactions.

    WHEN TO USE (graph vs. plain ``brain_search``): reach for this when the
    question is about how things RELATE — themes that keep coming up, patterns
    across conversations, what connects two people/topics, how thinking on a
    subject evolved, the bigger picture, "map out / cluster …". Use plain
    ``brain_search`` instead for a flat "find docs about X", a quote, or a
    single fact from one document. Rule of thumb: *flat answer about content →*
    ``brain_search``; *relationships / themes / clustering →* this tool. When a
    person is named and the ask is thematic, ``mode='themes'`` (or
    ``brain_graphrag_themes``) is the headline move.

    Pick a ``mode`` (the five retrieval strategies):

    - ``auto`` *(default router)* — heuristic: a thematic query WITH a
      resolvable person → themes; thematic WITHOUT a person → global; otherwise
      → local. Let it choose when intent is fuzzy.
    - ``local`` — entity-centric: resolve the seed entity, traverse its bounded
      ``CO_OCCURS`` neighbourhood, return the seed + reached entities and their
      docs. "What connects to X." Same core as ``brain_graphrag_entity``.
    - ``themes`` — *the headline* — "themes in my conversations with X".
      Requires ``person``. Groups the person's co-occurrence subgraph into
      ranked theme groups. Same core as ``brain_graphrag_themes``.
    - ``global`` — community-level RRF over the detected clusters (FTS over
      community summaries ⊕ vector over summary embeddings). Best for "overall
      themes in my brain"; build the communities first via
      ``brain_graphrag_communities_build``.
    - ``fuse`` — RRF-merge the local-graph doc leg with the vector/FTS hybrid
      leg into one ranked doc list. Explicit-only (``auto`` never routes here);
      default tenant only.

    Full parity with ``brain graphrag search``. Returns the
    :func:`brain.format.graph_context_json` wire shape (identical to the CLI's
    ``--json`` output), which carries a fresh ``session_id`` (like
    ``brain_search``) plus ``mode`` / ``query`` / ``tenant_id`` / ``person``,
    the degradation signals (``requested_mode`` / ``degraded_from`` /
    ``degradation_reason``), the ranked ``themes`` (themes mode) and
    ``entities`` (local mode), the document hits (``docs``), and the ranking
    ``explanation``. In ``global`` mode the JSON carries a top-level
    ``communities`` key; in ``fuse`` mode per-doc leg provenance rides in
    ``explanation.matched_filters.fuse_doc_provenance``. Raw Cypher is never
    accepted or returned — the backend injects the tenant + caps automatically.

    Params (all but ``query`` optional; mirror the CLI flags 1:1):

    - ``mode``: one of the five above (default ``auto``). Under ``auto`` a
      thematic query with no resolvable person routes to ``global`` (the G3-e
      flip, spec §17c Q6 — no longer a global→local degradation); an explicit
      ``global`` EXECUTES (spec §6c). ``fuse`` is explicit-only (wave G4-c,
      spec §17d Q1).
    - ``person``: scope themes to this person (resolved via the directory). An
      unknown / ambiguous person raises ``INVALID_PARAMS``.
    - ``depth``: traversal depth (default ``BRAIN_GRAPH_DEPTH``).
    - ``limit``: max documents returned (default 10).
    - ``tenant``: tenant to query (default ``BRAIN_GRAPH_TENANT``); the backend
      scopes every query to it — no cross-tenant leak.
    - ``synthesize``: attach a best-effort local-Ollama summary to each theme
      group (opt-in; never required for retrieval — a missing/failed Ollama
      yields ``summary=None``).
    """
    state = _get_state()
    logger.debug(
        "brain_graphrag_search: query=%r mode=%s person?=%s",
        query,
        mode,
        person is not None,
    )
    ctx = _graphrag_search_or_mcp_error(
        state.cfg,
        query,
        mode=mode,
        tenant=tenant,
        person=person,
        depth=depth,
        limit=limit,
        synthesize=synthesize,
        enricher=state.enricher if synthesize else None,
        # The global (community) path's vector leg embeds the query via this
        # embedder (spec §17c Q9 — local/themes never use it). Passes the
        # long-lived server embedder built in :func:`main` directly — no
        # per-call construction (perf-T4 G5).
        embedder=state.embedder,
    )
    return graph_context_json(ctx)


@mcp_app.tool()
def brain_graphrag_themes(
    person: str,
    depth: int | None = None,
    limit: int | None = None,
    tenant: str | None = None,
    synthesize: bool = False,
) -> dict[str, Any]:
    """THE HEADLINE — "themes in my conversations with X" (spec §6b).

    WHEN TO USE: the go-to graph tool whenever the ask is thematic AND names a
    person — "what themes keep coming up with X", "what do X and I talk about",
    "patterns in my conversations with X". This is the most common graph route;
    prefer it over ``brain_graphrag_search`` when a person is explicit. For a
    thematic ask with NO person, use ``brain_graphrag_search(mode='global')``;
    for one entity's neighbourhood, ``brain_graphrag_entity``; for a flat doc
    lookup, plain ``brain_search``.

    Full parity with ``brain graphrag themes`` — a convenience wrapper for
    ``brain_graphrag_search(mode='themes', person=X)``: scopes to ``person``,
    groups their co-occurrence subgraph, and returns ranked theme groups (key
    entities + representative documents) in the
    :func:`brain.format.graph_context_json` wire shape. ``person`` is REQUIRED;
    an empty / whitespace-only value raises ``INVALID_PARAMS`` (an unknown or
    ambiguous person likewise). ``synthesize`` attaches a best-effort per-group
    local-Ollama summary (opt-in; a missing/failed Ollama yields
    ``summary=None``).
    """
    if not person.strip():
        raise _mcp_error(
            INVALID_PARAMS,
            "person is required for themes mode (the X to scope to)",
        )
    state = _get_state()
    logger.debug("brain_graphrag_themes: person=%r", person)
    ctx = _graphrag_search_or_mcp_error(
        state.cfg,
        "",
        mode="themes",
        tenant=tenant,
        person=person,
        depth=depth,
        limit=limit,
        synthesize=synthesize,
        enricher=state.enricher if synthesize else None,
    )
    return graph_context_json(ctx)


@mcp_app.tool()
def brain_graphrag_entity(
    name: str,
    depth: int | None = None,
    limit: int | None = None,
    tenant: str | None = None,
) -> dict[str, Any]:
    """One entity's neighbourhood — "what connects to X" (spec §9).

    WHEN TO USE: the ask centres on a SINGLE named entity (a person, project,
    org, tool, or topic) and you want what it links to — "who/what is related
    to X", "show me everything around X", "X's neighbourhood". For thematic
    asks scoped to a person use ``brain_graphrag_themes``; for brain-wide
    clusters use ``brain_graphrag_search(mode='global')``; to merely ENUMERATE
    entities (not traverse one) use ``brain_graphrag_entities``.

    Full parity with ``brain graphrag entity``: a thin wrapper over local
    (entity-centric) retrieval seeded on ``name`` — it reuses the SAME path as
    ``brain_graphrag_search(mode='local')``, resolving the entity, traversing
    its bounded ``CO_OCCURS`` neighbourhood, and returning the seed + reached
    entities and their documents in the
    :func:`brain.format.graph_context_json` wire shape. ``name`` is REQUIRED; an
    empty / whitespace-only value raises ``INVALID_PARAMS``.
    """
    if not name.strip():
        raise _mcp_error(INVALID_PARAMS, "name is required")
    state = _get_state()
    logger.debug("brain_graphrag_entity: name=%r", name)
    ctx = _graphrag_search_or_mcp_error(
        state.cfg,
        name,
        mode="local",
        tenant=tenant,
        person=None,
        depth=depth,
        limit=limit,
        synthesize=False,
        enricher=None,
    )
    return graph_context_json(ctx)


@mcp_app.tool()
def brain_graphrag_build(
    tenant: str | None = None,
    concepts: bool = False,
    backfill: bool = False,
    force: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """ADMIN/SETUP — bulk-build the entity graph from all documents (spec §9).

    WHEN TO USE: a slow, write-side maintenance op — NOT everyday querying.
    Reach for it only to bootstrap the graph the first time, or to rebuild
    after ``brain doctor`` reports drift / a missing graph. Once built, query
    with ``brain_graphrag_search`` / ``_themes`` / ``_entity``. After a
    corpus-wide weighting change that needs no re-resolve, prefer the lighter
    ``brain_graphrag_refresh``; to (re)detect clusters for ``mode='global'``
    use ``brain_graphrag_communities_build``.

    Full parity with ``brain graphrag build``. Walks every document in id order
    and reconciles its people aspect (and, when ``concepts`` or
    ``BRAIN_GRAPH_CONCEPTS`` is on, its concept aspect) into the Apache AGE
    graph, sharing the SAME :class:`ReconcileConfig` as the incremental sync
    path. Idempotent + resumable via the per-aspect watermark.

    Params (mirror the CLI flags):

    - ``backfill``: reconcile the people aspect of EVERY existing document.
      Required (pass ``backfill`` or ``force``) — calling with neither raises
      ``INVALID_PARAMS`` (the MCP equivalent of the CLI's "pass --backfill"
      hint).
    - ``force``: authoritative full rebuild — bypass the watermark and
      re-reconcile every document from the relational source-of-truth (recovery
      for a dropped / corrupted AGE mirror). Implies ``backfill`` and is
      incompatible with ``limit`` (rejected with ``INVALID_PARAMS``).
    - ``concepts``: also (re)build the concept aspect via the local Ollama
      extractor (explicit per-run opt-in, independent of ``BRAIN_GRAPH_CONCEPTS``).
    - ``tenant``: tenant to build (default ``BRAIN_GRAPH_TENANT``).
    - ``limit``: max documents to reconcile (default all).

    Returns the :class:`~brain.graph_rag.build.BuildResult` tally as a dict:
    ``{processed, reconciled, skipped, orphans_removed, tenant_id, concepts}``.
    """
    if force and limit is not None:
        raise _mcp_error(
            INVALID_PARAMS,
            "force rebuilds the full corpus and cannot be combined with limit",
        )
    if not (backfill or force):
        raise _mcp_error(
            INVALID_PARAMS,
            "pass backfill=true to reconcile all existing documents into the "
            "graph (or force=true for an authoritative full rebuild that ignores "
            "the watermark). Add concepts=true to also build the concept aspect.",
        )
    state = _get_state()
    cfg = state.cfg
    logger.debug(
        "brain_graphrag_build: backfill=%s force=%s concepts=%s limit=%s",
        backfill,
        force,
        concepts,
        limit,
    )
    # Concepts run when the param is passed OR the env gate is on (mirrors the
    # CLI): the param is an explicit per-run opt-in independent of
    # BRAIN_GRAPH_CONCEPTS, so a caller can build the concept graph on demand
    # without flipping the ingest-time gate.
    include_concepts = concepts or cfg.graph_concepts
    config = _graphrag_reconcile_config(cfg, tenant)

    from .graph_rag.backends import AgeBackend
    from .graph_rag.build import build_graph
    from .graph_rag.extract import EntityExtractor, make_extractor

    extractor: EntityExtractor | None = None
    if include_concepts:
        config = replace(config, concepts_enabled=True)
        extractor = make_extractor(cfg)

    try:
        with connect_age(cfg.database_url) as conn:
            conn.autocommit = True
            _require_age_or_mcp_error(conn)
            backend = AgeBackend()
            backend.bootstrap(conn)
            document_ids = (
                doc_id for batch in iter_all_document_ids(conn) for doc_id in batch
            )
            result = build_graph(
                conn,
                document_ids,
                backend=backend,
                config=config,
                limit=limit,
                extractor=extractor,
                force=force,
            )
    except GraphBackendError as exc:
        raise _wrap_graph_backend_error(exc) from exc
    except GraphReconcileError as exc:
        raise _mcp_error(INTERNAL_ERROR, str(exc)) from exc
    except psycopg.Error as exc:
        raise _wrap_db_error(exc) from exc
    return {
        "processed": result.processed,
        "reconciled": result.reconciled,
        "skipped": result.skipped,
        "orphans_removed": result.orphans_removed,
        "tenant_id": config.tenant_id,
        "concepts": include_concepts,
    }


@mcp_app.tool()
def brain_graphrag_refresh(
    tenant: str | None = None,
) -> dict[str, Any]:
    """ADMIN — recompute a tenant's aggregate edges (no re-resolve; spec §7/§9).

    WHEN TO USE: a corpus-wide weight/edge recompute that does NOT re-resolve
    any document's persons — the response to a weighting / suppression knob
    change (e.g. a new ``BRAIN_GRAPH_GENERIC_DF``) that must propagate to every
    edge at once. Lighter than a full ``brain_graphrag_build``; NOT everyday
    querying. It assumes the tenant's entity vertices already exist — run
    ``brain_graphrag_build(backfill=true)`` first. For a dropped / corrupted AGE
    mirror (vertices missing) use ``brain_graphrag_build(force=true)`` instead.

    Full parity with ``brain graphrag refresh``: rebuilds every
    ``graph_relationships`` edge from ``graph_edge_contributions`` (normalized
    lift + generic suppression), GCs now-orphaned catalog rows, and
    rematerializes the AGE ``CO_OCCURS`` edges. Idempotent — a second run with
    the same config converges to the identical graph. Shares the SAME
    :class:`ReconcileConfig` as the incremental sync + the build path.

    Params:

    - ``tenant``: tenant to refresh (default ``BRAIN_GRAPH_TENANT``).

    Returns ``{tenant_id, relationship_count, orphans_removed}`` — the number of
    aggregate edges written and now-zero-mention catalog rows GC'd. No raw
    Cypher is ever returned.
    """
    state = _get_state()
    cfg = state.cfg
    logger.debug("brain_graphrag_refresh: tenant=%s", tenant)
    config = _graphrag_reconcile_config(cfg, tenant)

    from .graph_rag.aggregates import refresh_aggregates
    from .graph_rag.backends import AgeBackend

    try:
        with connect_age(cfg.database_url) as conn:
            conn.autocommit = True
            _require_age_or_mcp_error(conn)
            backend = AgeBackend()
            backend.bootstrap(conn)
            result = refresh_aggregates(conn, backend=backend, config=config)
    except GraphBackendError as exc:
        raise _wrap_graph_backend_error(exc) from exc
    except psycopg.Error as exc:
        raise _wrap_db_error(exc) from exc
    return {
        "tenant_id": config.tenant_id,
        "relationship_count": result.relationship_count,
        "orphans_removed": result.orphans_removed,
    }


# ---------------------------------------------------------------------------
# Wave G3-f — GraphRAG communities admin (MCP parity with the CLI
# `brain graphrag communities` group). Communities are RELATIONAL-only (spec
# §17c Q2/Q3); build/refresh run Louvain + persist + eagerly summarize, list
# reads the stored rows. Structured params only; tenant-scoped; NEVER raw Cypher.
# ---------------------------------------------------------------------------


def _graphrag_communities_build_or_mcp_error(
    cfg: Config,
    *,
    tenant: str | None,
    limit: int | None,
    force: bool,
    enricher: OllamaEnricher | None,
    embedder: Embedder | None,
) -> dict[str, Any]:
    """Build (``force=False``) / refresh (``force=True``) + summarize communities.

    The MCP twin of the CLI's :func:`brain.cli._run_communities_build`: resolves
    the tenant, runs :func:`build_communities` (dirty-gated unless ``force``),
    then the best-effort :func:`summarize_communities` (a ``None`` enricher or an
    unreachable Ollama leaves summaries NULL; a ``None`` embedder still writes
    summaries and only skips the embeddings — the build always succeeds; spec
    §17c Q10). Returns ``{tenant_id, build:{…}, summary:{…}}``.

    Error mapping mirrors :func:`_graphrag_search_or_mcp_error`:
    :class:`GraphTenantError` → ``INTERNAL_ERROR``,
    :class:`GraphBackendError` → wrapped (class-name only — no Cypher on the
    wire), :class:`psycopg.Error` → ``INTERNAL_ERROR``.
    """
    from .graph_rag.communities import build_communities
    from .graph_rag.communities_summary import summarize_communities
    from .graph_rag.tenancy import resolve_tenant

    try:
        tenant_id = resolve_tenant(cfg, tenant)
        with connect_age(cfg.database_url) as conn:
            conn.autocommit = True
            _require_age_or_mcp_error(conn)
            build_result = build_communities(conn, cfg, tenant=tenant_id, force=force)
            summary_result = summarize_communities(
                conn,
                cfg,
                tenant=tenant_id,
                enricher=enricher,
                embedder=embedder,
                limit=limit,
            )
    except GraphTenantError as exc:
        raise _mcp_error(INTERNAL_ERROR, str(exc)) from exc
    except GraphBackendError as exc:
        raise _wrap_graph_backend_error(exc) from exc
    except psycopg.Error as exc:
        raise _wrap_db_error(exc) from exc
    return {
        "tenant_id": tenant_id,
        "build": {
            "communities_total": build_result.communities_total,
            "created": build_result.created,
            "reused": build_result.reused,
            "deleted": build_result.deleted,
            "dirty": build_result.dirty,
            "skipped": build_result.skipped,
        },
        "summary": {
            "candidates": summary_result.candidates,
            "summarized": summary_result.summarized,
            "summary_failures": summary_result.summary_failures,
            "embedded": summary_result.embedded,
            "embed_failures": summary_result.embed_failures,
            "skipped": summary_result.skipped,
        },
    }


@mcp_app.tool()
def brain_graphrag_communities_build(
    tenant: str | None = None,
    limit: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """ADMIN/SETUP — detect + summarize the tenant's clusters (spec §17c Q3).

    WHEN TO USE: a slow, write-side prerequisite for ``mode='global'`` (and for
    ``brain_graphrag_communities`` to have rows to list) — NOT everyday
    querying. Run it once after a ``brain_graphrag_build``, then re-run only
    when ``brain doctor`` flags stale communities. To force a rebuild past the
    dirty gate after a corpus-wide weighting change, use ``force=True`` here or
    the sibling ``brain_graphrag_communities_refresh``.

    Full parity with ``brain graphrag communities build`` / ``… refresh``: runs
    Louvain over the tenant's relational ``graph_relationships`` edges, persists
    the partition to ``graph_communities`` / ``graph_community_members``, then
    EAGERLY (best-effort) summarizes + embeds each community via the server's
    enricher + embedder. A missing/unreachable Ollama leaves summaries NULL and
    the build still succeeds (the global path then ranks FTS-only).

    Params (mirror the CLI flags):

    - ``force``: bypass the ``(build_version, source_graph_hash)`` dirty gate and
      rebuild even when the graph is unchanged (the ``communities refresh``
      equivalent). Default ``False`` skips an unchanged graph.
    - ``tenant``: tenant to build (default ``BRAIN_GRAPH_TENANT``).
    - ``limit``: max stale/new communities to (re)summarize this run (does NOT
      cap detection — Louvain always partitions the full edge set).

    Returns ``{tenant_id, build:{communities_total, created, reused, deleted,
    dirty, skipped}, summary:{candidates, summarized, summary_failures, embedded,
    embed_failures, skipped}}``. No raw Cypher is ever returned.
    """
    state = _get_state()
    logger.debug(
        "brain_graphrag_communities_build: force=%s tenant=%s limit=%s",
        force,
        tenant,
        limit,
    )
    return _graphrag_communities_build_or_mcp_error(
        state.cfg,
        tenant=tenant,
        limit=limit,
        force=force,
        enricher=state.enricher,
        embedder=state.embedder,
    )


@mcp_app.tool()
def brain_graphrag_communities_refresh(
    tenant: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """ADMIN — force a community rebuild past the dirty gate (spec §17c Q3).

    WHEN TO USE: identical to ``brain_graphrag_communities_build`` except it
    BYPASSES the ``(build_version, source_graph_hash)`` dirty gate — Louvain +
    the relational replace always run, then the eager (best-effort)
    summary/embedding pass. Reach for it after a corpus-wide weighting /
    suppression change (or a knob change that should re-partition an otherwise
    unchanged graph); for a routine "build if stale" pass, use the plain
    ``brain_graphrag_communities_build`` (which skips an unchanged graph). NOT
    everyday querying.

    Full parity with ``brain graphrag communities refresh``: the MCP twin of
    ``brain_graphrag_communities_build(force=true)`` — same Louvain detection,
    relational persistence, and eager summary/embedding pass; a
    missing/unreachable Ollama leaves summaries NULL and the rebuild still
    succeeds (the global path then ranks FTS-only).

    Params (mirror the CLI flags):

    - ``tenant``: tenant to refresh (default ``BRAIN_GRAPH_TENANT``).
    - ``limit``: max stale/new communities to (re)summarize this run (does NOT
      cap detection — Louvain always partitions the full edge set).

    Returns the same ``{tenant_id, build:{…}, summary:{…}}`` shape as
    ``brain_graphrag_communities_build``. No raw Cypher is ever returned.
    """
    state = _get_state()
    logger.debug(
        "brain_graphrag_communities_refresh: tenant=%s limit=%s", tenant, limit
    )
    return _graphrag_communities_build_or_mcp_error(
        state.cfg,
        tenant=tenant,
        limit=limit,
        force=True,
        enricher=state.enricher,
        embedder=state.embedder,
    )


@mcp_app.tool()
def brain_graphrag_communities(
    tenant: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """List the tenant's materialized community clusters (spec §17c Q3).

    WHEN TO USE: an admin/overview read of the brain's top-level clusters —
    "what are the big clusters / themes in my brain" at a glance, or to confirm
    communities exist before a ``mode='global'`` search. Read-only and fast (no
    detection). For the actual ranked global retrieval use
    ``brain_graphrag_search(mode='global')``; to (re)build the clusters first
    use ``brain_graphrag_communities_build``.

    Full parity with ``brain graphrag communities list``: reads the stored
    ``graph_communities`` rows (largest-first, ``limit`` capped) and returns
    ``{tenant_id, count, communities:[{community_key, level, build_version,
    member_count, edge_count, total_weight, summary, summary_model,
    summary_at}]}``. Read-only; the raw ``summary_embedding`` vector is omitted
    and no raw Cypher is ever returned.

    Params:

    - ``tenant``: tenant to list (default ``BRAIN_GRAPH_TENANT``).
    - ``limit``: max communities to return (default all).
    """
    from .graph_rag.communities import list_communities
    from .graph_rag.tenancy import resolve_tenant

    state = _get_state()
    logger.debug("brain_graphrag_communities: tenant=%s limit=%s", tenant, limit)
    try:
        tenant_id = resolve_tenant(state.cfg, tenant)
        with connect_age(state.cfg.database_url) as conn:
            conn.autocommit = True
            _require_age_or_mcp_error(conn)
            records = list_communities(conn, tenant_id, limit=limit)
    except GraphTenantError as exc:
        raise _mcp_error(INTERNAL_ERROR, str(exc)) from exc
    except psycopg.Error as exc:
        raise _wrap_db_error(exc) from exc
    return {
        "tenant_id": tenant_id,
        "count": len(records),
        "communities": [community_record_json(r) for r in records],
    }


@mcp_app.tool()
def brain_graphrag_entities(
    entity_type: str | None = None,
    sort: str = "docs",
    limit: int = 50,
    tenant: str | None = None,
) -> dict[str, Any]:
    """ENUMERATE the entities in the graph — "what's in my brain" (admin view).

    WHEN TO USE: the ask is to LIST / inventory entities — "what orgs (or
    people / projects / topics / tools) are in my brain", "list all projects",
    "what topics do I have". Distinct from ``brain_graphrag_entity`` (singular),
    which traverses ONE entity's neighbourhood: this one just enumerates and
    filters by type. For a one-line size overview use ``brain_graphrag_stats``.

    Full parity with ``brain graphrag entities``: reads ``graph_entities``
    rows (filtered, sorted, ``limit``-capped) and returns
    ``{tenant_id, count, entities:[{entity_type, name, canonical_key,
    doc_count, description}]}``. Read-only; no raw Cypher.

    Params:

    - ``entity_type``: filter to one type — ``org`` / ``project`` / ``tool`` /
      ``topic`` / ``person`` (default all).
    - ``sort``: ``"docs"`` (doc_count DESC, default) or ``"name"`` (name ASC).
    - ``limit``: max entities to return (default 50; 0 = all).
    - ``tenant``: tenant to list (default ``BRAIN_GRAPH_TENANT``).
    """
    from .graph_rag.relational import list_entities
    from .graph_rag.tenancy import resolve_tenant

    state = _get_state()
    logger.debug(
        "brain_graphrag_entities: entity_type=%s sort=%s limit=%s tenant=%s",
        entity_type,
        sort,
        limit,
        tenant,
    )
    try:
        tenant_id = resolve_tenant(state.cfg, tenant)
        with connect_age(state.cfg.database_url) as conn:
            conn.autocommit = True
            _require_age_or_mcp_error(conn)
            rows = list_entities(
                conn, tenant_id, entity_type=entity_type, sort=sort, limit=limit
            )
    except GraphTenantError as exc:
        raise _mcp_error(INTERNAL_ERROR, str(exc)) from exc
    # Safe to surface str(exc): list_entities raises GraphBackendError only from
    # its own input-validation guards on a pure-relational path (no AGE/Cypher to
    # leak per spec §4 D9). If this path ever raises GraphBackendError from an
    # AGE/DB failure, switch to _wrap_db_error() instead.
    except GraphBackendError as exc:
        raise _mcp_error(INTERNAL_ERROR, str(exc)) from exc
    except psycopg.Error as exc:
        raise _wrap_db_error(exc) from exc
    return {
        "tenant_id": tenant_id,
        "count": len(rows),
        "entities": [entity_summaries_json(r) for r in rows],
    }


@mcp_app.tool()
def brain_graphrag_stats(
    tenant: str | None = None,
) -> dict[str, Any]:
    """OVERVIEW — how big is the graph, at a glance (the graph's "status").

    WHEN TO USE: the ask is about the graph's SIZE / shape — "how big is my
    graph", "how many entities/relationships/communities do I have", "is the
    graph built / worth querying". One fast read-only roll-up. To then list the
    entities themselves use ``brain_graphrag_entities``; to list clusters use
    ``brain_graphrag_communities``.

    Full parity with ``brain graphrag stats``: reads entity counts by type,
    total relationships, total communities, and the top-10 entities by
    doc_count from the relational tables. Returns ``{tenant_id,
    counts_by_type, total_entities, total_relationships, total_communities,
    top_entities:[…]}``. Read-only; no raw Cypher.

    Params:

    - ``tenant``: tenant to summarize (default ``BRAIN_GRAPH_TENANT``).
    """
    from .graph_rag.relational import graph_stats
    from .graph_rag.tenancy import resolve_tenant

    state = _get_state()
    logger.debug("brain_graphrag_stats: tenant=%s", tenant)
    try:
        tenant_id = resolve_tenant(state.cfg, tenant)
        with connect_age(state.cfg.database_url) as conn:
            conn.autocommit = True
            _require_age_or_mcp_error(conn)
            stats = graph_stats(conn, tenant_id)
    except GraphTenantError as exc:
        raise _mcp_error(INTERNAL_ERROR, str(exc)) from exc
    except psycopg.Error as exc:
        raise _wrap_db_error(exc) from exc
    return {"tenant_id": tenant_id, **graph_stats_json(stats)}


# ---------------------------------------------------------------------------
# Feedback (G4-b parity, spec §17d Q2) — the MCP counterpart of the CLI's
# `brain rate`. Closes the parity gap noted in the T3 rate audit: before this
# tool the graph feedback path (entity/community/theme ratings) was reachable
# ONLY from the CLI. Mirrors the CLI's local tuple rather than importing the
# writer's private `brain.interactions._VALID_TARGET_TYPES` set.
# ---------------------------------------------------------------------------
_RATE_TARGET_TYPES = ("entity", "community", "theme")


@mcp_app.tool()
def brain_rate(
    id: str,
    verdict: str,
    target_type: str | None = None,
    graph_retrieved: bool = False,
) -> dict[str, Any]:
    """Record a thumbs-up / thumbs-down on a document OR a graph target.

    WHEN TO USE: the user reacts to a specific result — "that one was useful" /
    "that's irrelevant". Works for BOTH a document (the default) and a graph
    target (an entity / community / theme), so graph retrieval feedback is
    reachable here, not just from the CLI. Graph retrieval tools
    (``brain_graphrag_*``) deliberately do NOT auto-log feedback at retrieval
    time — only this explicit user action does. (Document *opens* are logged
    separately by ``brain_show(originating_query=…)``.)

    Full parity with ``brain rate``. Persists one append-only row to the
    ``interactions`` table (``action='rated_useful'`` or ``'rated_irrelevant'``,
    ``source='mcp'``, ``session_id=NULL``). Ratings APPEND every call — re-rating
    the same target adds a new row with a fresh timestamp; the full history is
    preserved.

    Two mutually-exclusive target shapes (the XOR is enforced by
    ``record_interaction`` + the SQL CHECK):

    - Document (default — leave ``target_type`` unset): ``id`` is a document id
      prefix (6+ hex chars), resolved to a document. A too-short / non-hex /
      unknown / ambiguous prefix raises ``INVALID_PARAMS``.
    - Graph target (``target_type`` set): ``id`` is the durable graph-target id
      (entity UUID / community key / theme key) and is NOT resolved as a
      document; ``document_id`` stays NULL.

    Params:

    - ``id``: the document id prefix, or — when ``target_type`` is set — the
      graph target's id.
    - ``verdict``: ``'useful'`` or ``'irrelevant'`` (anything else →
      ``INVALID_PARAMS``).
    - ``target_type``: ``None`` (document, default) or one of
      ``'entity'`` / ``'community'`` / ``'theme'`` (anything else →
      ``INVALID_PARAMS``).
    - ``graph_retrieved``: provenance flag — set ``true`` when this rating came
      from a graph surface. Orthogonal to the target shape: a document rated via
      a graph path is still a document row with ``graph_retrieved=true``.

    Returns ``{interaction_id, action, graph_retrieved}`` plus ``document_id``
    (document shape) or ``target_type`` + ``target_id`` (graph shape). Unlike
    ``brain_show``'s best-effort open logging, a persistence failure here is
    surfaced as an MCP error — recording the rating IS this tool's job.
    """
    if verdict not in {"useful", "irrelevant"}:
        raise _mcp_error(
            INVALID_PARAMS, "verdict must be 'useful' or 'irrelevant'"
        )
    action: InteractionAction = (
        "rated_useful" if verdict == "useful" else "rated_irrelevant"
    )
    if target_type is not None and target_type not in _RATE_TARGET_TYPES:
        raise _mcp_error(
            INVALID_PARAMS,
            "target_type must be one of: " + ", ".join(_RATE_TARGET_TYPES),
        )
    state = _get_state()
    logger.debug(
        "brain_rate: target_type=%s graph_retrieved=%s", target_type, graph_retrieved
    )
    try:
        with connect(state.cfg.database_url) as conn:
            conn.autocommit = True
            if target_type is not None:
                # Graph-target rating: ``id`` is the durable target id, NOT a
                # document prefix — skip _resolve_id; document_id stays NULL.
                # Validated against _RATE_TARGET_TYPES above → safe to narrow to
                # the writer's Literal for the static checker.
                narrowed: InteractionTargetType = target_type  # type: ignore[assignment]
                new_id = record_interaction(
                    conn,
                    action=action,
                    source="mcp",
                    target_type=narrowed,
                    target_id=id,
                    graph_retrieved=graph_retrieved,
                )
                return {
                    "interaction_id": new_id,
                    "action": action,
                    "target_type": target_type,
                    "target_id": id,
                    "graph_retrieved": graph_retrieved,
                }
            # Document rating (the default shape).
            doc_id = _resolve_id(conn, id)
            new_id = record_interaction(
                conn,
                document_id=doc_id,
                action=action,
                source="mcp",
                graph_retrieved=graph_retrieved,
            )
    except InteractionError as exc:
        # Shape / enum guard tripped (caller-fixable) → INVALID_PARAMS.
        raise _mcp_error(INVALID_PARAMS, str(exc)) from exc
    except psycopg.Error as exc:
        raise _wrap_db_error(exc) from exc
    return {
        "interaction_id": new_id,
        "action": action,
        "document_id": doc_id,
        "graph_retrieved": graph_retrieved,
    }


def _build_note_file_text(
    *,
    body: str,
    template_text: str,
    title: str,
    document_id: str,
    tags: list[str],
    iso_now: str,
    date_iso: str,
    slug: str,
) -> str:
    """Assemble the on-disk file text for ``brain_note_new`` / ``brain_daily``.

    Mirrors :func:`brain.cli._build_note_text`'s contract: brain-managed
    frontmatter fields (``id`` / ``title`` / ``created`` / ``updated`` /
    ``kind`` / ``tags``) always win over whatever the template author
    wrote; the template's body is preserved (and substituted with
    ``{{title}}`` / ``{{date}}`` / ``{{datetime}}`` / ``{{slug}}``).

    The user-supplied ``body`` (from ``brain_note_new``) is prepended
    above the template's body so MCP-authored notes get the user's
    content first, then any boilerplate from the template (``daily.md``
    sections, etc.). Pass ``body=""`` to use only the template.
    """
    if template_text:
        rendered = render_template(
            template_text,
            {
                "title": title,
                "date": date_iso,
                "datetime": iso_now,
                "slug": slug,
            },
        )
        try:
            existing_fields, template_body = parse_frontmatter(rendered)
        except (ValueError, yaml.YAMLError):
            existing_fields = {}
            template_body = rendered
    else:
        existing_fields = {}
        template_body = ""

    if body and template_body:
        # User content first, then a blank line, then the template body.
        combined_body = f"{body.rstrip()}\n\n{template_body.lstrip()}"
    else:
        combined_body = body or template_body

    fields: dict[str, Any] = dict(existing_fields)
    fields["id"] = document_id
    fields["title"] = title
    fields["created"] = iso_now
    fields["updated"] = iso_now
    fields["kind"] = "vault"
    if tags:
        fields["tags"] = list(tags)
    elif "tags" not in fields:
        fields["tags"] = []

    return dump_frontmatter(fields, combined_body)


def _resolve_proposal_dst(
    conn: psycopg.Connection[Any], value: str
) -> tuple[str, str, str]:
    """Resolve ``value`` to ``(document_id, title, link_text)`` for proposals.

    Resolution order:

    - 6+ chars and all hex (digits + ``a-f`` + hyphens) → id-prefix lookup
      against ``documents.id``. On match, ``link_text`` falls back to
      ``brain:<short-id>`` so the resulting ``[[brain:...]]`` is
      unambiguous.
    - Otherwise → case-insensitive exact match on ``documents.title``.
      ``link_text`` is the dst's title verbatim (preserving the user's
      capitalization).

    Ambiguity (multiple title matches) raises ``INVALID_PARAMS`` with
    the candidate ids included in the error message.
    """
    cleaned = value.strip()
    if (
        len(cleaned) >= _ID_PREFIX_MIN_LEN
        and _HEX_ONLY_RE.match(cleaned.lower()) is not None
    ):
        rows = conn.execute(
            "SELECT id::text, title FROM documents "
            "WHERE id::text LIKE %s LIMIT 2",
            (cleaned.lower() + "%",),
        ).fetchall()
        if len(rows) == 1:
            dst_id = str(rows[0][0])
            dst_title = str(rows[0][1])
            return dst_id, dst_title, f"brain:{dst_id[:8]}"
        if len(rows) > 1:
            raise _mcp_error(
                INVALID_PARAMS,
                f"id-prefix {cleaned!r} is ambiguous; provide more characters",
            )
        # Zero hex matches → fall through to title resolution; users may
        # legitimately have a title that happens to look like hex.
    rows = conn.execute(
        "SELECT id::text, title FROM documents "
        "WHERE LOWER(title) = LOWER(%s) LIMIT 5",
        (cleaned,),
    ).fetchall()
    if not rows:
        raise _mcp_error(
            INVALID_PARAMS, f"no document found matching {cleaned!r}"
        )
    if len(rows) > 1:
        candidates = ", ".join(str(r[0])[:8] for r in rows)
        raise _mcp_error(
            INVALID_PARAMS,
            f"title {cleaned!r} is ambiguous (candidates: {candidates}); "
            "use a 6+ hex id prefix to disambiguate",
        )
    dst_id = str(rows[0][0])
    dst_title = str(rows[0][1])
    return dst_id, dst_title, dst_title


def _configure_logging() -> None:
    """Configure stderr logging from the ``BRAIN_MCP_LOG_LEVEL`` env var.

    Defaults to ``INFO``. Unknown values fall back to ``INFO`` and emit a
    warning. Logging always goes to stderr — stdout belongs to the MCP
    JSON-RPC channel.
    """
    raw_level = os.environ.get("BRAIN_MCP_LOG_LEVEL", "INFO").upper()
    if raw_level in _VALID_LOG_LEVELS:
        level = getattr(logging, raw_level)
        logging.basicConfig(stream=sys.stderr, level=level)
        logger.setLevel(level)
    else:
        logging.basicConfig(stream=sys.stderr, level=logging.INFO)
        logger.setLevel(logging.INFO)
        logger.warning(
            "BRAIN_MCP_LOG_LEVEL=%r is not a valid level; falling back to INFO",
            raw_level,
        )


def main() -> None:
    """Initialize shared state and run the brain-mcp server over stdio."""
    global _state
    _configure_logging()
    cfg = Config.load()
    embedder = make_embedder(cfg)
    # Wave Q2-SUMMARY-WIKI: build the long-lived enricher so the
    # ``brain_edit`` MCP tool can refresh ``documents.summary`` on
    # body-changing edits. Construction is cheap (no Ollama probe — the
    # hook handles unavailability inline). Per CLAUDE.md, every external
    # service has explicit timeouts; the enricher reads
    # ``Config.enrich_timeout_seconds`` from env.
    enricher = make_enricher(cfg)
    # Wave G1-c: build the per-process people-aspect graph syncer (shares one
    # ReconcileConfig). Cheap + self-gating on BRAIN_GRAPH_ENABLED + AGE
    # availability, so it's a no-op on a stock pgvector DB.
    graph_syncer = make_graph_syncer(cfg)
    _state = _State(
        cfg=cfg, embedder=embedder, enricher=enricher, graph_syncer=graph_syncer
    )
    # Warmup embed to cut cold-start latency on the first real ``brain_search``.
    # A single bounded retry covers cold-boot races where Ollama is up but the
    # embedding model is still loading. Persistent failure must NOT abort
    # startup — real tool calls surface the error to the MCP caller via
    # ``_wrap_embed_error``. Only ``OllamaEmbedError`` is caught so import /
    # programming errors still surface here.
    try:
        _state.embedder.embed(["hello"], input_type="document")
        logger.info("warmup embed completed")
    except OllamaEmbedError:
        time.sleep(_WARMUP_RETRY_DELAY_SECONDS)
        try:
            _state.embedder.embed(["hello"], input_type="document")
            logger.info("warmup embed completed (after retry)")
        except OllamaEmbedError as e:
            logger.warning(
                "warmup embed failed (continuing without): %s", type(e).__name__
            )
    logger.info("brain-mcp starting (stdio transport)")
    mcp_app.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - exercised by integration test
    main()
