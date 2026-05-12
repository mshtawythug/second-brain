"""Backfill `summary:` frontmatter into existing vault mirror files.

Wave Q2-SUMMARY-WIKI Item 3.

Q1-D ships ``documents.summary`` and exposes it via ``brain show`` /
MCP ``brain_show``. The vault mirror writer
(``brain.vault.export._build_frontmatter``) was extended in the same
wave to emit ``summary:`` into per-doc frontmatter, but every doc
ingested BEFORE that change has stale on-disk frontmatter — the DB
carries a summary, but the ``.md`` file under ``<vault>/_ingested/``
(or the user-authored vault tier) doesn't yet. This one-shot module
reconciles the on-disk frontmatter for those rows so the Quartz
``SummaryLede`` component has something to render after the next
``brain vault render`` pass.

Idempotent: rerunning on a synced vault is a fast NO-OP (every row
returns ``unchanged``). Non-destructive: only the ``summary:`` key is
mutated — every other frontmatter key (and the file body) round-trips
verbatim through :mod:`brain.vault.frontmatter`. Atomic per file via
:func:`brain.vault._atomic.atomic_write_text` (sibling tempfile +
``os.replace``).

Discovery is the same shape as ``brain enrich --backfill``: keyset
pagination over ``documents.id``, batches yielded to keep the in-memory
footprint bounded. The driver loops over batches until exhaustion.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import yaml

from ._atomic import atomic_write_text
from .frontmatter import dump_frontmatter, parse_frontmatter

_logger = logging.getLogger(__name__)

_BATCH_SIZE = 100


@dataclass
class SyncSummariesReport:
    """Outcome counters for one :func:`sync_summaries` run.

    ``inspected`` counts every row pulled from the DB (the universe of
    ``summary IS NOT NULL AND vault_path IS NOT NULL`` docs);
    ``updated`` / ``unchanged`` / ``missing_file`` / ``errored`` are the
    four mutually-exclusive outcomes per row. ``errored`` carries one
    string per failure so the CLI can surface them at the end of the
    run without aborting the loop mid-corpus.
    """

    inspected: int = 0
    updated: int = 0
    unchanged: int = 0
    missing_file: int = 0
    errored: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class _SummaryRow:
    """Internal projection of one document for the sync loop."""

    id: str
    summary: str
    vault_path: str


def _iter_rows(
    conn: psycopg.Connection,
    *,
    limit: int | None,
    batch_size: int = _BATCH_SIZE,
) -> Iterator[_SummaryRow]:
    """Yield documents that have a summary AND an on-disk vault path.

    Keyset pagination over ``documents.id`` (same shape as
    :func:`brain.queries.iter_unenriched_documents`) keeps memory
    bounded on a large corpus. ``limit`` caps the total emitted across
    all batches — when set, the last batch is shrunk so we stop on the
    exact requested row count.
    """
    last_id: str | None = None
    emitted = 0
    while True:
        if limit is not None and emitted >= limit:
            return
        remaining = batch_size if limit is None else min(batch_size, limit - emitted)
        if last_id is None:
            rows = conn.execute(
                "SELECT id::text, summary, vault_path FROM documents "
                "WHERE summary IS NOT NULL AND vault_path IS NOT NULL "
                "ORDER BY id LIMIT %s",
                (remaining,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id::text, summary, vault_path FROM documents "
                "WHERE summary IS NOT NULL AND vault_path IS NOT NULL "
                "AND id > %s::uuid "
                "ORDER BY id LIMIT %s",
                (last_id, remaining),
            ).fetchall()
        if not rows:
            return
        last_id = str(rows[-1][0])
        for r in rows:
            yield _SummaryRow(
                id=str(r[0]),
                summary=str(r[1]),
                vault_path=str(r[2]),
            )
            emitted += 1


def _rewrite_with_summary(target: Path, summary: str) -> bool:
    """Insert/refresh ``summary:`` on ``target``'s frontmatter atomically.

    Returns ``True`` when the file was rewritten, ``False`` when the
    existing frontmatter already matches (idempotent skip).

    Field order preserved: ``summary`` is inserted immediately after
    ``content_type`` when that key exists (mirrors the order
    :func:`brain.vault.export._build_frontmatter` uses for fresh
    writes), otherwise appended to the end. Existing user-authored keys
    are not reordered.

    Raises :class:`OSError` if reading or writing fails. Raises
    :class:`yaml.YAMLError` or :class:`ValueError` if the existing
    frontmatter is malformed — the caller treats both as "errored" so
    a corrupt file doesn't kill the whole backfill loop.
    """
    text = target.read_text(encoding="utf-8")
    fields, body = parse_frontmatter(text)

    existing = fields.get("summary")
    if isinstance(existing, str) and existing == summary:
        return False

    # Build a fresh ordered dict so the inserted key sits in the
    # canonical slot (after ``content_type`` when present). Re-creating
    # the dict is necessary because Python preserves insertion order —
    # mutating in place would put ``summary`` at the END of the file,
    # which works but reads oddly when compared against a freshly-
    # exported mirror.
    new_fields: dict[str, object] = {}
    inserted = False
    for key, value in fields.items():
        if key == "summary":
            # Drop the stale entry; we'll insert a fresh one below at
            # the canonical position (or here if content_type already
            # passed). If we already inserted, just skip — guards
            # against a duplicate ``summary:`` line ending up in the
            # output.
            if not inserted:
                new_fields["summary"] = summary
                inserted = True
            continue
        new_fields[key] = value
        if key == "content_type" and not inserted:
            new_fields["summary"] = summary
            inserted = True
    if not inserted:
        new_fields["summary"] = summary

    atomic_write_text(target, dump_frontmatter(new_fields, body))
    return True


def sync_summaries(
    conn: psycopg.Connection,
    *,
    vault_root: Path,
    dry_run: bool = False,
    limit: int | None = None,
) -> SyncSummariesReport:
    """Reconcile ``summary:`` frontmatter for every enriched doc on disk.

    Drives the backfill loop: iterate every ``documents`` row that has
    both a ``summary`` and a ``vault_path``, read the corresponding
    file under ``vault_root``, parse its frontmatter, and either
    rewrite (with :func:`_rewrite_with_summary`) or skip when the
    on-disk ``summary:`` already matches.

    ``dry_run=True`` performs every read + comparison but writes
    nothing — the report reflects what WOULD have happened. Useful for
    sanity-checking a backfill before it touches the disk on a large
    corpus.

    ``limit`` caps the total number of rows inspected. The CLI exposes
    this for testing (``--limit 5``) and for incremental drains in case
    a backfill needs to be paced.

    The report's ``missing_file`` counter is bumped when a DB row
    references a ``vault_path`` whose ``.md`` file no longer exists on
    disk (mirror was rm'd manually, vault wiped, etc.). ``errored``
    captures parsing / OS errors per row with a one-line message —
    the loop continues so a single bad file doesn't halt the run.
    """
    report = SyncSummariesReport()

    for row in _iter_rows(conn, limit=limit):
        report.inspected += 1
        target = vault_root / row.vault_path
        if not target.is_file():
            report.missing_file += 1
            _logger.warning(
                "sync-summaries: vault_path missing for %s at %s; "
                "run `brain vault export --force` to recreate",
                row.id,
                target,
            )
            continue

        try:
            # ``parse_frontmatter`` returns ``({}, text)`` on a file
            # with no frontmatter fences — we re-emit with a fresh
            # ``summary:`` block in that case, which adds the fences
            # the file is missing. That's the right thing: the file
            # has a corresponding DB row, so it SHOULD carry the
            # canonical frontmatter shape.
            existing_text = target.read_text(encoding="utf-8")
            fields, _body = parse_frontmatter(existing_text)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            report.errored += 1
            msg = f"{row.vault_path}: parse failed ({exc})"
            report.errors.append(msg)
            _logger.warning("sync-summaries: %s", msg)
            continue

        on_disk_summary = fields.get("summary")
        if isinstance(on_disk_summary, str) and on_disk_summary == row.summary:
            report.unchanged += 1
            continue

        if dry_run:
            report.updated += 1
            continue

        try:
            wrote = _rewrite_with_summary(target, row.summary)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            report.errored += 1
            msg = f"{row.vault_path}: rewrite failed ({exc})"
            report.errors.append(msg)
            _logger.warning("sync-summaries: %s", msg)
            continue

        # ``wrote`` is False only when ``_rewrite_with_summary``'s own
        # idempotency check (re-reading the frontmatter inside the
        # helper) matched — defensive belt-and-suspenders against a
        # race where another writer touched the file between the
        # outer read and the rewrite call.
        if wrote:
            report.updated += 1
        else:
            report.unchanged += 1

    return report
