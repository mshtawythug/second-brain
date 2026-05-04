"""Collapse legacy per-message Gmail rows into one merged thread row each.

Phase 2.4 of the wiki UX overhaul. Before P2.3 every Gmail message was its
own ``content_type='email'`` row; after P2.3 new ingests assemble whole
threads into a single ``content_type='email_thread'`` row keyed by
``thread_id``. This script does the *destructive* one-way migration of the
existing per-message corpus into the thread-keyed shape:

For each Gmail thread that currently has ≥2 ``content_type='email'`` rows:

1. Re-fetch the full thread via the ``gws`` CLI and assemble one merged
   :class:`brain.ingest.ExtractedDoc` via :func:`to_extracted_thread`.
2. Compute the tag union over every old per-message row, normalized via
   :func:`brain.tags.normalize_tags`.
3. Insert the merged thread doc through :func:`ingest_document` (P2.2's
   thread upsert path picks the right shape automatically). The old
   per-message rows still exist at this point so referrers don't dangle.
4. Retarget every ``links`` / ``derived_links`` row whose
   ``dst_document_id`` was an old per-message id onto the merged doc id.
   Use INSERT-ON-CONFLICT-DO-NOTHING + DELETE so a UNIQUE collision (two
   old siblings shared an outbound edge) collapses to one edge instead
   of failing the whole transaction.
5. Drop ``unresolved_links`` whose ``link_text`` matched an old slug or
   old title — they're now resolved by the merged doc.
6. Walk the vault and rewrite every ``[[old-title]]`` / ``[[old-slug]]``
   reference onto ``[[<merged-title>]]`` via
   :func:`brain.vault.rename.collect_references` +
   :func:`brain.vault.rename.apply_matches_to_text`. Reuses the rename
   helper rather than duplicating the link parser / synthetic-display
   drop / heading-anchor logic.
7. ``DELETE`` the old per-message rows. Cascade drops their chunks (and
   any inbound src-side edges from them — the relink pass owns rebuilding
   those). ``Path.unlink(missing_ok=True)`` removes the old vault mirror
   files.
8. Verify no surviving ``links`` / ``derived_links`` row references a
   deleted document. If any do, abort the whole run with a non-zero
   exit code.
9. After every thread is collapsed, recompute Gmail
   ``directory_entries`` rows from scratch (delete + rescan) so
   ``occurrence_count`` reflects the surviving thread docs only.

``--dry-run`` prints the per-thread plan + summary without touching the
DB or filesystem. The live destructive execution requires a fresh DB
backup and explicit user authorization — the coordinator owns both.

Standalone script (not a ``brain`` subcommand) — invoked as::

    python scripts/collapse_gmail_threads.py --dry-run

A run with ``--dry-run`` is read-only; without it, every per-thread
transaction is committed independently so a mid-run crash leaves the
corpus partially migrated but never half-deleted within a thread. Re-run
the script (with or without ``--dry-run``) on a partially-migrated
corpus to finish the job — singletons are skipped, so any thread that
made it through is left alone.
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

from brain.config import Config, ConfigError
from brain.db import connect
from brain.embeddings import make_embedder
from brain.errors import BrainError
from brain.ingest import Embedder, ingest_document
from brain.ingest.gmail import GmailError, Runner, read_message, to_extracted_thread
from brain.tags import normalize_tags
from brain.vault.derived_links.directory import rescan_gmail_directory
from brain.vault.rename import (
    ReferenceMatch,
    apply_matches_to_text,
    collect_references,
)

_logger = logging.getLogger("brain.scripts.collapse_gmail_threads")


# ---------------------------------------------------------------------------
# Internal data carriers — populated by :func:`_load_thread_groups` and
# consumed by :func:`collapse_threads`. Kept private (``_`` prefix) because
# callers outside the script have no business poking at them; the public
# surface is :func:`collapse_threads` + :func:`main`.
# ---------------------------------------------------------------------------


@dataclass
class _OldMessage:
    """One row of an existing ``content_type='email'`` gmail document."""

    document_id: str
    title: str
    tags: list[str]
    message_id: str | None
    vault_path: str | None  # documents.vault_path (relative POSIX path) or None


@dataclass
class _ThreadGroup:
    """All per-message rows that belong to one Gmail thread."""

    thread_id: str
    messages: list[_OldMessage]

    @property
    def old_doc_ids(self) -> list[str]:
        return [m.document_id for m in self.messages]

    @property
    def old_message_ids(self) -> list[str]:
        return [m.message_id for m in self.messages if m.message_id]

    def union_tags(self) -> list[str]:
        """Sorted normalized union of every old message's tags."""
        merged: list[str] = []
        for msg in self.messages:
            merged.extend(msg.tags)
        return sorted(normalize_tags(merged))

    def old_title_targets(self) -> list[tuple[str, str]]:
        """``(old_title, old_path_stem)`` pairs for vault link refactor.

        ``old_path_stem`` is the POSIX path of the old mirror with the
        ``.md`` suffix stripped. Rows with no ``vault_path`` (a stale
        ingest that never produced a mirror) contribute only the title
        form so we still catch ``[[old-title]]`` references.
        """
        pairs: list[tuple[str, str]] = []
        for msg in self.messages:
            stem = ""
            if msg.vault_path and msg.vault_path.endswith(".md"):
                stem = msg.vault_path[: -len(".md")]
            elif msg.vault_path:
                stem = msg.vault_path
            pairs.append((msg.title, stem))
        return pairs


