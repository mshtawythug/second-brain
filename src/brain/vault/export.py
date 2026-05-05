"""One-shot DB → vault export.

Walks ``documents`` in batches and materializes each row as a Markdown file
with YAML frontmatter under the vault root. Idempotent: a re-run skips files
whose existing content_hash on disk matches the document's content_hash in
the DB.

Older manual ingests have no ``sources`` row (no upstream identifier). For
those we synthesize a ``local-<short-uuid>`` placeholder in the filename so
two such docs with the same date + title can still be disambiguated.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml

from . import init_vault
from .frontmatter import dump_frontmatter, parse_frontmatter
from .slug import gmail_slug, slugify

_BATCH_SIZE = 100
_MAX_SHORT_ID = 8  # first N chars of an external/document UUID for filename use


@dataclass
class ExportSummary:
    """Outcome counts for ``brain vault export``."""

    written: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class _DocumentForExport:
    """Internal projection of a documents row plus its source kind/external id."""

    id: str
    title: str
    content: str
    content_hash: str
    content_type: str
    tags: list[str]
    metadata: dict[str, Any]
    ingested_at: datetime | None
    kind: str
    vault_path: str | None
    source_kind: str | None
    source_external_id: str | None
    draft: bool


def _is_directory_unmanaged(target: Path) -> bool:
    """True iff ``target`` is non-empty AND not a previously-initialized vault.

    A "managed" vault is one that already contains the ``README.md`` written
    by :func:`init_vault`. Anything else with files in it is unmanaged — the
    user has to opt in with ``--force`` so we never silently scribble into
    an unrelated folder.

    Dotfiles and hidden directories (``.git/``, ``.DS_Store``, etc.) are
    intentionally excluded from the emptiness check. A folder that contains
    only a ``.git`` directory still counts as empty for our purposes — the
    user almost certainly wants to use a git-tracked vault, and forcing
    them through ``--force`` for an empty initial repo would be obnoxious.
    """
    if not target.exists():
        return False
    if not target.is_dir():
        return True
    contents = [p for p in target.iterdir() if not p.name.startswith(".")]
    if not contents:
        return False
    # Treat the presence of vault README.md (signature of init_vault) as the
    # "yes, this is our folder" marker.
    return not (target / "README.md").is_file()


_DOCUMENT_FOR_EXPORT_COLUMNS = (
    "d.id::text, d.title, d.content, d.content_hash, "
    "d.content_type, d.tags, d.metadata, d.ingested_at, "
    "d.kind, d.vault_path, s.kind, s.external_id, d.draft"
)


def _row_to_document_for_export(row: tuple[Any, ...]) -> _DocumentForExport:
    """Project a SELECT row into the dataclass.

    Centralizes the column-index mapping so :func:`_iter_documents` and
    :func:`_fetch_document_for_export` share one source of truth and a
    schema change touches a single helper.
    """
    return _DocumentForExport(
        id=str(row[0]),
        title=str(row[1]),
        content=str(row[2]),
        content_hash=str(row[3]),
        content_type=str(row[4]),
        tags=list(row[5] or []),
        metadata=dict(row[6] or {}),
        ingested_at=row[7],
        kind=str(row[8]),
        vault_path=row[9],
        source_kind=row[10],
        source_external_id=row[11],
        draft=bool(row[12]),
    )


def _iter_documents(conn: psycopg.Connection[Any]) -> Iterator[_DocumentForExport]:
    """Yield every document, joined with its source row, in batches.

    Keyset pagination on ``documents.id`` keeps memory bounded even for very
    large corpora; the batch is materialized in Python (the connection is
    free for follow-up writes / lookups while the iterator is in flight).
    """
    last_id: str | None = None
    while True:
        if last_id is None:
            rows = conn.execute(
                f"""
                SELECT {_DOCUMENT_FOR_EXPORT_COLUMNS}
                FROM documents d
                LEFT JOIN sources s ON s.id = d.source_id
                ORDER BY d.id
                LIMIT %s
                """,
                (_BATCH_SIZE,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT {_DOCUMENT_FOR_EXPORT_COLUMNS}
                FROM documents d
                LEFT JOIN sources s ON s.id = d.source_id
                WHERE d.id > %s::uuid
                ORDER BY d.id
                LIMIT %s
                """,
                (last_id, _BATCH_SIZE),
            ).fetchall()
        if not rows:
            return
        last_id = str(rows[-1][0])
        for r in rows:
            yield _row_to_document_for_export(r)


