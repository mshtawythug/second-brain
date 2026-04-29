"""MCP server exposing the second brain's tools over stdio."""
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml
from mcp import McpError
from mcp.server.fastmcp import FastMCP
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData

from .config import Config
from .db import connect
from .embeddings import OllamaEmbedError, make_embedder
from .errors import (
    IdPrefixAmbiguous,
    IdPrefixNotFound,
    IdPrefixNotHex,
    IdPrefixTooShort,
)
from .ingest import (
    Embedder,
    apply_tags,
    ingest_document,
    update_document,
)
from .ingest.stdin import make_doc as _stdin_make_doc
from .queries import (
    fetch_document,
    list_documents,
    resolve_document_prefix,
    summary_counts,
)
from .search import hybrid_search
from .vault.frontmatter import dump_frontmatter, parse_frontmatter
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


@dataclass
class _State:
    """Shared server state initialized once in :func:`main`.

    Tests build this directly and assign to ``_state`` (via
    ``monkeypatch.setattr``) instead of calling :func:`main`.
    """

    cfg: Config
    embedder: Embedder


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


@mcp_app.tool()
def brain_search(
    query: str,
    limit: int = 5,
    source: str | None = None,
    tag: str | None = None,
    since_days: int | None = None,
    fts_only: bool = False,
) -> list[dict[str, Any]]:
    """Hybrid search across the second brain.

    Returns up to ``limit`` matching documents ranked by RRF over FTS + vector
    cosine similarity. Filter by ``source`` kind, ``tag``, or ``since_days``
    recency. Set ``fts_only=True`` to skip the local Ollama embed call.
    """
    state = _get_state()
    logger.debug("brain_search: query=%r limit=%d", query, limit)
    try:
        with connect(state.cfg.database_url) as conn:
            results = hybrid_search(
                conn,
                embedder=state.embedder,
                query=query,
                limit=limit,
                source_kind=source,
                tag=tag,
                since_days=since_days,
                fts_only=fts_only,
            )
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    except OllamaEmbedError as e:
        raise _wrap_embed_error(e) from e
    return [
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
    ]


@mcp_app.tool()
def brain_show(id_prefix: str) -> dict[str, Any]:
    """Return the full body and metadata of a single document by id prefix.

    The prefix must be at least 6 hex characters and must uniquely identify a
    document. Raises :class:`McpError` (``INVALID_PARAMS``) if the prefix is
    too short, non-hex, unknown, or ambiguous.
    """
    state = _get_state()
    logger.debug("brain_show: id_prefix=%s", id_prefix)
    try:
        with connect(state.cfg.database_url) as conn:
            doc_id = _resolve_id(conn, id_prefix)
            doc = fetch_document(conn, doc_id)
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
    assert doc is not None  # _resolve_id confirmed the doc exists
    return {
        "id": doc.id,
        "title": doc.title,
        "content": doc.content,
        "content_type": doc.content_type,
        "tags": doc.tags,
        "source_path": doc.source_path,
        "ingested_at": doc.ingested_at.isoformat() if doc.ingested_at else None,
        "source_kind": doc.source_kind,
    }


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
            tags = apply_tags(conn, doc_id, add=add, remove=remove)
    except psycopg.Error as e:
        raise _wrap_db_error(e) from e
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
    _state = _State(cfg=cfg, embedder=embedder)
    # One-shot warmup embed to cut cold-start latency on the first real
    # ``brain_search``. Failure must NOT abort startup — search will retry on
    # demand. Catch ``OllamaEmbedError`` so import / programming errors still
    # surface.
    try:
        _state.embedder.embed(["hello"], input_type="document")
        logger.info("warmup embed completed")
    except OllamaEmbedError as e:
        logger.warning(
            "warmup embed failed (continuing without): %s", type(e).__name__
        )
    logger.info("brain-mcp starting (stdio transport)")
    mcp_app.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - exercised by integration test
    main()