@dataclass
class ThreadReport:
    """Outcome of collapsing one Gmail thread.

    Surfaced in the run report. ``error`` is non-None when the per-thread
    transaction was rolled back; the script exits non-zero whenever any
    thread reports an error.
    """

    thread_id: str
    title: str
    msg_count_before: int
    msg_count_after: int  # always 1 on success, 0 on failure
    tag_union_count: int
    refs_rewritten: int
    db_links_rewritten: int
    db_derived_rewritten: int
    db_unresolved_dropped: int
    vault_files_unlinked: int
    error: str | None = None


@dataclass
class CollapseReport:
    """Outcome of one ``collapse_gmail_threads`` invocation."""

    dry_run: bool
    processed: list[ThreadReport] = field(default_factory=list)
    skipped_singletons: int = 0
    failed: list[ThreadReport] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.processed if r.error is None)

    @property
    def failure_count(self) -> int:
        return len(self.failed)


# ---------------------------------------------------------------------------
# Read side — gather thread groups from the live DB.
# ---------------------------------------------------------------------------


def _load_thread_groups(conn: psycopg.Connection[Any]) -> list[_ThreadGroup]:
    """Return every Gmail thread group that has ≥2 per-message rows.

    Reads ``content_type='email'`` rows joined with their ``sources`` row
    so we have ``thread_id`` (preferring the typed column over the JSONB
    blob; the typed column is backfilled by P1.4) and ``message_id``
    (used as the ``read_message`` argument when re-fetching). Rows with
    no resolvable ``thread_id`` are dropped — there's nothing to group on.

    Singleton threads (one per-message row) are filtered here too — the
    spec leaves them as-is so a re-run on an already-collapsed corpus is
    a no-op.
    """
    rows = conn.execute(
        """
        SELECT d.id::text,
               d.title,
               d.tags,
               coalesce(d.thread_id, d.metadata ->> 'thread_id') AS thread_id,
               coalesce(d.metadata ->> 'message_id', s.external_id) AS message_id,
               d.vault_path
        FROM documents d
        LEFT JOIN sources s ON s.id = d.source_id
        WHERE d.kind = 'ingested'
          AND d.content_type = 'email'
          AND s.kind = 'gmail'
        ORDER BY d.id
        """
    ).fetchall()

    grouped: dict[str, list[_OldMessage]] = defaultdict(list)
    for doc_id, title, tags, thread_id, message_id, vault_path in rows:
        if not thread_id:
            continue
        grouped[str(thread_id)].append(
            _OldMessage(
                document_id=str(doc_id),
                title=str(title),
                tags=list(tags or []),
                message_id=str(message_id) if message_id else None,
                vault_path=str(vault_path) if vault_path else None,
            )
        )

    return [
        _ThreadGroup(thread_id=tid, messages=msgs)
        for tid, msgs in sorted(grouped.items())
        if len(msgs) >= 2
    ]


