"""Vault → DB sync engine.

Walks a vault folder, reconciles every ``.md`` file against the
``documents`` table by frontmatter ``id``, materializes ``[[wiki-links]]``
into the ``links`` / ``unresolved_links`` tables, and (optionally) prunes
vault-tier rows whose files vanished.

**Tier-aware metadata contract:**

- For ``kind='vault'`` rows the file is authoritative — sync overwrites
  ``documents.metadata`` from the file's frontmatter on every pass. Aliases,
  freeform keys, everything in the YAML header round-trips into JSONB.
- For ``kind='ingested'`` rows the **DB** is authoritative for metadata.
  ``_ingested/`` files are content/title/tags mirrors, not metadata mirrors.
  Re-syncing an ingested-tier file does NOT overwrite ``documents.metadata``
  (e.g. ``date``, ``duration_min``, source-specific fields populated during
  the original ingest are preserved). New aliases written into an ingested
  file ARE merged in — they're authored by the user — but everything else
  the DB owns stays put.

**Atomicity:** each file's upsert runs in a single transaction. The disk
write that stamps an assigned ``id`` back into the file's frontmatter is
deferred until after the DB transaction commits, so a DB crash mid-sync
leaves the file untouched (the prior write-first-then-DB ordering could
leave a file id-stamped on disk with no DB row to back it).
"""
import hashlib
import json
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import yaml

from ..ingest import Embedder
from ..ingest.chunker import chunk_text
from .derived_links import DirectoryStore, rebuild_derived_for
from .derived_links.fence import strip_fence
from .frontmatter import body_hash, dump_frontmatter, parse_frontmatter
from .links import ParsedLink, parse_wiki_links
from .resolver import resolve_link, title_collisions

# Top-level vault directories (or anything starting with ``_``) that the sync
# engine never treats as authored notes. ``_templates`` is editor scaffolding,
# ``_attachments`` is binaries (non-Markdown), and ``_ingested`` is the
# read-only mirror tier (treated specially below).
_TEMPLATE_DIR_NAME = "_templates"
_ATTACHMENTS_DIR_NAME = "_attachments"
_INGESTED_DIR_NAME = "_ingested"

logger = logging.getLogger(__name__)


@dataclass
class SyncReport:
    """Aggregate counters returned from :func:`sync_vault`.

    Each counter is incremented exactly once per file, except
    ``links_resolved`` / ``links_unresolved`` which count link insertions
    (a file with three links contributes three to one or both of those).

    ``derived_links`` counts ``derived_links`` rows inserted by the
    metadata-aware linker pass that runs at the end of every non-dry-run
    sync (see :func:`brain.vault.derived_links.rebuild_derived_for`). The
    counter stays ``0`` on dry-run and on syncs that touched no Gmail/Krisp
    docs.

    ``errors`` lists files we couldn't sync along with a short human-readable
    reason; the file is skipped (no DB write) and the run continues.
    """

    created: int = 0
    updated: int = 0
    skipped: int = 0
    deleted: int = 0
    warned: int = 0
    links_resolved: int = 0
    links_unresolved: int = 0
    id_assigned: int = 0
    derived_links: int = 0
    errors: list[tuple[Path, str]] = field(default_factory=list)


@dataclass
class _WalkedFile:
    """Internal projection of one ``.md`` file the walker handed us."""

    abs_path: Path
    relative_posix: str  # always POSIX-style (forward slashes)
    classification: str  # 'vault' or 'ingested' — derived from the path


