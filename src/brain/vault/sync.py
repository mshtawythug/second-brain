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

**Why this file is allowed over the 800-line ceiling, and why it grew.** It is
one reconciliation algorithm; splitting it would separate the walk from the
per-file upsert that only makes sense inside it. The growth on this branch is
:func:`_source_from_frontmatter`, which now validates a file's ``source:``
against :data:`brain.source_kinds.VALID_SOURCE_KINDS` — the third and last write
boundary at which an unvalidated string could reach ``sources.kind`` (a column
with no CHECK constraint, read everywhere as a closed enum). Most of those lines
are the ruling on the FAILURE MODE, which is the non-obvious part: an unknown
kind is dropped to ``None`` and warned, never rejected and never substituted,
because a sync that fails a file over a metadata typo is worse than the defect
it prevents. The reasoning lives on the function; this pointer exists so it can
be found from the top.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import yaml

from ..errors import SecretGuardError
from ..ingest import Embedder, _chunk_search_metadata, _frontmatter_allows_secrets
from ..ingest.chunker import chunk_text
from ..ingest.guard import apply_guard
from ..ingest.sub_tokens import extract_sub_tokens
from ..queries import sync_chunk_search_metadata
from ..sensitivity import DEFAULT_SENSITIVITY, normalize_level
from ..source_kinds import VALID_SOURCE_KINDS, source_kinds_hint
from ..tags import normalize_tags
from .derived_links import DirectoryStore, rebuild_derived_for
from .derived_links.fence import rewrite_derived_fences, strip_fence
from .frontmatter import body_hash, dump_frontmatter, parse_frontmatter
from .link_rewrite import rewrite_vault_links
from .links import ParsedLink, parse_wiki_links
from .resolver import resolve_link, title_collisions

if TYPE_CHECKING:
    from ..graph_rag.sync import GraphSyncer

# Top-level vault directories (or anything starting with ``_``) that the sync
# engine never treats as authored notes. ``_templates`` is editor scaffolding,
# ``_attachments`` is binaries (non-Markdown), and ``_ingested`` is the
# read-only mirror tier (treated specially below).
_TEMPLATE_DIR_NAME = "_templates"
_ATTACHMENTS_DIR_NAME = "_attachments"
_INGESTED_DIR_NAME = "_ingested"

logger = logging.getLogger(__name__)

#: Default guard mode for the sync entry points. Deliberately ``off`` rather
#: than ``brain.config.DEFAULT_SECRET_GUARD`` ("warn"): sync has seven existing
#: call sites (``cli.py`` x4, ``mcp_server.py`` x2, the watcher) and defaulting
#: to anything active would change all of their behaviour silently. Callers opt
#: in by passing ``cfg.secret_guard`` explicitly.
_GUARD_OFF = "off"

#: Marker prefix on the ``_SyncError`` message a guard refusal raises. Lets the
#: per-file handlers count refusals separately from genuine failures without a
#: second exception type — the two share a handler by design, since both mean
#: "this file was skipped, the walk continues".
_GUARD_REFUSAL_PREFIX = "secret guard:"