# ---------------------------------------------------------------------------
# DB-side retarget helpers — all wrapped in INSERT-ON-CONFLICT-DO-NOTHING +
# DELETE so a UNIQUE collision on the destination row collapses cleanly
# instead of breaking the per-thread transaction.
# ---------------------------------------------------------------------------


def _retarget_links(
    conn: psycopg.Connection[Any], *, new_id: str, old_ids: list[str]
) -> int:
    """Move every ``links`` row whose dst was an old id onto ``new_id``.

    Returns the number of rows that originally pointed at one of the old
    ids (the count includes any that collapsed into existing edges via
    ON CONFLICT DO NOTHING — that's what the user wants surfaced). Two
    old ids that shared an outbound edge from the same src + link_text
    + link_kind would otherwise collide on the (src, dst, link_text,
    link_kind) UNIQUE — the INSERT-then-DELETE pattern keeps us safe.
    """
    before = conn.execute(
        "SELECT count(*) FROM links WHERE dst_document_id = ANY(%s::uuid[])",
        (old_ids,),
    ).fetchone()
    assert before is not None  # count(*) always yields a row
    conn.execute(
        """
        INSERT INTO links (src_document_id, dst_document_id, link_text, link_kind, display_text)
        SELECT src_document_id, %s::uuid, link_text, link_kind, display_text
        FROM links
        WHERE dst_document_id = ANY(%s::uuid[])
        ON CONFLICT (src_document_id, dst_document_id, link_text, link_kind)
        DO NOTHING
        """,
        (new_id, old_ids),
    )
    conn.execute(
        "DELETE FROM links WHERE dst_document_id = ANY(%s::uuid[])",
        (old_ids,),
    )
    return int(before[0])


def _retarget_derived_links(
    conn: psycopg.Connection[Any], *, new_id: str, old_ids: list[str]
) -> int:
    """Same shape as :func:`_retarget_links` but for ``derived_links``.

    The UNIQUE on derived_links is ``(src, dst, rule)`` — two old ids
    sharing the same rule from the same src would otherwise collide on
    the UPDATE. INSERT-with-DO-NOTHING + DELETE handles it cleanly.
    """
    before = conn.execute(
        "SELECT count(*) FROM derived_links "
        "WHERE dst_document_id = ANY(%s::uuid[]) "
        "  AND src_document_id <> %s::uuid",
        (old_ids, new_id),
    ).fetchone()
    assert before is not None  # count(*) always yields a row
    conn.execute(
        """
        INSERT INTO derived_links (src_document_id, dst_document_id, rule, evidence, weight)
        SELECT src_document_id, %s::uuid, rule, evidence, weight
        FROM derived_links
        WHERE dst_document_id = ANY(%s::uuid[])
          AND src_document_id <> %s::uuid
        ON CONFLICT (src_document_id, dst_document_id, rule)
        DO NOTHING
        """,
        (new_id, old_ids, new_id),
    )
    conn.execute(
        "DELETE FROM derived_links WHERE dst_document_id = ANY(%s::uuid[])",
        (old_ids,),
    )
    return int(before[0])


def _drop_unresolved_links(
    conn: psycopg.Connection[Any], *, link_texts: list[str]
) -> int:
    """Delete unresolved_links rows whose link_text matches an old slug/title.

    Returns the number of rows deleted. The next sync pass re-resolves
    surviving references through the merged thread doc.
    """
    if not link_texts:
        return 0
    row = conn.execute(
        "DELETE FROM unresolved_links WHERE link_text = ANY(%s) RETURNING id",
        (link_texts,),
    ).fetchall()
    return len(row)


