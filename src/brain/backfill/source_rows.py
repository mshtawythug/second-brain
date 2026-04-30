"""Backfill ``documents.source_id`` for legacy file-ingested rows."""
from dataclasses import dataclass

import psycopg

from ..ingest import _upsert_source


@dataclass
class BackfillReport:
    """Outcome of :func:`backfill_source_rows`.

    ``candidates`` is the number of rows matched by the WHERE clause, BEFORE
    any writes. ``sources_created`` counts new ``sources`` rows inserted by
    this run (existing rows reused via the ``(kind, external_id)`` unique
    pair don't count). ``documents_updated`` is the number of ``documents``
    rows whose ``source_id`` was set by this run. ``dry_run`` mirrors the
    ``commit`` argument: ``True`` means no writes were applied (counts come
    from the same query that would have been used for the live run).
    """

    candidates: int
    sources_created: int
    documents_updated: int
    dry_run: bool


_BACKFILL_SQL = (
    "SELECT id, source_path FROM documents "
    "WHERE source_id IS NULL "
    "  AND content_type = 'markdown' "
    "  AND source_path IS NOT NULL "
    "ORDER BY id"
)


def backfill_source_rows(
    conn: psycopg.Connection, *, commit: bool = True
) -> BackfillReport:
    """Set ``source_id`` to a "manual" sources row for legacy markdown docs.

    Selection: ``content_type = 'markdown'`` AND ``source_id IS NULL`` AND
    ``source_path IS NOT NULL``. For each match, upsert a ``sources`` row
    with ``kind="manual"`` and ``external_id = source_path`` (so two docs
    sharing a path collapse onto one source row), then point the document
    at it.

    Idempotent: a second run is a no-op because the WHERE clause filters
    on ``source_id IS NULL``. The whole pass runs in a single transaction
    — either every row gets fixed or none do. Set ``commit=False`` for a
    preview that returns the candidate counts without writing.

    Stdin-ingested rows (``source_path IS NULL``) are intentionally skipped.
    Their kind cannot be inferred from the path alone — they would need
    a per-source backfill (e.g. krisp re-ingest) instead.
    """
    rows = conn.execute(_BACKFILL_SQL).fetchall()
    candidates = len(rows)

    if not commit or candidates == 0:
        return BackfillReport(
            candidates=candidates,
            sources_created=0,
            documents_updated=0,
            dry_run=not commit,
        )

    sources_before = conn.execute(
        "SELECT count(*) FROM sources WHERE kind = 'manual'"
    ).fetchone()
    assert sources_before is not None  # COUNT(*) always returns one row
    before = int(sources_before[0])

    documents_updated = 0
    with conn.transaction():
        for doc_id, source_path in rows:
            source_id = _upsert_source(
                conn, kind="manual", external_id=source_path, metadata={}
            )
            # External_id is non-NULL for every row in this scan (WHERE
            # clause filters source_path IS NOT NULL), so _upsert_source's
            # "no external id and no metadata" early-return cannot fire.
            assert source_id is not None
            conn.execute(
                "UPDATE documents SET source_id = %s WHERE id = %s",
                (source_id, doc_id),
            )
            documents_updated += 1

    sources_after = conn.execute(
        "SELECT count(*) FROM sources WHERE kind = 'manual'"
    ).fetchone()
    assert sources_after is not None
    sources_created = int(sources_after[0]) - before

    return BackfillReport(
        candidates=candidates,
        sources_created=sources_created,
        documents_updated=documents_updated,
        dry_run=False,
    )