def _is_guard_refusal(exc: Exception) -> bool:
    """True iff this ``_SyncError`` came from the secret guard."""
    return str(exc).startswith(_GUARD_REFUSAL_PREFIX)


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
    # Phase D — count of ``_ingested/`` files whose derived-edges fence was
    # rewritten this pass. Driven by ``rebuild_derived_for``'s affected-ids
    # set; stays at ``0`` on dry-run, on syncs with no affected ingested
    # docs, and when the renderer skips every candidate (vault-tier,
    # missing mirror file, etc.).
    fences_written: int = 0
    # Vault-tier files whose ``[[…]]`` markers were rewritten to
    # vault-root-relative path form so Quartz can resolve them without a
    # frontmatter lookup. Counted by :func:`brain.vault.link_rewrite.
    # rewrite_vault_links`; stays at ``0`` on dry-run, on syncs that
    # touched only ingested-tier files, when ``--no-link-rewrite`` is in
    # effect, and when every link is already in canonical path form
    # (idempotent re-sync).
    links_rewritten: int = 0
    # Files the F4 secret guard refused, so one credential-bearing note is a
    # REPORTED SKIP rather than a dead walk. Counted separately from ``errors``
    # (which it also appears in) because a guard refusal is a deliberate policy
    # outcome, not a malfunction: a caller showing "3 files refused by the
    # secret guard" is telling the user something actionable, whereas folding it
    # into a generic error count reads as breakage.
    secrets_refused: int = 0
    # Fence-stripped body baseline the vault watcher caches for fence-only-write
    # detection. Set only by the single-file path (:func:`_sync_one` via
    # :func:`sync_one_file`) and reflects the content sync actually INDEXED —
    # NOT a fresh disk read that could have picked up a racing user edit. The
    # watcher prefers this over re-reading disk so an edit landing between
    # sync's write and the cache refresh is never masked as a no-op. Stays
    # ``None`` on dry-run, on documentation-skips, and on bulk ``sync_vault``
    # aggregate reports (only the per-file watcher path consumes it).
    body_baseline: str | None = None
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
    link_rewrite: bool = True,
    owner_participants: frozenset[str] = frozenset(),
    graph_syncer: GraphSyncer | None = None,
    secret_guard: str = _GUARD_OFF,
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
                secret_guard=secret_guard,
            )
        except _SyncError as e:
            report.errors.append((walked_file.abs_path, str(e)))
            if _is_guard_refusal(e):
                report.secrets_refused += 1
            continue
        if doc_id is not None:
            seen_doc_ids.add(doc_id)

    # Step 3: detect missing-on-disk vault-tier rows.
    pruned_ids = _process_missing(
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
    #
    # Step 6 (Phase D): regenerate the fenced "Related" section in every
    # affected ``_ingested/`` file so Quartz's graph view reflects the
    # latest derived edges. The renderer is also a no-op on an empty
    # affected-set, so it's safe to call unconditionally on the post-linker
    # branch.
    if not dry_run:
        report.derived_links, affected_ids = rebuild_derived_for(
            conn,
            seen_doc_ids,
            directory=DirectoryStore(conn),
            owner_participants=owner_participants,
        )
        report.fences_written = rewrite_derived_fences(
            conn, affected_ids, vault_path=vault_path
        )
        if link_rewrite:
            report.links_rewritten = _rewrite_vault_tier_links(
                conn,
                vault_path=vault_path,
                doc_ids=seen_doc_ids,
            )

    # Step 7 (wave G1-c): people-aspect graph sync. Runs only on a real
    # (non-dry-run) sync, AFTER the per-file transactions + linker pass have
    # committed, reusing ``conn``. Reconcile every doc we touched (most are
    # watermark-skips — vault-tier rows carry no participants, and ingested
    # mirrors keep DB-authoritative metadata that sync doesn't change — so the
    # graph only moves for the rare synced doc whose person set actually
    # differs) and remove every pruned doc. Both calls are best-effort /
    # never-raise and no-op when graph sync is disabled or AGE is absent.
    if not dry_run and graph_syncer is not None:
        for doc_id in sorted(seen_doc_ids):
            graph_syncer.reconcile(conn, doc_id)
        for doc_id in pruned_ids:
            graph_syncer.remove(conn, doc_id)

    return report


def sync_one_file(
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder,
    vault_path: Path,
    file_path: Path,
    link_rewrite: bool = True,
    owner_participants: frozenset[str] = frozenset(),
    graph_syncer: GraphSyncer | None = None,
    secret_guard: str = _GUARD_OFF,
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
            secret_guard=secret_guard,
        )
    except _SyncError as e:
        report.errors.append((walked.abs_path, str(e)))
        if _is_guard_refusal(e):
            report.secrets_refused += 1
        return report

    if doc_id is not None:
        _retry_unresolved(conn, {doc_id}, report)
        # Mirror ``sync_vault``: rebuild metadata-derived edges scoped to the
        # one doc we just processed. ``sync_one_file`` has no dry_run mode,
        # so the linker always runs here. Non-linkable (vault-tier) doc ids
        # are filtered by the runner.
        #
        # Phase D step 6: regenerate the fenced "Related" section in every
        # ``_ingested/`` file the linker touched. ``rewrite_derived_fences``
        # is also a no-op on an empty affected-set.
        report.derived_links, affected_ids = rebuild_derived_for(
            conn,
            {doc_id},
            directory=DirectoryStore(conn),
            owner_participants=owner_participants,
        )
        report.fences_written = rewrite_derived_fences(
            conn, affected_ids, vault_path=vault_path
        )
        # Vault-tier link rewrite — only runs for the one doc we just
        # processed, and only when the path lives under a vault-tier
        # directory (the rewriter is a no-op for ingested-tier mirrors).
        if (
            link_rewrite
            and walked.classification == "vault"
            and rewrite_vault_links(
                walked.abs_path,
                document_id=doc_id,
                conn=conn,
            )
        ):
            report.links_rewritten += 1

        # Wave G1-c: reconcile the single touched doc into the people graph
        # (best-effort / never-raises; a no-op when graph sync is disabled or
        # AGE is absent). Runs after the per-file + linker transactions
        # committed, reusing ``conn``.
        if graph_syncer is not None:
            graph_syncer.reconcile(conn, doc_id)

    return report


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------


class _SyncError(Exception):
    """Per-file sync failure; the run continues with the next file."""


def _violation_constraint(exc: psycopg.errors.UniqueViolation) -> str:
    """Extract a human-readable constraint name from a ``UniqueViolation``.

    Falls back to a sentinel when ``exc.diag.constraint_name`` is empty —
    psycopg's diagnostic fields are documented as nullable, and we'd rather
    surface a stable label in :class:`SyncReport.errors` than crash a second
    time formatting the original error.
    """
    name = getattr(exc.diag, "constraint_name", None)
    return name or "<unknown>"


def _rewrite_vault_tier_links(
    conn: psycopg.Connection[Any],
    *,
    vault_path: Path,
    doc_ids: set[str],
) -> int:
    """Rewrite ``[[…]]`` markers in every vault-tier file in ``doc_ids``.

    Pulls each touched doc's ``kind`` + ``vault_path`` in one batch query
    so we don't issue N round-trips on a full-vault sync, then dispatches
    to :func:`brain.vault.link_rewrite.rewrite_vault_links` for each
    vault-tier file that exists on disk. Ingested-tier docs are silently
    filtered — wiki-link rewriting is contractually a vault-tier-only
    operation (their ``[[…]]`` markers would all be inside the
    auto-generated derived-edges fence, which is itself regenerated each
    sync).

    Empty input short-circuits to ``0`` — no DB round-trip, no FS scan.
    Returns the count of files actually rewritten on disk; files that
    were already in canonical path form (idempotent fast path) and files
    skipped due to read/write errors do not contribute to the count.
    """
    if not doc_ids:
        return 0
    rows = conn.execute(
        "SELECT id::text, vault_path FROM documents "
        "WHERE id = ANY(%s) AND kind = 'vault'",
        (sorted(doc_ids),),
    ).fetchall()
    rewritten = 0
    for doc_id, vp in rows:
        if not vp:
            # Vault-tier rows are supposed to have a ``vault_path``; defensive
            # skip for the corrupted-row case (no file to rewrite).
            continue
        target = vault_path / str(vp)
        if not target.is_file():
            continue
        if rewrite_vault_links(
            target,
            document_id=str(doc_id),
            conn=conn,
        ):
            rewritten += 1
    return rewritten


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
    secret_guard: str = _GUARD_OFF,
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
    parsed_tags = _coerce_tag_list(frontmatter.get("tags"))
    tags = normalize_tags(parsed_tags)
    # If the on-disk frontmatter has non-canonical tags (mixed case,
    # underscores, duplicates), schedule a write-back so the file
    # converges with what we're about to upsert into ``documents.tags``.
    # This piggybacks on the existing ``needs_disk_write`` path that the
    # missing-id branch already uses, so we still write the file at most
    # once per sync. ``updated:`` is intentionally NOT bumped here — a tag
    # normalization is a one-time canonicalization, not an authored edit;
    # bumping ``updated:`` on every initial sync would churn git history.
    if tags != parsed_tags:
        frontmatter["tags"] = tags
        needs_disk_write = True
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

    # F4 secret guard — BEFORE ``_normalized_body`` feeds the content hash, for
    # the same load-bearing reason as the ingest pipeline: the stored hash must
    # describe the bytes actually stored, or a redacted body would be compared
    # against the hash of the un-redacted text and the next sync would resolve
    # to a no-op, freezing the redaction while reporting it as up to date.
    body = _guard_synced_body(
        body,
        title=title,
        mode=secret_guard,
        tier=classification,
        metadata=frontmatter,
    )
    normalized_body = _normalized_body(body)
    with conn.transaction():
        existing = conn.execute(
            "SELECT content_hash, title, tags, content_type, metadata, kind, "
            "vault_path, sensitivity FROM documents WHERE id = %s",
            (document_id,),
        ).fetchone()
        if existing is None:
            # Brand-new row: build metadata fresh (no DB row to merge against).
            metadata = _build_metadata(
                frontmatter, aliases, tier=classification, existing_metadata=None
            )
            sensitivity = _sensitivity_from_frontmatter(
                frontmatter,
                tier=classification,
                current=DEFAULT_SENSITIVITY,
                path=walked.abs_path,
            )
            try:
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
                    source=_source_from_frontmatter(
                        frontmatter, classification, path=walked.relative_posix
                    ),
                    external_id=_external_id_from_frontmatter(
                        frontmatter, classification
                    ),
                    sensitivity=sensitivity,
                )
            except psycopg.errors.UniqueViolation as exc:
                # Mirror drift: the file's vault_path or content_hash already
                # belongs to another row. One bad file must not crash the
                # whole sync — convert to ``_SyncError`` so the outer loop
                # routes it into ``report.errors`` and continues.
                raise _SyncError(
                    "unique constraint violation (mirror drift): "
                    f"{_violation_constraint(exc)}"
                ) from exc
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
                cur_sensitivity,
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
            # F6: resolved AFTER ``cur_sensitivity`` is known, because the
            # ingested-tier branch returns it unchanged (the DB is authoritative
            # there — see the helper's docstring).
            sensitivity = _sensitivity_from_frontmatter(
                frontmatter,
                tier=classification,
                current=str(cur_sensitivity),
                path=walked.abs_path,
            )
            sensitivity_changed = str(cur_sensitivity) != sensitivity
            user_visible_change = (
                metadata_changed
                or tags_changed
                or title_changed
                or kind_changed
                or vault_path_changed
                or type_changed
                # Without this, editing ONLY the ``sensitivity:`` line in a note
                # would leave the body hash unchanged and every other field
                # equal, so the update would be skipped entirely and the trust
                # tier the user just set would never reach the column.
                or sensitivity_changed
            )
            if not body_unchanged or user_visible_change:
                try:
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
                        sensitivity=sensitivity,
                        body_changed=not body_unchanged,
                    )
                except psycopg.errors.UniqueViolation as exc:
                    # Mirror drift on update: another row already owns the
                    # vault_path or content_hash this file is trying to
                    # claim. Convert to ``_SyncError`` so the run continues.
                    raise _SyncError(
                        "unique constraint violation (mirror drift): "
                        f"{_violation_constraint(exc)}"
                    ) from exc
                report.updated += 1
            elif cur_hash != new_hash:
                # Silent hash migration: body is byte-equivalent under the
                # new normalization, but the stored hash is in the legacy
                # (raw) form. Update the hash without re-embedding so the
                # next run is a clean skip.
                # updated_at deliberately NOT bumped — the hash FORM changed,
                # the knowledge did not. One `brain vault sync` would
                # otherwise restamp every pre-normalization document.
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

    # DB transaction has now committed. Stamp the assigned id (and any
    # normalized tags) back onto disk — if this fails, the DB row is intact
    # and a follow-up sync will detect the orphaned-id-on-disk via the
    # vault_path recovery branch above.
    if needs_disk_write:
        _write_frontmatter_back(
            walked.abs_path,
            frontmatter=frontmatter,
            body=body,
            original_text=text,
            document_id=document_id,
            relative_posix=walked.relative_posix,
        )

    # Record the fence-stripped body baseline the watcher should cache for
    # fence-only-write detection: the content sync actually INDEXED, NOT a
    # fresh disk read that could have picked up a racing user edit. When we
    # wrote frontmatter back, reconstruct the intended on-disk text from the
    # indexed body so the write-back's own follow-up event is recognized as a
    # no-op; otherwise the file is untouched on disk and equals ``text``.
    baseline_text = (
        dump_frontmatter(frontmatter, body) if needs_disk_write else text
    )
    report.body_baseline = strip_fence(baseline_text)

    return document_id