def _verify_no_dangling_fk(
    conn: psycopg.Connection[Any], *, deleted_ids: list[str]
) -> None:
    """Raise BrainError if any links/derived_links row still points at a deleted doc.

    Belt-and-suspenders against an unfilled retarget branch. Should never
    fire under normal operation: the FK CASCADE on documents would have
    dropped the row when its parent disappeared, so a surviving
    ``WHERE dst_document_id = ANY(deleted_ids)`` is a real bug.
    """
    if not deleted_ids:
        return
    bad_links = conn.execute(
        "SELECT count(*) FROM links WHERE dst_document_id = ANY(%s::uuid[])",
        (deleted_ids,),
    ).fetchone()
    bad_derived = conn.execute(
        "SELECT count(*) FROM derived_links "
        "WHERE dst_document_id = ANY(%s::uuid[])",
        (deleted_ids,),
    ).fetchone()
    assert bad_links is not None and bad_derived is not None
    if bad_links[0] or bad_derived[0]:
        raise BrainError(
            f"verification failed after collapse: "
            f"{bad_links[0]} links + {bad_derived[0]} derived_links rows still "
            f"point at deleted document(s) {deleted_ids}"
        )


# ---------------------------------------------------------------------------
# Vault refactor — group matches by file, splice back-to-front per file.
# ---------------------------------------------------------------------------


def _apply_vault_rewrites(
    matches: list[ReferenceMatch],
) -> int:
    """Splice every match into its file. Returns reference count rewritten.

    Groups by file path (parser yields document order) so a single file's
    matches all hit the same file read + write cycle. Backed by
    :func:`apply_matches_to_text` from ``brain.vault.rename`` — the same
    splice logic the rename CLI uses.
    """
    grouped: dict[Path, list[ReferenceMatch]] = defaultdict(list)
    for m in matches:
        grouped[m.file_path].append(m)
    total = 0
    for path, file_matches in grouped.items():
        text = path.read_text(encoding="utf-8")
        new_text = apply_matches_to_text(text, file_matches)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            total += len(file_matches)
    return total


# ---------------------------------------------------------------------------
# Per-thread orchestration — collapse one ``_ThreadGroup`` end-to-end.
# ---------------------------------------------------------------------------


