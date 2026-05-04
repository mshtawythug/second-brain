"""Backfill the typed email/krisp columns added by migration 007.

Migration 007 added six narrow columns on ``documents`` (``thread_id``,
``rfc_message_id``, ``in_reply_to``, ``sent_at``, ``participants``,
``duration_min``) plus the boolean ``draft``. The ingest pipeline writes
them on insert/update going forward; this one-shot script fills them for
every pre-existing ``kind='ingested'`` row whose JSONB ``metadata`` blob
already carries the source data.

Behavior:

- Read-only by default? No — default is the actual write. Pass ``--dry-run``
  to print the report without committing.
- Idempotent: only NULL columns are filled, and the candidate WHERE clause
  excludes already-fully-populated rows. Re-running on a backfilled corpus
  is a no-op.
- Additive: never overwrites a non-NULL column. Never deletes / drops.
- Per-row failures (e.g. a corrupt metadata blob) log at WARNING and the
  run continues — the row is skipped, not the whole batch. Each row's
  UPDATE is wrapped in a savepoint so a single bad row does not roll back
  the surrounding 100-row batch.
- Reuses :func:`brain.ingest._promote_metadata_to_columns` so the projection
  logic stays in lockstep with the live ingest path.

Standalone script — invoked as ``python scripts/backfill_email_columns.py``
or ``python -m scripts.backfill_email_columns``. Not a ``brain`` subcommand;
this is a one-shot piece of plumbing, not a user-facing CLI surface.
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import psycopg

from brain.config import Config, ConfigError
from brain.db import connect
from brain.errors import BrainError
from brain.ingest import _PROMOTED_COLUMNS, _promote_metadata_to_columns

_logger = logging.getLogger("brain.scripts.backfill_email_columns")

# How many successful UPDATEs we accumulate before issuing a COMMIT. Keeps
# the failure window bounded without paying per-row commit overhead.
_BATCH_SIZE = 100

# Columns the candidate WHERE filter examines. A row is a candidate when at
# least one of these is currently NULL. We deliberately omit
# ``rfc_message_id`` / ``in_reply_to`` from this filter — they're typically
# only present on a strict subset of gmail rows, so requiring them in the
# WHERE clause would either pull in nearly every row or miss real candidates
# depending on which way we skewed the filter. Per-row UPDATE still writes
# them when the metadata blob carries a value (see ``_PROMOTED_COLUMNS``).
_FILTER_NULLABLE_COLUMNS: tuple[str, ...] = (
    "thread_id",
    "sent_at",
    "participants",
    "duration_min",
)

# Buckets used in the final report. Anything outside this set (or with a
# NULL source_id) lands in ``"other"``.
_KNOWN_SOURCE_KINDS: tuple[str, ...] = ("gmail", "krisp", "slack", "manual")


@dataclass
class _SourceCounts:
    """Per-source-kind tally used to assemble the run report.

    ``columns_populated`` tracks how many UPDATEs wrote each promoted column
    (one row can bump multiple counters). ``failed`` counts rows whose
    UPDATE raised a psycopg error and was skipped. ``scanned`` reflects
    every row pulled by the candidate query — sum of updated + skipped +
    failed.
    """

    scanned: int = 0
    updated: int = 0
    skipped_already_set: int = 0
    failed: int = 0
    columns_populated: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(_PROMOTED_COLUMNS, 0)
    )


@dataclass
class BackfillReport:
    """Outcome of :func:`backfill_email_columns`.

    ``by_source`` keys are the source-kind buckets in
    :data:`_KNOWN_SOURCE_KINDS` plus ``"other"``. Buckets with zero scanned
    rows are still present (zero-filled) so the printed report has a stable
    shape.
    """

    dry_run: bool
    by_source: dict[str, _SourceCounts]

    @property
    def total_scanned(self) -> int:
        return sum(b.scanned for b in self.by_source.values())

    @property
    def total_updated(self) -> int:
        return sum(b.updated for b in self.by_source.values())

    @property
    def total_skipped(self) -> int:
        return sum(b.skipped_already_set for b in self.by_source.values())

    @property
    def total_failed(self) -> int:
        return sum(b.failed for b in self.by_source.values())


def _bucket_for(source_kind: str | None) -> str:
    """Map a raw ``sources.kind`` value onto one of the report buckets."""
    if source_kind in _KNOWN_SOURCE_KINDS:
        return source_kind
    return "other"


def _empty_buckets() -> dict[str, _SourceCounts]:
    """Pre-populate every report bucket so the output shape is stable."""
    buckets: dict[str, _SourceCounts] = {
        kind: _SourceCounts() for kind in _KNOWN_SOURCE_KINDS
    }
    buckets["other"] = _SourceCounts()
    return buckets


def _verify_schema(conn: psycopg.Connection) -> None:
    """Confirm migration 007 has been applied. Raises BrainError otherwise.

    Cheap pre-flight so a misconfigured DB fails fast with a clear message
    instead of crashing inside the per-row UPDATE with a column-not-found
    error from psycopg.
    """
    row = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='documents' "
        "AND column_name = ANY(%s)",
        (list(_PROMOTED_COLUMNS),),
    ).fetchall()
    found = {str(r[0]) for r in row}
    missing = sorted(set(_PROMOTED_COLUMNS) - found)
    if missing:
        raise BrainError(
            "documents is missing the typed columns added by migration 007: "
            f"{missing}. Run `brain init` to apply pending migrations."
        )


_CANDIDATE_SQL = """
SELECT d.id,
       d.metadata,
       d.thread_id,
       d.rfc_message_id,
       d.in_reply_to,
       d.sent_at,
       d.participants,
       d.duration_min,
       s.kind AS source_kind