def _write_frontmatter_back(
    abs_path: Path,
    *,
    frontmatter: dict[str, Any],
    body: str,
    original_text: str,
    document_id: str,
    relative_posix: str,
) -> None:
    """Stamp frontmatter (assigned id / canonical tags) back onto disk without
    clobbering a concurrent user edit.

    ``_sync_one`` reads the file, then embeds — a multi-second call for a real
    backend. If the user saves the file during that window, the naive
    ``write_text(dump_frontmatter(frontmatter, body))`` would overwrite their
    new text with the stale ``body`` captured before the embed. Guard against
    that: re-read the file; if its bytes changed since the initial read
    (``original_text``), splice ONLY the frontmatter onto the freshly-read
    body so the user's edit survives. If the fresh file can't be read or its
    frontmatter no longer parses, skip the write-back with a WARN — a later
    sync reconciles the missing id rather than risk clobbering the edit.

    The DB row is one sync behind in this case (it was built from the body we
    read before the embed); the next sync's body-hash check re-embeds the
    freshly-saved content, so the two converge on the following pass.
    """
    try:
        current_text = abs_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error(
            "vault sync: %s — DB row %s is committed but the file could not "
            "be re-read for id write-back: %s. Next sync will detect the "
            "missing id and rewrite the frontmatter.",
            relative_posix,
            document_id[:8],
            e,
        )
        return

    body_to_write = body
    if current_text != original_text:
        # A concurrent edit landed between the initial read and now.
        try:
            _fresh_frontmatter, fresh_body = parse_frontmatter(current_text)
        except (ValueError, yaml.YAMLError):
            logger.warning(
                "vault sync: %s changed during sync and now has malformed "
                "frontmatter — skipping id write-back to avoid clobbering the "
                "concurrent edit; next sync will reconcile.",
                relative_posix,
            )
            return
        body_to_write = fresh_body
        logger.info(
            "vault sync: %s changed during sync — splicing frontmatter onto "
            "the freshly-read body so the concurrent edit survives.",
            relative_posix,
        )

    try:
        abs_path.write_text(
            dump_frontmatter(frontmatter, body_to_write), encoding="utf-8"
        )
    except OSError as e:
        logger.error(
            "vault sync: %s — DB row %s is committed but failed to write id "
            "back to disk: %s. Next sync will detect the missing id and "
            "rewrite the frontmatter.",
            relative_posix,
            document_id[:8],
            e,
        )


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
        # F6 trust boundary. Reserved so a note's ``sensitivity:`` never lands
        # in ``documents.metadata`` — it belongs in the typed column, and a
        # shadow copy in the JSONB blob would be a second source of truth for a
        # security-relevant value.
        #
        # This entry and export's ``_EXPORT_OWNED_FRONTMATTER_KEYS`` entry are a
        # PAIR and were added in one change. A key present in export's strip set
        # but absent here is silently deleted from the user's file on the next
        # export — the exact regression documented at ``export.py:44-57``.
        # ``tests/test_vault_frontmatter_registry_parity.py`` pins the invariant.
        "sensitivity",
    }
    for key, value in frontmatter.items():
        if key in reserved:
            continue
        meta[key] = value
    if aliases:
        meta["aliases"] = aliases
    return meta