def _collapse_thread(
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder,
    runner: Runner | None,
    vault_path: Path | None,
    group: _ThreadGroup,
) -> ThreadReport:
    """Collapse one thread; run the full pipeline inside one transaction.

    Returns a :class:`ThreadReport` on either success or per-thread failure.
    A failure path rolls back the per-thread transaction (the conn was
    autocommit before this call; we open an explicit transaction here) and
    populates ``report.error`` so the caller can surface it.

    Any post-commit work (vault file unlinks, vault link rewrites) happens
    after the DB transaction commits — by then the merged doc + retargeted
    refs are durable in the DB. A filesystem failure here is logged and
    reported but does NOT roll back the DB ingest (drift is recoverable
    via ``brain vault prune-orphans`` + ``brain vault sync``).
    """
    report = ThreadReport(
        thread_id=group.thread_id,
        title=group.messages[0].title,
        msg_count_before=len(group.messages),
        msg_count_after=0,
        tag_union_count=0,
        refs_rewritten=0,
        db_links_rewritten=0,
        db_derived_rewritten=0,
        db_unresolved_dropped=0,
        vault_files_unlinked=0,
    )

    # Re-fetch every message in the thread via the gws CLI. Failure of
    # any single fetch aborts the thread (we never want a partial
    # assembly).
    try:
        messages: list[dict[str, Any]] = []
        for old in group.messages:
            if old.message_id is None:
                raise GmailError(
                    f"old document {old.document_id} has no message_id; "
                    "skip the thread until ingest backfills the metadata"
                )
            messages.append(read_message(old.message_id, runner=runner))
        merged_doc = to_extracted_thread(messages)
    except (GmailError, ValueError) as exc:
        report.error = f"thread fetch/assemble failed: {exc}"
        return report

    union_tags = group.union_tags()
    report.tag_union_count = len(union_tags)
    report.title = merged_doc.title

    # Save vault paths BEFORE the DB delete so the post-commit unlink
    # step still has them. ``documents.vault_path`` is dropped on cascade.
    old_vault_paths = [m.vault_path for m in group.messages if m.vault_path]
    old_link_texts: list[str] = []
    for m in group.messages:
        if m.title:
            old_link_texts.append(m.title)
        if m.vault_path and m.vault_path.endswith(".md"):
            old_link_texts.append(m.vault_path[: -len(".md")])

    # Per-thread DB transaction — commit makes the collapse durable, any
    # exception rolls back so the corpus stays in its pre-call state.
    try:
        with conn.transaction():
            ingest_result = ingest_document(
                conn,
                embedder=embedder,
                doc=merged_doc,
                source_kind="gmail",
                tags=union_tags,
                # Don't pass vault_root here — the merged doc's mirror is
                # written AFTER the per-thread DB commit (post-commit), via
                # an explicit ``regenerate_vault_file`` call. That keeps the
                # DB transaction tight and avoids file-system writes inside
                # the long-running per-thread tx.
            )
            new_id = ingest_result.document_id
            if new_id is None:
                raise BrainError(
                    "ingest_document returned no document_id — empty body?"
                )
            old_ids = [m.document_id for m in group.messages]
            # Don't retarget references that point at the *new* doc onto
            # itself (defensive — the new id is freshly minted so this
            # never happens in practice, but a future mirror-of-self edge
            # would otherwise hit a self-referential UNIQUE row).
            old_ids_no_self = [oid for oid in old_ids if oid != new_id]
            report.db_links_rewritten = _retarget_links(
                conn, new_id=new_id, old_ids=old_ids_no_self
            )
            report.db_derived_rewritten = _retarget_derived_links(
                conn, new_id=new_id, old_ids=old_ids_no_self
            )
            report.db_unresolved_dropped = _drop_unresolved_links(
                conn, link_texts=old_link_texts
            )
            conn.execute(
                "DELETE FROM documents WHERE id = ANY(%s::uuid[])",
                (old_ids_no_self,),
            )
            _verify_no_dangling_fk(conn, deleted_ids=old_ids_no_self)
            report.msg_count_after = 1
    except (psycopg.Error, BrainError) as exc:
        report.error = f"per-thread transaction failed: {exc}"
        return report

    # Post-commit: vault refactor + mirror unlink + merged-doc mirror write.
    # A filesystem failure here is logged + counted but does NOT roll back
    # the DB collapse — drift is recoverable via ``brain vault sync``.
    if vault_path is not None:
        try:
            matches = collect_references(
                vault_path,
                old_targets=group.old_title_targets(),
                new_title=merged_doc.title,
            )
            report.refs_rewritten = _apply_vault_rewrites(matches)
        except OSError as exc:
            _logger.warning(
                "thread %s: vault refactor failed (%s); DB collapse stands; "
                "recover via `brain vault sync`",
                group.thread_id,
                exc,
            )
        for rel_path in old_vault_paths:
            target = vault_path / rel_path
            try:
                target.unlink(missing_ok=True)
                report.vault_files_unlinked += 1
            except OSError as exc:
                _logger.warning(
                    "thread %s: failed to unlink old mirror %s: %s",
                    group.thread_id,
                    target,
                    exc,
                )
        # Materialize the merged doc's mirror file. Importing here so the
        # script still works for callers that don't pass vault_path.
        from brain.vault.export import regenerate_vault_file

        try:
            regenerate_vault_file(
                conn, new_id, vault_path=vault_path, force=True
            )
        except (OSError, ValueError) as exc:
            _logger.warning(
                "thread %s: failed to materialize merged mirror for %s: %s",
                group.thread_id,
                new_id,
                exc,
            )

    return report


# ---------------------------------------------------------------------------
# Dry-run path — pure read; no DB writes, no filesystem writes.
# ---------------------------------------------------------------------------