def sync_vault(
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder,
    vault_path: Path,
    prune: bool = False,
    dry_run: bool = False,
) -> SyncReport:
    """Reconcile every ``.md`` file under ``vault_path`` into the DB.

    Steps:

    1. Walk the vault, collecting ``.md`` files (skipping ``_templates/``
       and ``_attachments/``).
    2. For each file: parse frontmatter, assign an ``id`` if missing
       (writing back to disk), compute the body hash, then upsert the
       ``documents`` row + chunks. Re-parse links and materialize them.
    3. After the walk, look at every vault-tier ``documents`` row whose
       ``vault_path`` was NOT seen and either prune (``prune=True``) or
       warn.
    4. Re-resolution pass: for every previously-unresolved link whose
       ``src_document_id`` was processed in this run, retry the lookup
       so dangling refs that just got their target created turn into
       resolved links.
    5. Linker pass: rebuild ``derived_links`` for every touched doc so the
       metadata-derived (R1 shared_thread / R2 shared_participant /
       R3 same_day_participant) edges stay consistent with the latest
       Gmail / Krisp metadata. The pass is scoped to ``seen_doc_ids`` and
       the runner filters non-linkable (vault-tier) ids itself.

    ``embedder`` is an explicit dependency (Dependency Inversion): the sync
    engine never reaches for a global. Tests pass a fake; production passes
    whatever ``brain.embeddings.make_embedder`` returned.

    With ``dry_run=True`` no writes happen — neither to the DB nor to disk
    (no id assignment, no link materialization, no derived-links rebuild).
    The returned report still counts the actions that *would* have been
    taken for steps 1–3; ``derived_links`` stays at 0 because the linker is
    skipped entirely on dry-run.
    """
    report = SyncReport()
    if not vault_path.is_dir():
        report.errors.append((vault_path, "vault path does not exist or is not a directory"))
        return report

    walked: list[_WalkedFile] = list(_walk_vault(vault_path))
    seen_doc_ids: set[str] = set()
    seen_relative: set[str] = set()

    for walked_file in walked:
        seen_relative.add(walked_file.relative_posix)
        try:
            doc_id = _sync_one(
                conn,
                embedder=embedder,
                vault_path=vault_path,
                walked=walked_file,
                report=report,
                dry_run=dry_run,
            )
        except _SyncError as e:
            report.errors.append((walked_file.abs_path, str(e)))
            continue
        if doc_id is not None:
            seen_doc_ids.add(doc_id)

    # Step 3: detect missing-on-disk vault-tier rows.
    _process_missing(
        conn,
        seen_relative=seen_relative,
        report=report,
        prune=prune,
        dry_run=dry_run,
    )

    # Step 4: resolution pass for unresolved links from just-processed docs.
    if not dry_run and seen_doc_ids:
        _retry_unresolved(conn, seen_doc_ids, report)

    # Step 5: rebuild metadata-derived edges (R1/R2/R3) for the touched docs.
    # Skipped on dry-run to honor the "no DB writes" contract; the linker
    # opens its own transaction internally, so we call it AFTER the per-file
    # transactions in ``_sync_one`` have committed (no nesting). Vault-tier
    # docs in ``seen_doc_ids`` are silently filtered by the runner — only
    # gmail / krisp rows produce edges. ``rebuild_derived_for`` already
    # short-circuits on an empty set, so no extra guard here.
    if not dry_run:
        report.derived_links = rebuild_derived_for(
            conn, seen_doc_ids, directory=DirectoryStore(conn)
        )

    return report


def sync_one_file(
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder,
    vault_path: Path,
    file_path: Path,
) -> SyncReport:
    """Sync exactly one ``.md`` file under ``vault_path``.

    Used by the authoring commands (``brain note new``, ``brain daily``,
    ``brain edit`` for vault-tier docs) so we re-index just the file the user
    touched instead of walking the entire vault. The contract matches
    :func:`sync_vault` for the fields it touches:

    - The file is classified the same way as the full walker (path under
      ``_ingested/`` → ``kind='ingested'``; anywhere else → ``kind='vault'``;
      ``_templates/`` and ``_attachments/`` are explicitly out of bounds —
      passing one returns an error in ``report.errors``).
    - Frontmatter id is auto-assigned + written back to disk if missing
      (same recovery semantics as a full sync).
    - Wiki-links from this file are re-materialized; targets that exist
      anywhere in the DB resolve, others land in ``unresolved_links``.
    - The link-retry pass runs scoped to this single document so a body
      that just gained ``[[Foo]]`` (with Foo.md already in the DB from a
      prior sync) ends up resolved.
    - The metadata-derived linker pass also runs scoped to this single
      document so any ``derived_links`` rows touching it stay current.
      Vault-tier docs are filtered out by the runner — only Gmail / Krisp
      ids contribute edges.

    What's intentionally NOT re-checked: the rest of the vault. If another
    file's ``[[<this title>]]`` was previously unresolved, a full
    ``brain vault sync`` is still required to convert that dangling ref —
    otherwise we'd have to walk every file on every authoring action, which
    defeats the point of the helper. The follow-up sync is cheap (body-hash
    short-circuits every unchanged file).

    ``file_path`` may be absolute or relative to ``vault_path``; we
    normalize before classifying. A path outside ``vault_path``, a non-file,
    or a non-``.md`` file each surfaces in ``report.errors`` rather than
    raising — the caller is the CLI and a clean error message beats a
    traceback.
    """
    report = SyncReport()
    if not vault_path.is_dir():
        report.errors.append(
            (vault_path, "vault path does not exist or is not a directory")
        )
        return report

    abs_path = (
        file_path
        if file_path.is_absolute()
        else (vault_path / file_path)
    ).resolve()
    try:
        relative = abs_path.relative_to(vault_path.resolve())
    except ValueError:
        report.errors.append(
            (abs_path, "file is not under the vault path")
        )
        return report

    if abs_path.suffix.lower() != ".md":
        report.errors.append((abs_path, "not a .md file"))
        return report
    if not abs_path.is_file():
        report.errors.append((abs_path, "file does not exist"))
        return report

    parts = relative.parts
    if not parts:
        report.errors.append((abs_path, "empty relative path"))
        return report
    first = parts[0]
    if first in {_TEMPLATE_DIR_NAME, _ATTACHMENTS_DIR_NAME}:
        report.errors.append(
            (abs_path, f"path is under {first}/ — not a syncable note")
        )
        return report
    if _has_hidden_component(parts):
        report.errors.append(
            (abs_path, "path contains a hidden directory component (starts with '.')")
        )
        return report

    classification = "ingested" if first == _INGESTED_DIR_NAME else "vault"
    walked = _WalkedFile(
        abs_path=abs_path,
        relative_posix=relative.as_posix(),
        classification=classification,
    )

    try:
        doc_id = _sync_one(
            conn,
            embedder=embedder,
            vault_path=vault_path,
            walked=walked,
            report=report,
            dry_run=False,
        )
    except _SyncError as e:
        report.errors.append((walked.abs_path, str(e)))
        return report

    if doc_id is not None:
        _retry_unresolved(conn, {doc_id}, report)
        # Mirror ``sync_vault``: rebuild metadata-derived edges scoped to the
        # one doc we just processed. ``sync_one_file`` has no dry_run mode,
        # so the linker always runs here. Non-linkable (vault-tier) doc ids
        # are filtered by the runner.
        report.derived_links = rebuild_derived_for(
            conn, {doc_id}, directory=DirectoryStore(conn)
        )

    return report


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------