FROM documents d
LEFT JOIN sources s ON s.id = d.source_id
WHERE d.kind = 'ingested'
  AND ({null_predicates})
ORDER BY d.id
"""


def _candidate_query() -> str:
    """Return the candidate-row SELECT, with the NULL predicate inlined.

    The inlined column names come from :data:`_FILTER_NULLABLE_COLUMNS` —
    they're known constants, never user input, so f-string interpolation
    is safe (no parameterization needed for column identifiers).
    """
    null_predicates = " OR ".join(
        f"d.{col} IS NULL" for col in _FILTER_NULLABLE_COLUMNS
    )
    return _CANDIDATE_SQL.format(null_predicates=null_predicates)


def _columns_to_write(
    *,
    promoted: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """Pick the subset of promoted values that targets currently-NULL columns.

    Never overwrites a non-NULL column (idempotency). The output preserves
    insertion order so the dynamic UPDATE statement and its values list
    line up by index.
    """
    return {
        col: value
        for col, value in promoted.items()
        if current.get(col) is None
    }


def _apply_row(
    conn: psycopg.Connection,
    *,
    doc_id: str,
    to_write: dict[str, Any],
) -> None:
    """Issue the UPDATE for a single document, wrapped in a savepoint.

    Caller decides what to do with a raised :class:`psycopg.Error`; the
    savepoint is committed on normal return and rolled back on exception
    so the surrounding batch transaction can keep accumulating other rows.
    """
    set_clause = ", ".join(f"{col} = %s" for col in to_write)
    params: tuple[Any, ...] = (*to_write.values(), doc_id)
    with conn.transaction():
        conn.execute(
            f"UPDATE documents SET {set_clause} WHERE id = %s",
            params,
        )


def backfill_email_columns(
    conn: psycopg.Connection,
    *,
    dry_run: bool = False,
    batch_size: int = _BATCH_SIZE,
) -> BackfillReport:
    """Project ``metadata`` JSONB onto the typed columns on every candidate row.

    Logic per row:

    1. Load metadata + the current values of every promoted column.
    2. Run :func:`brain.ingest._promote_metadata_to_columns` to derive the
       set of column-writes the metadata implies.
    3. Filter to columns that are currently NULL — never overwrite.
    4. If the filtered set is empty, count the row as skipped and move on.
    5. Otherwise issue a single dynamic UPDATE wrapped in a savepoint.

    Commits every ``batch_size`` successful UPDATEs (default 100). A
    per-row exception logs + skips, never aborts the run.
    """
    # Toggle autocommit FIRST — before any query opens an implicit
    # transaction. Trying to set ``autocommit`` while the connection is
    # INTRANS raises ``ProgrammingError`` in psycopg3, so we have to do
    # the toggle while the connection is still IDLE. We only flip when
    # the caller had autocommit=True (test fixtures, interactive use);
    # autocommit=False (the default from ``connect()``) is left alone
    # but we still drive batched commits ourselves.
    prior_autocommit = conn.autocommit
    if prior_autocommit:
        conn.autocommit = False

    buckets = _empty_buckets()
    in_batch = 0
    try:
        _verify_schema(conn)

        rows = conn.execute(_candidate_query()).fetchall()
        _logger.info("backfilling %d candidate document(s)", len(rows))

        if not rows:
            # Commit so the implicit read tx closes cleanly before we
            # restore autocommit (psycopg3 won't toggle while INTRANS).
            conn.commit()
            return BackfillReport(dry_run=dry_run, by_source=buckets)
        for raw in rows:
            (
                doc_id,
                metadata,
                cur_thread_id,
                cur_rfc_message_id,
                cur_in_reply_to,
                cur_sent_at,
                cur_participants,
                cur_duration_min,
                source_kind,
            ) = raw

            bucket_name = _bucket_for(source_kind)
            bucket = buckets[bucket_name]
            bucket.scanned += 1

            current = {
                "thread_id": cur_thread_id,
                "rfc_message_id": cur_rfc_message_id,
                "in_reply_to": cur_in_reply_to,
                "sent_at": cur_sent_at,
                "participants": cur_participants,
                "duration_min": cur_duration_min,
            }

            # Defensive: psycopg3 returns JSONB as ``dict`` already, but a
            # row with a malformed blob (extremely unlikely) would surface
            # as something else. Skip gracefully rather than crash.
            if not isinstance(metadata, dict):
                _logger.warning(
                    "doc %s has non-dict metadata (%s); skipping",
                    doc_id,
                    type(metadata).__name__,
                )
                bucket.failed += 1
                continue

            promoted = _promote_metadata_to_columns(metadata)
            to_write = _columns_to_write(promoted=promoted, current=current)
            if not to_write:
                bucket.skipped_already_set += 1
                continue

            if dry_run:
                bucket.updated += 1
                for col in to_write:
                    bucket.columns_populated[col] += 1
                _logger.debug(
                    "doc %s would set columns: %s", doc_id, sorted(to_write)
                )
                continue

            try:
                _apply_row(conn, doc_id=str(doc_id), to_write=to_write)
            except psycopg.Error as exc:
                # Savepoint already rolled back on the exception path.
                _logger.warning(
                    "doc %s UPDATE failed (%s); skipping",
                    doc_id,
                    exc,
                )
                bucket.failed += 1
                continue

            bucket.updated += 1
            for col in to_write:
                bucket.columns_populated[col] += 1
            _logger.debug(
                "doc %s set columns: %s", doc_id, sorted(to_write)
            )

            in_batch += 1
            if in_batch >= batch_size:
                conn.commit()
                in_batch = 0

        # Final flush: commit any in-flight batch AND the implicit read
        # transaction opened by the candidate SELECT, so the connection
        # is IDLE before we restore autocommit.
        conn.commit()
    except BaseException:
        # Any unexpected error: roll back the open batch (if any) so we
        # don't leave the connection in a half-written state.
        conn.rollback()
        raise
    finally:
        if prior_autocommit:
            conn.autocommit = prior_autocommit

    return BackfillReport(dry_run=dry_run, by_source=buckets)


def _format_report(report: BackfillReport) -> str:
    """Render a :class:`BackfillReport` as the multi-line CLI report.

    Format is intentionally easy to grep — bucket names, then a small block
    of indented metrics per bucket, then a totals line at the bottom.
    """
    lines: list[str] = []
    if report.dry_run:
        lines.append("Backfill report (DRY RUN — no rows committed):")
    else:
        lines.append("Backfill report:")

    bucket_order = list(_KNOWN_SOURCE_KINDS) + ["other"]
    for kind in bucket_order:
        bucket = report.by_source[kind]
        if bucket.scanned == 0:
            lines.append(f"  [{kind}] no candidate rows")
            continue
        lines.append(f"  [{kind}]")
        lines.append(f"    scanned:             {bucket.scanned}")
        lines.append(f"    updated:             {bucket.updated}")
        lines.append(
            f"    skipped (already set): {bucket.skipped_already_set}"
        )
        if bucket.failed:
            lines.append(f"    failed:              {bucket.failed}")
        # Stable column order — matches _PROMOTED_COLUMNS so callers can
        # diff two reports without churn from dict iteration order.
        col_lines: list[str] = []
        for col in _PROMOTED_COLUMNS:
            n = bucket.columns_populated[col]
            if n:
                col_lines.append(f"      {col}: {n}")
        if col_lines:
            lines.append("    columns populated:")
            lines.extend(col_lines)
    lines.append(
        "  TOTAL "
        f"scanned={report.total_scanned} "
        f"updated={report.total_updated} "
        f"skipped={report.total_skipped} "
        f"failed={report.total_failed}"
    )
    return "\n".join(lines)


def _emit_report(report: BackfillReport, *, out: Iterable[str] | None = None) -> None:
    """Print the formatted report to stdout. Separated so tests can capture."""
    del out  # placeholder for future output-file routing
    print(_format_report(report))


def main(argv: list[str] | None = None) -> int:
    """Parse args, run the backfill, print the report. Returns a shell exit code.

    Connection-failure / migration-not-applied paths return non-zero; a
    clean run (even one with per-row skips) returns ``0``.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Backfill the typed email/krisp columns added by migration "
            "007 from the documents.metadata JSONB blob."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report without committing any UPDATEs.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Log one DEBUG line per updated document.",
    )
    args = parser.parse_args(argv)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Surface the helper's WARNING lines (malformed metadata.* values) at the
    # same level so a user running this against the live DB sees them.
    logging.getLogger("brain.ingest").setLevel(log_level)

    try:
        cfg = Config.load()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    try:
        with connect(cfg.database_url) as conn:
            report = backfill_email_columns(conn, dry_run=args.dry_run)
    except BrainError as e:
        print(f"backfill aborted: {e}", file=sys.stderr)
        return 3
    except psycopg.OperationalError as e:
        print(f"database connection failed: {e}", file=sys.stderr)
        return 4

    _emit_report(report)
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    sys.exit(main())