def _dry_run_thread(
    conn: psycopg.Connection[Any], group: _ThreadGroup
) -> ThreadReport:
    """Build a thread report without touching the DB or filesystem.

    Computes the same counters the live path would surface (tag union,
    estimated link / derived_links retarget counts) so the user can
    review the plan before authorizing the destructive run.
    """
    union_tags = group.union_tags()
    old_ids = group.old_doc_ids
    link_count = conn.execute(
        "SELECT count(*) FROM links WHERE dst_document_id = ANY(%s::uuid[])",
        (old_ids,),
    ).fetchone()
    derived_count = conn.execute(
        "SELECT count(*) FROM derived_links "
        "WHERE dst_document_id = ANY(%s::uuid[])",
        (old_ids,),
    ).fetchone()
    old_link_texts: list[str] = []
    for m in group.messages:
        if m.title:
            old_link_texts.append(m.title)
        if m.vault_path and m.vault_path.endswith(".md"):
            old_link_texts.append(m.vault_path[: -len(".md")])
    unresolved_count = (
        conn.execute(
            "SELECT count(*) FROM unresolved_links WHERE link_text = ANY(%s)",
            (old_link_texts,),
        ).fetchone()
        if old_link_texts
        else None
    )
    assert link_count is not None and derived_count is not None
    vault_files_planned = sum(1 for m in group.messages if m.vault_path)
    return ThreadReport(
        thread_id=group.thread_id,
        title=group.messages[0].title,
        msg_count_before=len(group.messages),
        msg_count_after=1,
        tag_union_count=len(union_tags),
        refs_rewritten=0,  # vault scan deferred to live path
        db_links_rewritten=int(link_count[0]),
        db_derived_rewritten=int(derived_count[0]),
        db_unresolved_dropped=int(unresolved_count[0]) if unresolved_count else 0,
        vault_files_unlinked=vault_files_planned,
    )


# ---------------------------------------------------------------------------
# Top-level orchestration.
# ---------------------------------------------------------------------------


def collapse_threads(
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder | None,
    runner: Runner | None = None,
    vault_path: Path | None = None,
    dry_run: bool = False,
) -> CollapseReport:
    """Collapse every Gmail thread group of ≥2 per-message rows.

    Live mode: ``embedder`` is required; per-thread DB transactions
    commit independently. A per-thread failure populates the report's
    ``failed`` list but does NOT abort the run — surviving threads still
    get collapsed. The caller decides what to do with a non-empty
    ``failed`` list (the script CLI exits non-zero).

    Dry-run mode: ``embedder`` may be ``None``; the function computes the
    planned report without writing anything. ``vault_path`` is consulted
    only for ``vault_files_unlinked`` planning (count of old mirror
    files); the actual scan is deferred to the live path.

    After all threads collapse successfully (and only in non-dry-run
    mode), the Gmail directory is rebuilt: ``directory_entries`` rows
    with ``source='gmail'`` are dropped and re-derived from the surviving
    merged thread docs. This is the "recompute from scratch" rule —
    cheaper and safer than incremental delta math at our corpus scale.
    """
    report = CollapseReport(dry_run=dry_run)
    groups = _load_thread_groups(conn)
    _logger.info("found %d thread group(s) with ≥2 per-message rows", len(groups))
    if not groups:
        return report

    for group in groups:
        if dry_run:
            thread_report = _dry_run_thread(conn, group)
            report.processed.append(thread_report)
            continue
        if embedder is None:
            raise ValueError("embedder is required in non-dry-run mode")
        thread_report = _collapse_thread(
            conn,
            embedder=embedder,
            runner=runner,
            vault_path=vault_path,
            group=group,
        )
        if thread_report.error is not None:
            report.failed.append(thread_report)
        else:
            report.processed.append(thread_report)

    # Corpus-wide directory_entries recompute. Only safe in live mode
    # AND only when no thread failed — a partial collapse leaves the
    # directory inconsistent until the user re-runs.
    if not dry_run and not report.failed and report.processed:
        try:
            with conn.transaction():
                conn.execute(
                    "DELETE FROM directory_entries WHERE source = 'gmail'"
                )
            docs_seen, pairs = rescan_gmail_directory(conn)
            _logger.info(
                "directory recompute: %d gmail docs, %d (display, email) pairs",
                docs_seen,
                pairs,
            )
        except psycopg.Error as exc:
            # Logged + surfaced via stderr; the collapse itself stands.
            _logger.warning(
                "directory_entries recompute failed (%s); "
                "run `brain vault directory refresh` manually",
                exc,
            )

    return report