def _guard_synced_body(
    content: str,
    *,
    title: str,
    mode: str,
    tier: str,
    metadata: dict[str, Any],
) -> str:
    """Run the F4 secret guard over a file's body; return what to STORE.

    Raises :class:`_SyncError` — never :class:`~brain.errors.SecretGuardError` —
    so the refusal lands in the SAME per-file handler every other bad-file
    condition uses (``sync_vault``'s walk loop and ``sync_one_file`` both catch
    ``_SyncError``, record it in ``report.errors``, and continue). Letting the
    guard's own exception escape would abort the entire walk on one offending
    file, which the guard's docstring explicitly names as a worse failure than a
    loud message.

    **``redact`` is refused for VAULT-tier notes rather than applied**, and that
    asymmetry is load-bearing. Vault-tier files are file-authoritative (see
    :func:`_sensitivity_from_frontmatter`), so storing a redacted body while the
    user's file still holds the secret makes the DB and the file disagree — and
    on the next pass the file wins and the redaction silently evaporates. A
    redaction that undoes itself is worse than none, because it reports success.

    The two ways out are "rewrite the user's file" or "refuse", and refusing is
    right here: sync's remit is mirroring file into DB, and rewriting a user's
    authored PROSE is a categorically larger act than rewriting one generated
    frontmatter field. Refusal is loud, lossless, and reversible — the same
    reasoning that makes ``warn`` the guard's default rather than ``redact``.
    The user redacts the file themselves and syncs again.

    Ingested-tier bodies are generated mirrors, so redaction there is coherent
    and is applied normally.
    """
    try:
        outcome = apply_guard(
            content,
            mode=mode,
            allow=_frontmatter_allows_secrets(metadata),
            title=title,
        )
    except SecretGuardError as exc:
        # ``reject`` mode. Converted to ``_SyncError`` HERE — this is the whole
        # point of the function. Letting it propagate would escape both per-file
        # handlers and abort the entire walk on one offending note.
        #
        # The message is REBUILT rather than reused: ``SecretGuardError``'s text
        # is the multi-line CLI block, and it must start with
        # ``_GUARD_REFUSAL_PREFIX`` for the refusal counter to recognize it. The
        # finding details are deliberately dropped — ``report.errors`` is
        # rendered by callers into logs and terminals, and echoing the previews
        # there would spread the credential further than the file it came from.
        raise _SyncError(
            f"{_GUARD_REFUSAL_PREFIX} refusing to sync this note "
            f"(BRAIN_SECRET_GUARD=reject). Remove the credential from the file, "
            f"or add `allow_secrets: true` to its frontmatter if the note is "
            f"legitimately about credentials."
        ) from exc
    if outcome.findings and mode == "warn":
        logger.warning(
            "vault sync: %d secret finding(s) in %r — stored UNCHANGED "
            "(BRAIN_SECRET_GUARD=warn)",
            len(outcome.findings),
            title,
        )
    if outcome.redacted and tier == "vault":
        raise _SyncError(
            f"{_GUARD_REFUSAL_PREFIX} refusing to redact an authored vault note "
            f"({len(outcome.findings)} finding(s)). Redacting here would store "
            f"a clean body while your file keeps the secret, and the file wins "
            f"on the next sync — so the redaction would silently undo itself. "
            f"Edit the file to remove the secret, then sync again."
        )
    return outcome.content