class _SyncError(Exception):
    """Per-file sync failure; the run continues with the next file."""


def _has_hidden_component(parts: tuple[str, ...]) -> bool:
    """Return True if any path component starts with ``.``.

    Mirrors :func:`brain.vault.watch._filter_path`'s hidden-directory test
    so the watcher and the one-shot sync agree on what's a non-syncable
    path. ``.git/``, ``.quartz/`` (Quartz workspace), ``.obsidian/`` (Obsidian
    config), VSCode's ``.vscode/``, etc. are all unwanted descents — those
    trees contain ``.md`` files (issue templates, Quartz docs) that aren't
    user notes and don't carry our frontmatter contract.
    """
    return any(part.startswith(".") for part in parts)


def _walk_vault(vault_path: Path) -> Iterator[_WalkedFile]:
    """Yield every Markdown file the sync should process.

    Skips ``_templates/`` and ``_attachments/`` entirely, plus any path
    whose components include a hidden directory (``.git/``, ``.quartz/``,
    ``.obsidian/``, …). Files under ``_ingested/<source>/...`` are still
    yielded but classified as ``ingested`` so the caller treats them with
    ``kind='ingested'``. All other ``.md`` files are vault-tier.

    Paths are yielded in deterministic (sorted) order so test assertions on
    iteration order are stable.
    """
    for path in sorted(vault_path.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(vault_path)
        except ValueError:
            # ``rglob`` always produces children of ``vault_path``; the
            # relative_to should never fail. Defensive: skip and move on.
            continue
        parts = relative.parts
        if not parts:
            continue
        first = parts[0]
        if first in {_TEMPLATE_DIR_NAME, _ATTACHMENTS_DIR_NAME}:
            continue
        if _has_hidden_component(parts):
            # Hidden directories (``.git/``, ``.quartz/``, ``.obsidian/``, …)
            # are tooling state, not authored notes. The watcher's
            # ``_filter_path`` already skips these; the one-shot sync now
            # matches that behavior so a vault containing a Quartz workspace
            # or any other dotted tree doesn't pollute the DB.
            continue
        classification = "ingested" if first == _INGESTED_DIR_NAME else "vault"
        yield _WalkedFile(
            abs_path=path,
            relative_posix=relative.as_posix(),
            classification=classification,
        )


def _sync_one(
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder,
    vault_path: Path,
    walked: _WalkedFile,
    report: SyncReport,
    dry_run: bool,
) -> str | None:
    """Sync a single file. Returns the document id (or None on dry-run / skip).

    Wraps the per-file work in a single transaction (when not dry-run) so a
    DB error mid-file leaves no half-written state — chunks, document row,
    and links all commit together or not at all.
    """
    try:
        text = walked.abs_path.read_text(encoding="utf-8")
    except OSError as e:
        raise _SyncError(f"could not read file: {e}") from e

    try:
        frontmatter, body = parse_frontmatter(text)
    except (ValueError, yaml.YAMLError) as e:
        raise _SyncError(f"malformed frontmatter: {e}") from e

    # Files without any frontmatter block at all (no opening ``---``) are
    # treated as tooling-managed documentation (vault README, ``_ingested/``
    # README written by ``brain vault init``). They're intentional, not
    # malformed, and don't belong in the search index — silently skip.
    if not frontmatter and not text.lstrip().startswith("---"):
        logger.debug(
            "vault sync: %s has no frontmatter — skipping as documentation",
            walked.relative_posix,
        )
        return None

    # Validate / coerce the frontmatter mapping into a strongly typed view.
    title = frontmatter.get("title")
    if not isinstance(title, str) or not title.strip():
        raise _SyncError("missing or empty 'title' in frontmatter")

    raw_id = frontmatter.get("id")
    needs_disk_write = False
    if not isinstance(raw_id, str) or not raw_id:
        report.id_assigned += 1
        if dry_run:
            # In dry-run we don't write to disk, but we still want to count
            # the would-create + planned link materialization. A fresh UUID
            # gives the rest of the function a stable key for the simulated
            # documents row; the user doesn't see this synthetic id.
            document_id = str(uuid.uuid4())
        else:
            # Recovery branch: a previous sync may have committed the DB row
            # but crashed before stamping the id back to disk. If the file's
            # vault_path matches an existing vault-tier row, reuse that id —
            # otherwise we'd orphan the prior row (different id) and produce
            # two DB rows for the same file across runs. Only matches by
            # path; matching by title would risk false positives.
            existing_by_path = conn.execute(
                "SELECT id::text FROM documents "
                "WHERE vault_path = %s AND kind = %s "
                "LIMIT 1",
                (walked.relative_posix, walked.classification),
            ).fetchone()
            if existing_by_path is not None:
                document_id = str(existing_by_path[0])
            else:
                document_id = str(uuid.uuid4())
            # Defer the disk write until AFTER the DB upsert commits — a
            # half-written file (id on disk, no DB row) was the prior bug.
            frontmatter = dict(frontmatter)
            frontmatter["id"] = document_id
            needs_disk_write = True
    else:
        document_id = raw_id

    # ``content_hash`` mirrors ``body_hash`` (frontmatter-stripped, normalized);
    # this is what makes export → sync a no-op on unchanged content. We also
    # compute the legacy "raw body" hash because Phase 1 export wrote rows
    # with ``content_hash = sha256(doc.content)`` — to keep the round-trip a
    # no-op (zero re-embeds) we treat *either* hash matching as "body
    # unchanged" and silently migrate to ``body_hash`` form on the first run.
    new_hash = body_hash(text)
    legacy_hash = _legacy_body_hash(body)
    tags = _coerce_tag_list(frontmatter.get("tags"))
    aliases = _coerce_alias_list(frontmatter.get("aliases"))
    content_type = (
        frontmatter["content_type"]
        if isinstance(frontmatter.get("content_type"), str)
        else "note"
    )

    # Path classification: path wins over frontmatter when there's a conflict.
    classification = walked.classification
    declared_kind = frontmatter.get("kind")
    if (
        isinstance(declared_kind, str)
        and declared_kind in {"vault", "ingested"}
        and declared_kind != classification
    ):
        logger.warning(
            "vault sync: %s declares kind=%r but its path implies %r — using path",
            walked.relative_posix,
            declared_kind,
            classification,
        )

    if dry_run:
        # Dry-run: we still count what *would* happen so users see the plan.
        # We don't open a transaction or write to the DB.
        existing = conn.execute(
            "SELECT content_hash, title, tags, kind, vault_path, metadata "
            "FROM documents WHERE id = %s",
            (document_id,),
        ).fetchone()
        normalized_body = _normalized_body(body)
        if existing is None:
            report.created += 1
        else:
            cur_hash, cur_title, cur_tags, _cur_kind, cur_vp, cur_meta = existing
            cur_tags = list(cur_tags or [])
            cur_meta = dict(cur_meta or {})
            planned_metadata = _build_metadata(
                frontmatter,
                aliases,
                tier=classification,
                existing_metadata=cur_meta,
            )
            body_unchanged = cur_hash in (new_hash, legacy_hash)
            metadata_visible_change = (
                cur_title != title
                or sorted(cur_tags) != sorted(tags)
                or cur_vp != walked.relative_posix
                or cur_meta != planned_metadata
            )
            if not body_unchanged or metadata_visible_change:
                report.updated += 1
            else:
                report.skipped += 1
        # Count links as if we materialized them — purely informational on dry-run.
        for parsed in parse_wiki_links(normalized_body):
            target = resolve_link(conn, parsed, exclude_doc_id=document_id)
            if target is None:
                report.links_unresolved += 1
            else:
                report.links_resolved += 1
        return document_id

    normalized_body = _normalized_body(body)
    with conn.transaction():
        existing = conn.execute(
            "SELECT content_hash, title, tags, content_type, metadata, kind, vault_path "
            "FROM documents WHERE id = %s",
            (document_id,),
        ).fetchone()
        if existing is None:
            # Brand-new row: build metadata fresh (no DB row to merge against).
            metadata = _build_metadata(
                frontmatter, aliases, tier=classification, existing_metadata=None
            )
            _insert_document(
                conn,
                embedder=embedder,
                document_id=document_id,
                title=title,
                content=normalized_body,
                content_hash=new_hash,
                content_type=content_type,
                tags=tags,
                metadata=metadata,
                kind=classification,
                vault_path=walked.relative_posix,
                source=_source_from_frontmatter(frontmatter, classification),
                external_id=_external_id_from_frontmatter(
                    frontmatter, classification
                ),
            )
            report.created += 1
        else:
            (
                cur_hash,
                cur_title,
                cur_tags,
                cur_type,
                cur_meta,
                cur_kind,
                cur_vault_path,
            ) = existing
            cur_tags = list(cur_tags or [])
            cur_meta = dict(cur_meta or {})
            # Tier-aware metadata: ingested-tier preserves DB-owned fields,
            # vault-tier rebuilds from frontmatter. See module docstring.
            metadata = _build_metadata(
                frontmatter,
                aliases,
                tier=classification,
                existing_metadata=cur_meta,
            )
            # Body is unchanged iff cur_hash matches either the new normalized
            # form or the legacy raw form; the latter handles freshly-exported
            # files whose content_hash predates Phase 2's normalization.
            body_unchanged = cur_hash in (new_hash, legacy_hash)
            metadata_changed = cur_meta != metadata
            tags_changed = sorted(cur_tags) != sorted(tags)
            title_changed = cur_title != title
            kind_changed = cur_kind != classification
            vault_path_changed = cur_vault_path != walked.relative_posix
            type_changed = cur_type != content_type
            user_visible_change = (
                metadata_changed
                or tags_changed
                or title_changed
                or kind_changed
                or vault_path_changed
                or type_changed
            )
            if not body_unchanged or user_visible_change:
                _update_document(
                    conn,
                    embedder=embedder,
                    document_id=document_id,
                    title=title,
                    content=normalized_body,
                    content_hash=new_hash,
                    content_type=content_type,
                    tags=tags,
                    metadata=metadata,
                    kind=classification,
                    vault_path=walked.relative_posix,
                    body_changed=not body_unchanged,
                )
                report.updated += 1
            elif cur_hash != new_hash:
                # Silent hash migration: body is byte-equivalent under the
                # new normalization, but the stored hash is in the legacy
                # (raw) form. Update the hash without re-embedding so the
                # next run is a clean skip.
                conn.execute(
                    "UPDATE documents SET content_hash = %s WHERE id = %s",
                    (new_hash, document_id),
                )
                report.skipped += 1
            else:
                report.skipped += 1

        # Always re-materialize links — cheap, and ensures the link graph is
        # consistent with the body even when we determined the doc itself
        # was a no-op (e.g. a frontmatter-only edit removed a link).
        _materialize_links(
            conn,
            document_id=document_id,
            body=normalized_body,
            title=title,
            report=report,
        )

    # DB transaction has now committed. Stamp the assigned id back onto disk —
    # if this fails, the DB row is intact and a follow-up sync will detect
    # the orphaned-id-on-disk via the vault_path recovery branch above.
    if needs_disk_write:
        try:
            walked.abs_path.write_text(
                dump_frontmatter(frontmatter, body), encoding="utf-8"
            )
        except OSError as e:
            logger.error(
                "vault sync: %s — DB row %s is committed but failed to "
                "write id back to disk: %s. Next sync will detect the "
                "missing id and rewrite the frontmatter.",
                walked.relative_posix,
                document_id[:8],
                e,
            )

    return document_id


def _normalized_body(body: str) -> str:
    """Return ``body`` with the fence + line-ending + trailing-whitespace normalization.

    The DB stores the canonical form: LF line endings, no trailing whitespace,
    no auto-generated derived-edges fence. Reading the value back and
    comparing to a freshly-normalized disk read must produce identical
    strings, so the export → sync round-trip stays a no-op even if the
    file was saved with CRLF, has an extra trailing newline, or carries a
    fenced "Related" section appended by the linker (Phase D).

    The fence strip is what keeps ``documents.content`` clean — vector
    search must not surface documents by their auto-generated "Related"
    list, and the wiki-link parser (which runs over this same normalized
    body in :func:`_materialize_links`) must not double-count fence-internal
    references that already live in ``derived_links``.
    """
    fence_stripped = strip_fence(body)
    return fence_stripped.replace("\r\n", "\n").replace("\r", "\n").strip()


def _legacy_body_hash(body: str) -> str:
    """SHA-256 of ``body`` (fence stripped, otherwise unnormalized).

    Phase 1 ingest computed ``documents.content_hash = sha256(doc.content)``
    with no normalization, and Phase 1 export wrote files whose body bytes
    matched ``doc.content`` exactly. This helper reproduces that legacy
    digest so the sync engine can recognize "body unchanged under the
    legacy hash" and skip a re-embed on the very first sync after a Phase
    1 export — preserving the round-trip-no-op contract.

    Phase D adds the fence strip: a Phase 1 file that has since been
    rendered with a derived-edges fence still hashes equal to its original
    content under the legacy form, so the linker's first relink doesn't
    accidentally invalidate the legacy-hash short-circuit and trigger a
    re-embed. The fence is contractually not authored body.
    """
    return hashlib.sha256(strip_fence(body).encode("utf-8")).hexdigest()


def _build_metadata(
    frontmatter: dict[str, Any],
    aliases: list[str],
    *,
    tier: str,
    existing_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project the file's frontmatter into ``documents.metadata`` shape.

    For vault-tier rows the file is authoritative: metadata is built fresh
    from frontmatter (only fields that aren't first-class columns end up in
    metadata: aliases, plus any user-supplied keys like ``priority``).

    For ingested-tier rows the DB is authoritative for metadata: source
    fields like ``date`` and ``duration_min`` are populated during the
    original ingest and re-syncing the mirror file must not blow them
    away. We merge the existing DB metadata with any new ``aliases`` from
    the file (user-authored aliases on an ingested mirror are intentional)
    but otherwise leave the DB metadata untouched.

    Aliases live in ``metadata['aliases']`` per the spec; we never write an
    empty list (keeps the JSONB tidy and matches how export omits the field).
    """
    if tier == "ingested":
        merged = dict(existing_metadata or {})
        if aliases:
            merged["aliases"] = aliases
        elif "aliases" in merged and not merged["aliases"]:
            # An empty aliases list in the DB is noise; drop it.
            del merged["aliases"]
        return merged

    meta: dict[str, Any] = {}
    reserved = {
        "id",
        "title",
        "tags",
        "aliases",
        "kind",
        "content_type",
        "source",
        "external_id",
        "created",
        "updated",
        "vault_path",
    }
    for key, value in frontmatter.items():
        if key in reserved:
            continue
        meta[key] = value
    if aliases:
        meta["aliases"] = aliases
    return meta


def _coerce_tag_list(value: Any) -> list[str]:
    """Coerce a frontmatter ``tags`` value into a clean ``list[str]``.

    Accepts ``None`` / missing → ``[]``, a list → filter to string elements,
    a single string → ``[string]`` (some users write ``tags: career`` as a
    scalar). Anything else returns an empty list — the file is still
    syncable, the user just sees no tags.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [t for t in value if isinstance(t, str) and t.strip()]
    return []


def _coerce_alias_list(value: Any) -> list[str]:
    """Coerce a frontmatter ``aliases`` value into a clean ``list[str]``.

    Mirrors :func:`_coerce_tag_list` — accepts a list of strings, a scalar
    string (rare but seen), or anything else → ``[]``.
    """
    return _coerce_tag_list(value)


def _source_from_frontmatter(
    frontmatter: dict[str, Any], tier: str
) -> str | None:
    """Extract the ``source`` field for ingested-tier files.

    Vault-tier files don't have a source, so we return ``None`` regardless
    of what the YAML says. For ingested-tier we trust the file's
    ``source:`` (krisp / slack / gmail / manual / ...). Returns ``None`` if
    the field is missing or not a string — the resulting row will have
    ``source_id=NULL`` which still lets ``brain show`` and ``brain search``
    work; only ``[[<source>:<external_id>]]`` resolution would miss it,
    which is a soft degradation rather than a failure.
    """
    if tier != "ingested":
        return None
    raw = frontmatter.get("source")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _external_id_from_frontmatter(
    frontmatter: dict[str, Any], tier: str
) -> str | None:
    """Extract the ``external_id`` field for ingested-tier files."""
    if tier != "ingested":
        return None
    raw = frontmatter.get("external_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _upsert_source_row(
    conn: psycopg.Connection[Any],
    *,
    kind: str,
    external_id: str | None,
) -> str | None:
    """Return an existing ``sources.id`` for ``(kind, external_id)``, or insert one.

    Mirrors the ingest pipeline's ``_upsert_source`` helper but with no
    metadata payload — vault-authored ingested-tier files don't have
    upstream metadata to merge in. Returns ``None`` when ``external_id``
    is ``None`` (a sourceless row gets ``source_id=NULL``, same as a
    purely manual ingest).
    """
    if external_id is None:
        return None
    row = conn.execute(
        "SELECT id::text FROM sources "
        "WHERE kind = %s AND external_id IS NOT DISTINCT FROM %s",
        (kind, external_id),
    ).fetchone()
    if row is not None:
        return str(row[0])
    new = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES (%s, %s, '{}'::jsonb) RETURNING id::text",
        (kind, external_id),
    ).fetchone()
    assert new is not None  # RETURNING id always yields a row
    return str(new[0])


def _insert_document(
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder,
    document_id: str,
    title: str,
    content: str,
    content_hash: str,
    content_type: str,
    tags: list[str],
    metadata: dict[str, Any],
    kind: str,
    vault_path: str,
    source: str | None = None,
    external_id: str | None = None,
) -> None:
    """Insert a brand-new ``documents`` row + chunks.

    Caller is responsible for the surrounding transaction — this helper
    runs INSERTs in the connection's current transaction.

    For ingested-tier rows with both ``source`` and ``external_id``, we
    upsert a ``sources`` row first so ``[[<source>:<external_id>]]`` link
    resolution works against this row from day one. Vault-authored
    ingested-tier files (a user manually drops one into ``_ingested/``)
    therefore don't depend on a prior ``brain ingest`` having run.
    """
    source_id: str | None = None
    if source is not None and external_id is not None:
        source_id = _upsert_source_row(conn, kind=source, external_id=external_id)
    conn.execute(
        """
        INSERT INTO documents
          (id, title, content, content_hash, content_type, tags,
           metadata, kind, vault_path, source_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
        """,
        (
            document_id,
            title,
            content,
            content_hash,
            content_type,
            tags,
            json.dumps(metadata),
            kind,
            vault_path,
            source_id,
        ),
    )
    _embed_and_insert_chunks(
        conn, embedder=embedder, document_id=document_id, content=content
    )


def _update_document(
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder,
    document_id: str,
    title: str,
    content: str,
    content_hash: str,
    content_type: str,
    tags: list[str],
    metadata: dict[str, Any],
    kind: str,
    vault_path: str,
    body_changed: bool,
) -> None:
    """Update an existing ``documents`` row + (when body changed) re-chunk."""
    conn.execute(
        """
        UPDATE documents SET
          title = %s,
          content = %s,
          content_hash = %s,
          content_type = %s,
          tags = %s,
          metadata = %s::jsonb,
          kind = %s,
          vault_path = %s
        WHERE id = %s
        """,
        (
            title,
            content,
            content_hash,
            content_type,
            tags,
            json.dumps(metadata),
            kind,
            vault_path,
            document_id,
        ),
    )
    if body_changed:
        conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
        _embed_and_insert_chunks(
            conn, embedder=embedder, document_id=document_id, content=content
        )


def _embed_and_insert_chunks(
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder,
    document_id: str,
    content: str,
) -> None:
    """Chunk + embed ``content`` and INSERT the resulting chunks.

    Empty / whitespace-only bodies produce zero chunks; the document row
    still exists (so ``brain show`` works) but is unsearchable until the user
    adds content. Matches the existing ingest pipeline's behavior.
    """
    chunks = chunk_text(content, count_tokens=embedder.count_tokens)
    if not chunks:
        return
    embeddings = embedder.embed([c.content for c in chunks], input_type="document")
    for c, vec in zip(chunks, embeddings, strict=True):
        conn.execute(
            "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
            "VALUES (%s, %s, %s, %s)",
            (document_id, c.index, c.content, vec),
        )


def _materialize_links(
    conn: psycopg.Connection[Any],
    *,
    document_id: str,
    body: str,
    title: str,
    report: SyncReport,
) -> None:
    """Drop + re-insert every link sourced from ``document_id``.

    The drop-and-recreate pattern is simpler than diffing and runs in the
    same transaction as the document upsert, so a crash mid-link leaves the
    DB consistent. Each parsed link either lands in ``links`` (resolved) or
    ``unresolved_links`` (dangling); the unique constraints on both tables
    deduplicate when a body has multiple identical ``[[X]]`` references.

    ``body`` is expected to already be fence-stripped — callers pass
    ``_normalized_body(...)``, which runs :func:`strip_fence` first.
    Wiki-links inside the auto-generated derived-edges fence (Phase D)
    therefore do NOT land in ``links``; those edges live in
    ``derived_links`` and would double-count if materialized here.
    """
    conn.execute(
        "DELETE FROM links WHERE src_document_id = %s", (document_id,)
    )
    conn.execute(
        "DELETE FROM unresolved_links WHERE src_document_id = %s", (document_id,)
    )
    for parsed in parse_wiki_links(body):
        target = resolve_link(conn, parsed, exclude_doc_id=document_id)
        if target is None:
            _record_unresolved(conn, document_id, parsed, title=title)
            report.links_unresolved += 1
        else:
            _record_resolved(conn, document_id, parsed, target.document_id)
            report.links_resolved += 1


def _record_resolved(
    conn: psycopg.Connection[Any],
    src_document_id: str,
    parsed: ParsedLink,
    dst_document_id: str,
) -> None:
    """INSERT into ``links``; ON CONFLICT DO NOTHING for repeated text in body."""
    conn.execute(
        """
        INSERT INTO links
          (src_document_id, dst_document_id, link_text, link_kind, display_text)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (src_document_id, dst_document_id, link_text, link_kind)
        DO NOTHING
        """,
        (
            src_document_id,
            dst_document_id,
            parsed.raw,
            parsed.kind,
            parsed.display_text,
        ),
    )


def _record_unresolved(
    conn: psycopg.Connection[Any],
    src_document_id: str,
    parsed: ParsedLink,
    *,
    title: str,
) -> None:
    """INSERT into ``unresolved_links`` with ON CONFLICT DO NOTHING.

    We log a one-line warning when the resolution failed because of a title
    collision — that's the single case where the user can fix it by adding
    an alias, and the diagnostic is the single most useful breadcrumb the
    sync produces.
    """
    if parsed.target_type == "title":
        collisions = title_collisions(
            conn, parsed.target_value, exclude_doc_id=src_document_id
        )
        if len(collisions) > 1:
            preview = ", ".join(c[:8] for c in collisions[:3])
            logger.warning(
                "vault sync: %r links to %r which matches %d documents (%s%s); "
                "use [[brain:<prefix>]] to disambiguate",
                title,
                parsed.target_value,
                len(collisions),
                preview,
                "…" if len(collisions) > 3 else "",
            )
    conn.execute(
        """
        INSERT INTO unresolved_links
          (src_document_id, link_text, link_kind, display_text)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (src_document_id, link_text, link_kind) DO NOTHING
        """,
        (src_document_id, parsed.raw, parsed.kind, parsed.display_text),
    )


def _process_missing(
    conn: psycopg.Connection[Any],
    *,
    seen_relative: set[str],
    report: SyncReport,
    prune: bool,
    dry_run: bool,
) -> None:
    """Find vault-tier rows whose ``vault_path`` was NOT seen and prune/warn.

    Dry-run reports the planned action via the report counters but never
    writes — and never logs a warning either, since dry-run is contractually
    "read-only" (no DB writes, no FS writes, no log noise the user wasn't
    asking for). The counters in the returned report still reflect what
    *would* happen.
    """
    rows = conn.execute(
        "SELECT id::text, vault_path, title FROM documents "
        "WHERE kind = 'vault' AND vault_path IS NOT NULL"
    ).fetchall()
    for doc_id, vault_path_value, title in rows:
        if vault_path_value in seen_relative:
            continue
        if prune:
            if not dry_run:
                conn.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
            report.deleted += 1
        else:
            if not dry_run:
                logger.warning(
                    "vault sync: %r (%s) has vault_path=%r but no file on disk; "
                    "use --prune to delete",
                    title,
                    str(doc_id)[:8],
                    vault_path_value,
                )
            report.warned += 1


def _retry_unresolved(
    conn: psycopg.Connection[Any],
    src_document_ids: set[str],
    report: SyncReport,
) -> None:
    """Try to resolve ``unresolved_links`` whose source was just processed.

    Restricting the scan to ``src_document_id IN (just-processed-ids)``
    keeps it cheap on a large vault — we don't re-scan dangling refs from
    every doc, only the ones whose body might have just been changed (or
    whose target may have just been created when another file in this
    same run produced it).

    Each newly-resolvable row moves from ``unresolved_links`` to ``links``
    in a single transaction (per row, to keep the transaction footprint
    small). The corresponding counters are adjusted so the final report
    reflects the post-pass state, not the per-file naive count.
    """
    if not src_document_ids:
        return
    # Two-step: collect the candidates (read-only), then for each one try a
    # resolve + move. Doing the move inline while iterating the cursor would
    # invalidate the cursor; the candidate set is small (just-processed
    # documents) so materializing it is cheap.
    rows = conn.execute(
        "SELECT id::text, src_document_id::text, link_text, link_kind, display_text "
        "FROM unresolved_links WHERE src_document_id = ANY(%s)",
        (list(src_document_ids),),
    ).fetchall()
    for row_id, src_id, link_text, link_kind, display_text in rows:
        parsed = _reparse_link_text(link_text, link_kind, display_text)
        if parsed is None:
            continue
        target = resolve_link(conn, parsed, exclude_doc_id=str(src_id))
        if target is None:
            continue
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO links
                  (src_document_id, dst_document_id, link_text, link_kind, display_text)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (src_document_id, dst_document_id, link_text, link_kind)
                DO NOTHING
                """,
                (
                    str(src_id),
                    target.document_id,
                    link_text,
                    link_kind,
                    display_text,
                ),
            )
            conn.execute(
                "DELETE FROM unresolved_links WHERE id = %s", (row_id,)
            )
        report.links_resolved += 1
        report.links_unresolved -= 1


def _reparse_link_text(
    link_text: str, link_kind: str, display_text: str | None
) -> ParsedLink | None:
    """Re-parse a stored ``unresolved_links.link_text`` back into a ParsedLink.

    The DB only stores the raw ``[[...]]`` form (we deliberately don't keep
    the parsed structure around — fewer columns to migrate later). To retry
    resolution we run the same parser over the stored text, which yields at
    most one ParsedLink (the entire text *is* one link). Returns ``None``
    for any malformed stored text — extremely unusual but the retry pass
    just skips it.
    """
    parsed = parse_wiki_links(link_text)
    if not parsed:
        return None
    only = parsed[0]
    # The retry pass uses the stored ``display_text`` rather than the
    # re-parsed one — the writer already extracted aliases, so they should
    # always agree. Defensive: the assertion catches a future divergence
    # rather than silently dropping the alias.
    assert only.display_text == display_text or only.display_text is None
    if link_kind not in {"wiki", "embed"}:
        return None
    return only