# ---------------------------------------------------------------------------
# CLI plumbing — argparse + report formatting + exit codes.
# ---------------------------------------------------------------------------


def _format_thread_line(r: ThreadReport) -> str:
    """One-line per-thread summary used in the printed report."""
    base = (
        f"thread {r.thread_id}: {r.msg_count_before} → {r.msg_count_after} "
        f"({r.title!r}) "
        f"tags={r.tag_union_count} refs={r.refs_rewritten} "
        f"links={r.db_links_rewritten} derived={r.db_derived_rewritten} "
        f"unresolved-dropped={r.db_unresolved_dropped} "
        f"files-unlinked={r.vault_files_unlinked}"
    )
    if r.error is not None:
        base = f"FAILED {base} error={r.error}"
    return base


def _format_report(report: CollapseReport) -> str:
    """Render the full :class:`CollapseReport` as the printed output."""
    lines: list[str] = []
    if report.dry_run:
        lines.append("Collapse report (DRY RUN — nothing written):")
    else:
        lines.append("Collapse report:")
    if not report.processed and not report.failed:
        lines.append("  no thread groups of ≥2 per-message rows")
        return "\n".join(lines)

    for r in report.processed:
        lines.append(f"  {_format_thread_line(r)}")
    for r in report.failed:
        lines.append(f"  {_format_thread_line(r)}")

    total_msgs = sum(r.msg_count_before for r in report.processed + report.failed)
    total_threads = len(report.processed) + len(report.failed)
    total_links = sum(r.db_links_rewritten for r in report.processed)
    total_derived = sum(r.db_derived_rewritten for r in report.processed)
    total_unresolved = sum(r.db_unresolved_dropped for r in report.processed)
    total_files = sum(r.vault_files_unlinked for r in report.processed)
    if report.dry_run:
        lines.append(
            f"  TOTAL would collapse {total_threads} threads, "
            f"{total_msgs} per-message rows → {total_threads} thread rows; "
            f"would rewrite {total_links} links + {total_derived} derived_links; "
            f"would drop {total_unresolved} unresolved_links; "
            f"would unlink {total_files} files"
        )
    else:
        lines.append(
            f"  TOTAL collapsed {report.success_count} threads "
            f"({total_msgs} per-message rows) "
            f"failed={report.failure_count} "
            f"links={total_links} derived={total_derived} "
            f"unresolved-dropped={total_unresolved} "
            f"files-unlinked={total_files}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Parse args, run the collapse, print the report. Returns shell exit code.

    Exit codes:
    - 0 — clean run (or dry-run).
    - 1 — at least one thread failed in live mode.
    - 2 — config / DB connection error (no work attempted).
    - 3 — :class:`BrainError` raised before per-thread loop (e.g. bad SQL).
    """
    parser = argparse.ArgumentParser(
        description=(
            "Destructively collapse legacy per-message Gmail rows into "
            "merged-thread rows. Use --dry-run for a read-only plan."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned collapse without touching DB or filesystem.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="DEBUG-level logging (one line per thread).",
    )
    parser.add_argument(
        "--no-vault",
        action="store_true",
        help=(
            "Skip vault link refactor + mirror unlink. "
            "DB-only collapse; the user is on the hook for cleanup later."
        ),
    )
    args = parser.parse_args(argv)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        cfg = Config.load()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    # Embedder construction may probe Ollama; skip on dry-run so the
    # plan still prints when Ollama is offline.
    embedder: Embedder | None = None if args.dry_run else make_embedder(cfg)
    vault_path: Path | None = None if args.no_vault else cfg.vault_path

    try:
        with connect(cfg.database_url) as conn:
            conn.autocommit = True
            report = collapse_threads(
                conn,
                embedder=embedder,
                runner=None,
                vault_path=vault_path,
                dry_run=args.dry_run,
            )
    except BrainError as e:
        print(f"collapse aborted: {e}", file=sys.stderr)
        return 3
    except psycopg.OperationalError as e:
        print(f"database connection failed: {e}", file=sys.stderr)
        return 2

    print(_format_report(report))
    return 1 if report.failure_count else 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    sys.exit(main())