def _fetch_document_for_export(
    conn: psycopg.Connection[Any], document_id: str
) -> _DocumentForExport | None:
    """Fetch a single ``documents`` row in the export projection.

    Returns ``None`` when no row matches ``document_id``. Mirrors the column
    list used by :func:`_iter_documents` so the resulting dataclass is
    populated identically for both code paths.
    """
    row = conn.execute(
        f"""
        SELECT {_DOCUMENT_FOR_EXPORT_COLUMNS}
        FROM documents d
        LEFT JOIN sources s ON s.id = d.source_id
        WHERE d.id = %s::uuid
        """,
        (document_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_document_for_export(row)


def _short_id(value: str) -> str:
    """Return the first 8 hex chars of a UUID-like value (no surrounding dashes).

    Used both for the external-id slot in filenames and for the collision
    suffix. Stripping dashes first makes ``-aabbccdd`` collisions deterministic
    regardless of the full UUID's hyphenation style. For non-UUID
    ``external_id`` values (e.g. Slack's ``1234567.890`` timestamps) the
    strip-then-truncate behavior preserves the leading bytes verbatim once
    any non-hex separator characters have been dropped.
    """
    return value.replace("-", "")[:_MAX_SHORT_ID]


def _date_prefix(doc: _DocumentForExport) -> str:
    """Pick the YYYY-MM-DD prefix for an ingested file's filename.

    ``metadata.date`` wins when present (a string ISO date or datetime, or a
    date/datetime object). Otherwise we fall back to ``ingested_at::date``,
    which is non-NULL by schema (``documents.ingested_at NOT NULL DEFAULT
    NOW()``) — the assert is defensive so a future schema regression
    surfaces here rather than producing a malformed filename.
    """
    raw = doc.metadata.get("date")
    if isinstance(raw, str) and raw:
        # Accept "2026-04-15" or "2026-04-15T..." — the date is the first 10 chars.
        return raw[:10]
    if isinstance(raw, (datetime, date)):
        return raw.strftime("%Y-%m-%d") if isinstance(raw, date) else raw.date().isoformat()
    assert doc.ingested_at is not None, "documents.ingested_at is NOT NULL"
    return doc.ingested_at.date().isoformat()


def _parse_iso_datetime(raw: Any) -> datetime | None:
    """Parse an ISO-8601 string (with optional trailing ``Z``) into a datetime.

    Returns ``None`` for any non-string / unparseable input. Used by the
    Gmail-slug branch to read ``metadata.sent_at`` (set by the gmail
    extractor). Naive datetimes survive as-is — :func:`gmail_slug` treats
    them as UTC.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _gmail_relative_path(doc: _DocumentForExport) -> str | None:
    """Compute the ``_ingested/gmail/<gmail-slug>.md`` path for a Gmail doc.

    Returns ``None`` when the metadata is too sparse to build a stable slug
    (no ``thread_id`` AND no usable date) — the caller falls back to the
    generic ingested-path rules so legacy rows still export. Idempotent: a
    given ``(thread_id, sent_at, subject)`` triple always yields the same
    path.
    """
    thread_id = doc.metadata.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        return None
    sent_at = _parse_iso_datetime(doc.metadata.get("sent_at"))
    if sent_at is None and doc.ingested_at is None:
        return None
    slug = gmail_slug(
        thread_id,
        sent_at,
        doc.title,
        fallback_date=doc.ingested_at,
    )
    return f"_ingested/gmail/{slug}.md"


def _ingested_relative_path(
    doc: _DocumentForExport, used_paths: set[str]
) -> str:
    """Compute the ``_ingested/<source>/...`` path for an ingested-tier doc.

    The shape depends on the source:

    - ``manual``: ``_ingested/manual/<slug>.md`` (no date prefix; older
      manual ingests had no upstream date at all).
    - ``gmail``: ``_ingested/gmail/<gmail-slug>.md`` where ``<gmail-slug>``
      is :func:`brain.vault.slug.gmail_slug` over
      ``(thread_id, sent_at, subject)``. Stable across re-ingests of the
      same thread; falls back to the generic shape below for legacy rows
      missing ``thread_id``.
    - everything else: ``_ingested/<source>/<YYYY-MM-DD>-<external-id>-<slug>.md``
      using the first 8 chars of ``sources.external_id`` (or
      ``local-<short-doc-id>`` when no source row exists, e.g. legacy
      manual ingests under any kind).

    On collision (two docs hashing to the same path) we append the short
    document UUID — stable across re-runs because it's keyed off the immutable
    ``documents.id``.
    """
    source = doc.source_kind or "manual"
    slug = slugify(doc.title)

    if source == "manual":
        candidate = f"_ingested/manual/{slug}.md"
    elif source == "gmail":
        gmail_path = _gmail_relative_path(doc)
        if gmail_path is not None:
            candidate = gmail_path
        else:
            date_prefix = _date_prefix(doc)
            external = (
                _short_id(doc.source_external_id)
                if doc.source_external_id
                else f"local-{_short_id(doc.id)}"
            )
            candidate = f"_ingested/gmail/{date_prefix}-{external}-{slug}.md"
    else:
        date_prefix = _date_prefix(doc)
        external = (
            _short_id(doc.source_external_id)
            if doc.source_external_id
            else f"local-{_short_id(doc.id)}"
        )
        candidate = f"_ingested/{source}/{date_prefix}-{external}-{slug}.md"

    if candidate in used_paths:
        # Deterministic collision suffix: short hash of the doc id, never a
        # counter (counters would shuffle every re-run).
        suffix = _short_id(doc.id)
        stem, ext = candidate.rsplit(".", 1)
        candidate = f"{stem}-{suffix}.{ext}"
    return candidate


def _resolve_relative_path(
    doc: _DocumentForExport, used_paths: set[str]
) -> str:
    """Pick the on-disk path for ``doc``.

    Rows with an explicit ``vault_path`` round-trip identity (vault-tier files
    stay where the user authored them; ingested mirrors stay at their recorded
    canonical path). Rows without one land in ``_ingested/`` per
    :func:`_ingested_relative_path`.
    """
    if doc.vault_path:
        return doc.vault_path
    return _ingested_relative_path(doc, used_paths)


def _build_frontmatter(doc: _DocumentForExport) -> dict[str, Any]:
    """Return the ordered frontmatter mapping for ``doc``.

    Field order is intentional and stable: id, title, created, updated, tags,
    aliases (when non-empty), kind, content_type, then ingested-tier extras
    (source / external_id) when present, and finally ``draft: true`` when the
    document is quarantined. Anything not applicable (e.g. ingested extras
    for a vault-tier doc, or an empty alias list) is omitted entirely rather
    than written as ``null`` / ``[]`` — keeps the vault file readable and
    round-trips cleanly through sync.

    The ``draft`` line is emitted ONLY when ``documents.draft`` is true. The
    default-false case is the vast majority of files; writing ``draft: false``
    on every export would be visual noise. The Quartz contentIndex emitter
    reads this key (or the absence of it) to filter quarantined docs out of
    the wiki's explorer / graph / search index without touching the DB row.

    Aliases come from ``documents.metadata['aliases']`` (the canonical
    storage location per the spec) — only string elements are emitted; any
    non-string value in the array is dropped silently to keep the
    frontmatter's ``aliases:`` always be a flat list of strings.
    """
    fields: dict[str, Any] = {
        "id": doc.id,
        "title": doc.title,
    }
    if doc.ingested_at is not None:
        iso = doc.ingested_at.isoformat()
        fields["created"] = iso
        fields["updated"] = iso
    fields["tags"] = list(doc.tags)
    aliases = _aliases_from_metadata(doc.metadata)
    if aliases:
        fields["aliases"] = aliases
    fields["kind"] = doc.kind
    fields["content_type"] = doc.content_type
    if doc.kind == "ingested":
        if doc.source_kind:
            fields["source"] = doc.source_kind
        if doc.source_external_id:
            fields["external_id"] = doc.source_external_id
    if doc.draft:
        fields["draft"] = True
    return fields


def _aliases_from_metadata(metadata: dict[str, Any]) -> list[str]:
    """Extract a clean list of alias strings from ``documents.metadata``.

    Only emits the field when the metadata payload actually carries one or
    more string aliases. Anything else (missing key, non-list, list of
    non-strings) returns an empty list — the caller then omits the
    ``aliases:`` line altogether. Defensive coercion here keeps a corrupt
    metadata blob from poisoning the entire export pass.
    """
    raw = metadata.get("aliases")
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, str) and a]


def _content_hash(text: str) -> str:
    """SHA-256 of ``text`` — matches the ingest pipeline's hashing scheme.

    Idempotent re-export compares this against ``documents.content_hash`` on a
    body re-extracted from the existing vault file (frontmatter stripped) so
    the user can edit frontmatter (or run a re-export with a new field
    ordering) without forcing a re-write.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _existing_body_hash(target: Path) -> str | None:
    """Read ``target`` as a vault file and return SHA-256 of its body.

    Returns ``None`` if the file doesn't exist or can't be parsed (we'll
    treat unparseable files as "needs rewrite" rather than skipping them
    silently). Frontmatter is stripped before hashing so a frontmatter-only
    edit doesn't trigger a needless body rewrite.
    """
    if not target.is_file():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        _, body = parse_frontmatter(text)
    except (ValueError, yaml.YAMLError):
        # ValueError covers our own "frontmatter must be a mapping" guard;
        # yaml.YAMLError covers malformed YAML inside the fences. Anything
        # broader would mask real bugs (per CLAUDE.md: always catch specific
        # exceptions).
        return None
    return _content_hash(body)


def _write_doc_file(
    doc: _DocumentForExport,
    *,
    vault_path: Path,
    relative: str,
    force: bool = False,
) -> tuple[Path, bool]:
    """Materialize ``doc`` at ``vault_path / relative`` and return the result.

    The returned tuple is ``(target_path, written)`` — ``written`` is False
    when the existing file's body already matches ``doc.content_hash``
    (idempotent skip), True when the file was rewritten or freshly created.

    ``force=True`` bypasses the body-hash skip and rewrites the file even
    when the body is unchanged. Callers must use ``force=True`` when they've
    already determined a frontmatter-only change happened — otherwise the
    body-hash check (which only fingerprints the body) silently drops the
    rewrite and leaves stale frontmatter on disk. The default
    ``force=False`` keeps the corpus-dump path (:func:`export_vault`) cheap
    on re-runs.

    Raises :class:`OSError` if the write fails. Callers decide whether to
    aggregate the failure into a summary or surface it directly.
    """
    target = vault_path / relative
    if not force:
        existing_hash = _existing_body_hash(target)
        if existing_hash == doc.content_hash:
            return target, False

    target.parent.mkdir(parents=True, exist_ok=True)
    fields = _build_frontmatter(doc)
    text = dump_frontmatter(fields, doc.content)
    target.write_text(text, encoding="utf-8")
    return target, True


def export_vault(
    conn: psycopg.Connection[Any],
    *,
    vault_path: Path,
    force: bool = False,
) -> ExportSummary:
    """Dump every ``documents`` row to ``vault_path`` as a Markdown file.

    Steps:

    1. Validate the target — refuse to write into a non-empty unmanaged
       directory unless ``force`` is true (keeps us from scribbling into an
       arbitrary folder the user pointed us at by mistake).
    2. Run :func:`init_vault` semantics so ``_templates/``, ``_ingested/``,
       and the README scaffold are guaranteed to exist.
    3. Iterate documents in keyset-paginated batches; for each one, compute
       the destination path, build the frontmatter, and skip the write if
       the existing file's body already matches the document's content_hash.

    Collisions in derived paths are resolved by appending a short
    document-id suffix — deterministic across re-runs because it's keyed off
    the immutable UUID, not a counter.
    """
    if _is_directory_unmanaged(vault_path) and not force:
        raise ValueError(
            f"target is not empty and was not created by this tool: "
            f"{vault_path} — pass --force to write into it"
        )

    init_vault(vault_path)

    summary = ExportSummary()
    used_paths: set[str] = set()

    for doc in _iter_documents(conn):
        relative = _resolve_relative_path(doc, used_paths)
        used_paths.add(relative)
        try:
            _, written = _write_doc_file(
                doc, vault_path=vault_path, relative=relative
            )
        except OSError as e:
            summary.errors.append(f"{relative}: {e}")
            continue
        if written:
            summary.written += 1
        else:
            summary.skipped += 1

    return summary


def regenerate_vault_file(
    conn: psycopg.Connection[Any],
    document_id: str,
    *,
    vault_path: Path,
    force: bool = False,
) -> Path:
    """Re-create a single doc's vault mirror file from its DB row.

    Returns the absolute path of the file written (or, on the idempotent
    skip path, the path that already matched). The body-hash check from
    :func:`export_vault` is preserved by default: if the on-disk body already
    matches ``documents.content_hash``, the file is left untouched.

    Pass ``force=True`` to bypass the body-hash skip and unconditionally
    rewrite the file. This is the right choice when the caller has already
    determined a change happened (e.g., a frontmatter-only edit such as a
    title, tags, metadata, or content_type update) — the body-hash check
    only fingerprints the body, so it would silently drop the rewrite and
    leave stale frontmatter on disk. The ingest pipeline always sets
    ``force=True`` because it gates the call on a confirmed change
    (created row or frontmatter-bearing field changed). The full-corpus
    :func:`export_vault` keeps ``force=False`` so re-runs stay cheap.

    Path resolution prefers the doc's existing ``vault_path`` when set —
    a previously-synced ingested doc must regenerate at the same place
    rather than at a freshly computed ``_ingested/<source>/...`` location
    (which would orphan the original mirror). Docs with no ``vault_path``
    fall back to the corpus-dump rules (``_resolve_relative_path``) with
    a fresh, single-doc collision set.

    Side effect: also UPDATEs ``documents.vault_path`` to the chosen
    relative path so subsequent ``regenerate_vault_file``, ``brain rm``,
    and ``brain tag`` calls all use the same on-disk location. The
    UPDATE is its own statement under ``conn.autocommit=True`` (which
    all current callers use) — a future caller wrapping this function
    in a read-only transaction would have to opt that statement in.
    The ``IS DISTINCT FROM`` predicate keeps it idempotent (NULL-safe):
    re-running on a row that already has the right ``vault_path`` is a
    no-op at the row level.

    Raises:
        ValueError: ``document_id`` does not match any row.
        ValueError: the row is ``kind='vault'``. Vault-tier authored notes
            are file-source-of-truth — regenerating from the DB risks
            losing edits that haven't been re-synced. Restore from backup
            or git instead.
        OSError: the write itself fails (permissions, disk full, etc.).
    """
    doc = _fetch_document_for_export(conn, document_id)
    if doc is None:
        raise ValueError(f"no document with id {document_id}")
    if doc.kind == "vault":
        raise ValueError(
            "cannot regenerate vault-tier authored note from DB; "
            "restore from backup or git instead"
        )

    relative = doc.vault_path or _resolve_relative_path(doc, used_paths=set())

    target, _ = _write_doc_file(
        doc, vault_path=vault_path, relative=relative, force=force
    )
    conn.execute(
        "UPDATE documents SET vault_path = %s "
        "WHERE id = %s AND vault_path IS DISTINCT FROM %s",
        (relative, document_id, relative),
    )
    return target.resolve()