def _sensitivity_from_frontmatter(
    frontmatter: dict[str, Any],
    *,
    tier: str,
    current: str,
    path: Path,
) -> str:
    """Resolve the sensitivity tier a synced file should carry (F6).

    **Tier split, mirroring :func:`_build_metadata`'s.** For a VAULT-tier note
    the file is the source of truth, so the frontmatter value wins — including
    removing the line, which returns the note to ``normal``. That is a
    deliberate edit of the user's own note and is exactly the round-trip F6
    documents.

    For an INGESTED-tier row the DB is authoritative, so ``current`` is returned
    unchanged and the mirror's value is ignored. This is not symmetry for its own
    sake: ``_ingested/`` files are GENERATED, and a mirror can legitimately be
    stale (written before the tier changed, restored from a backup, or
    hand-copied). Honouring a stale mirror would let a background
    ``vault sync --watch`` pass silently DOWNGRADE a confidential document — the
    same egress hole the escalate-only rule closes on the re-ingest path.
    Downgrading an ingested document stays an explicit act (``brain mark-normal``).

    An unrecognized value is COERCED to ``normal`` and logged at WARNING; it
    never raises. One hand-typed typo must not abort a ``vault sync --watch``
    pass over the whole corpus. Coercion is one-way by construction —
    :func:`~brain.sensitivity.normalize_level` only ever lands on ``normal``, so
    a typo can cost protection the user intended but can never fabricate
    protection they did not.
    """
    if tier != "vault":
        return current

    raw = frontmatter.get("sensitivity")
    level = normalize_level(raw, strict=False)
    if raw not in (None, "", level):
        logger.warning(
            "vault note %s has invalid sensitivity %r — coerced to %r",
            path,
            raw,
            level,
        )
    return level


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
    frontmatter: dict[str, Any], tier: str, *, path: str
) -> str | None:
    """Extract the ``source`` field for ingested-tier files.

    Vault-tier files don't have a source, so we return ``None`` regardless of
    what the YAML says. For ingested-tier the file names its own source, and
    the value is checked against :data:`brain.source_kinds.VALID_SOURCE_KINDS`.
    Missing, non-string, and UNRECOGNIZED all return ``None``: the row gets
    ``source_id=NULL``, which still lets ``brain show`` and ``brain search``
    work; only ``[[<source>:<external_id>]]`` resolution misses it.

    **This docstring used to advertise an open set** ("krisp / slack / gmail /
    manual / ..."). The ellipsis was aspirational, not a contract — nothing
    depended on it, and it could not have worked:
    :data:`brain.vault.links._SOURCE_KINDS` parses ``[[<kind>:<id>]]`` against
    the same closed four, so a fifth kind produced a ``sources`` row that no
    wiki-link could ever address — while the sole documented purpose of writing
    that row (see :func:`_insert_document`) is to make exactly that link
    resolve. The row was unreachable by construction.

    **Why an unknown kind is DROPPED rather than rejected or substituted.**
    Three routes were available and two are worse:

    * *Reject the file* (raise ``_SyncError``) puts it in ``report.errors`` —
      which ``mcp_server`` escalates to ``INTERNAL_ERROR`` and ``cli_note`` to
      exit 1. One mistyped ``source:`` would then fail a note whose body
      indexed perfectly, and across a whole-tree ``vault sync --watch`` pass it
      makes every affected note's content unsearchable over a metadata typo.
    * *Fall back to* ``manual`` fabricates provenance the user never wrote, and
      files the document under a real kind — the precise shape of the bug
      :data:`brain.facets.SOURCE_NONE_BUCKET` documents, where
      ``coalesce(s.kind, 'manual')`` produced "a wrong answer that looked
      right".
    * *Drop to* ``None`` collapses into the degradation this function already
      performs for a missing ``source:``, and it is one-way by construction: it
      can cost a source link the user intended, but can never invent an
      association they did not write.

    That is the same ruling, for the same reason, as
    :func:`_sensitivity_from_frontmatter` a few functions below — coerce, warn,
    never abort the walk — on a field whose blast radius is strictly larger.

    What it costs, stated rather than claimed away: the write is only consulted
    on INSERT (see the call site), so fixing the typo later does not
    retroactively create the source row. That limitation is pre-existing and
    identical for a ``source:`` line that was simply absent at creation; it is
    not introduced here.
    """
    if tier != "ingested":
        return None
    raw = frontmatter.get("source")
    if not isinstance(raw, str) or not raw.strip():
        return None
    source = raw.strip()
    if source not in VALID_SOURCE_KINDS:
        # CEILING RECORD (1 of 2 — see the module docstring for the pointer):
        # this function is the growth, in a file already over 800 lines.
        # ``sources.kind`` is bare ``TEXT NOT NULL`` with no CHECK, and this was
        # the last of three write boundaries where an unvalidated string reached
        # it. The other two (``cli_ingest.ingest_stdin``,
        # ``mcp_server.brain_ingest_stdin``) are already closed, and 2-of-3 is a
        # worse resting state than 0-of-3: a reader who checks either closed path
        # concludes the whole class is handled.
        logger.warning(
            "vault sync: %s declares source %r, which is not one of %s — "
            "indexing the document with no source row",
            path,
            source,
            source_kinds_hint(),
        )
        return None
    return source


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
    sensitivity: str = DEFAULT_SENSITIVITY,
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
    # ``updated_at`` is absent by design — migration 025's ``DEFAULT NOW()``
    # stamps a brand-new row.
    conn.execute(
        """
        INSERT INTO documents
          (id, title, content, content_hash, content_type, tags,
           metadata, kind, vault_path, source_id, sensitivity)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
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
            sensitivity,
        ),
    )
    _embed_and_insert_chunks(
        conn,
        embedder=embedder,
        document_id=document_id,
        content=content,
        title=title,
        tags=tags,
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
    sensitivity: str = DEFAULT_SENSITIVITY,
) -> None:
    """Update an existing ``documents`` row + (when body changed) re-chunk.

    Bumps ``updated_at``: editing a note in the vault and letting the watcher
    reconcile it is the single most common way a document actually changes.
    """
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
          vault_path = %s,
          sensitivity = %s,
          ingested_at = NOW(),
          updated_at = NOW()
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
            sensitivity,
            document_id,
        ),
    )
    if body_changed:
        conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
        _embed_and_insert_chunks(
            conn,
            embedder=embedder,
            document_id=document_id,
            content=content,
            title=title,
            tags=tags,
        )
    else:
        # Body unchanged but the parent doc's title / tags may have moved
        # (frontmatter-only edit). Migration 009 denormalizes both onto
        # every chunk for the weighted tsv, so propagate the change to the
        # existing chunks. The IS DISTINCT FROM guards inside the helper
        # make this a free no-op when neither column actually moved.
        sync_chunk_search_metadata(conn, document_id)


def _embed_and_insert_chunks(
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder,
    document_id: str,
    content: str,
    title: str,
    tags: list[str],
) -> None:
    """Chunk + embed ``content`` and INSERT the resulting chunks.

    Empty / whitespace-only bodies produce zero chunks; the document row
    still exists (so ``brain show`` works) but is unsearchable until the user
    adds content. Matches the existing ingest pipeline's behavior.

    ``title`` and ``tags`` populate the migration-009 denormalized
    ``chunks.title_text`` / ``chunks.tags_text`` columns so the weighted
    FTS tsvector (title at weight A, tags at weight B) ranks vault-tier
    title/tag hits ahead of body hits — same contract as the ingest
    pipeline.
    """
    chunks = chunk_text(content, count_tokens=embedder.count_tokens)
    if not chunks:
        return
    # FTS-only backend (BRAIN_EMBEDDER=none / NullEmbedder): store NULL vectors
    # rather than calling embed(), which raises. Duck-typed on
    # ``produces_embeddings`` so the real backends — which never declare the
    # flag — are untouched. Mirrors ``brain.ingest._embed_chunks``; without it
    # the two write paths disagreed and `brain capture` died under the
    # documented zero-Ollama setup, which is the first command a new user runs.
    embeddings: list[list[float] | None]
    if not getattr(embedder, "produces_embeddings", True):
        embeddings = [None] * len(chunks)
    else:
        embeddings = list(
            embedder.embed([c.content for c in chunks], input_type="document")
        )
    title_text, tags_text = _chunk_search_metadata(title, tags)
    for c, vec in zip(chunks, embeddings, strict=True):
        conn.execute(
            "INSERT INTO chunks (document_id, chunk_index, content, embedding, "
            "title_text, tags_text, search_extras) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                document_id,
                c.index,
                c.content,
                vec,
                title_text,
                tags_text,
                extract_sub_tokens(c.content),
            ),
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
) -> list[str]:
    """Find vault-tier rows whose ``vault_path`` was NOT seen and prune/warn.

    Dry-run reports the planned action via the report counters but never
    writes — and never logs a warning either, since dry-run is contractually
    "read-only" (no DB writes, no FS writes, no log noise the user wasn't
    asking for). The counters in the returned report still reflect what
    *would* happen.

    Returns the list of document ids that were ACTUALLY deleted this pass
    (``prune`` and not ``dry_run``) so the caller can drop them from the
    people graph (wave G1-c). Empty on warn-only / dry-run.
    """
    rows = conn.execute(
        "SELECT id::text, vault_path, title FROM documents "
        "WHERE kind = 'vault' AND vault_path IS NOT NULL"
    ).fetchall()
    pruned_ids: list[str] = []
    for doc_id, vault_path_value, title in rows:
        if vault_path_value in seen_relative:
            continue
        if prune:
            if not dry_run:
                conn.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
                pruned_ids.append(str(doc_id))
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
    return pruned_ids


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
    # always agree. A divergence (parser drift after an upgrade, a manual DB
    # edit, a migration) must NOT abort a foreground ``brain vault sync``, and
    # a bare ``assert`` here would (and is silently stripped under ``python
    # -O``, making the behavior inconsistent). Log it and continue: the caller
    # inserts the STORED ``display_text``, so no alias is dropped either way.
    if only.display_text is not None and only.display_text != display_text:
        logger.warning(
            "vault sync: unresolved link %r re-parsed display text %r differs "
            "from stored %r; using the stored display text and continuing",
            link_text,
            only.display_text,
            display_text,
        )
    if link_kind not in {"wiki", "embed"}:
        return None
    return only
