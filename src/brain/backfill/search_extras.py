"""Backfill ``chunks.title_text`` / ``tags_text`` / ``search_extras`` after migration 009."""
from dataclasses import dataclass

import psycopg

from ..ingest.sub_tokens import extract_sub_tokens


@dataclass
class BackfillReport:
    """Outcome of :func:`run`.

    ``stage_a_rows`` is the number of chunk rows updated by the SQL
    denormalization pass (title/tags from the parent document). ``stage_b_rows``
    is the number of chunk rows whose ``search_extras`` was written by the
    Python recompute pass — only rows whose computed value differs from the
    stored value get rewritten, so a converged corpus reports 0 here even
    when called repeatedly. ``total_chunks`` is the row count of the
    ``chunks`` table at the start of the run; provided for observability so
    operators can sanity-check the proportions.
    """

    stage_a_rows: int
    stage_b_rows: int
    total_chunks: int


# Stage A — denormalize ``documents.title`` and ``documents.tags`` onto chunks.
# ``IS DISTINCT FROM`` on both columns makes this an idempotent UPDATE; a
# converged corpus matches zero rows on the second call. The join is on
# ``chunks.document_id = d.id`` (FK), so every chunk has exactly one source
# document — the UPDATE rowcount equals the count of mismatching rows.
_STAGE_A_SQL = (
    "UPDATE chunks "
    "SET title_text = d.title, "
    "    tags_text  = array_to_string(d.tags, ' ') "
    "FROM documents d "
    "WHERE chunks.document_id = d.id "
    "  AND (chunks.title_text IS DISTINCT FROM d.title "
    "       OR chunks.tags_text IS DISTINCT FROM array_to_string(d.tags, ' '))"
)


# Stage B reads chunks in pages of this size to keep memory bounded on a
# 100k-chunk corpus. ``content`` is the only large column we pull, but
# materializing all of it in one shot would cost on the order of (chunk_size
# * row_count) of RSS — at 1k tokens (~4kB) per chunk and 100k chunks that's
# ~400MB. Paging keeps it bounded at ~4MB per batch.
_STAGE_B_PAGE_SIZE = 1000


def run(conn: psycopg.Connection) -> BackfillReport:
    """Backfill ``chunks.title_text`` / ``tags_text`` / ``search_extras``.

    Two stages, both idempotent and rerunnable:

    Stage A — one SQL ``UPDATE`` denormalizes ``documents.title`` and
    ``documents.tags`` onto every chunk via the ``document_id`` FK. The
    ``IS DISTINCT FROM`` guards mean a converged corpus is a no-op (returns
    ``stage_a_rows=0``).

    Stage B — Python loop. Reads chunks in pages of ``_STAGE_B_PAGE_SIZE``,
    recomputes ``extract_sub_tokens(content)`` for each, and writes back only
    when the computed value differs from the stored ``search_extras``. This
    "compute and compare" shape (per revision #5 of the plan) restores the
    canonical value when an operator manually edits ``search_extras`` to a
    stale string.

    Both stages run within ``conn.transaction()`` blocks. Caller is
    responsible for connection lifecycle.

    Returns a :class:`BackfillReport` with per-stage rowcounts and the
    total chunk count observed at the start of the run.
    """
    total_row = conn.execute("SELECT count(*) FROM chunks").fetchone()
    assert total_row is not None  # COUNT(*) always returns one row
    total_chunks = int(total_row[0])

    # Stage A — single SQL UPDATE in its own transaction. Empty-corpus case
    # returns rowcount 0 naturally; no special-casing needed.
    with conn.transaction():
        cur = conn.execute(_STAGE_A_SQL)
        stage_a_rows = cur.rowcount or 0

    if total_chunks == 0:
        return BackfillReport(
            stage_a_rows=stage_a_rows,
            stage_b_rows=0,
            total_chunks=0,
        )

    # Stage B — page through chunks, recompute, write only on diff. We use
    # keyset pagination on the primary key (UUID) rather than OFFSET so the
    # page boundary stays stable when concurrent writes shift row positions.
    # Mirrors :func:`brain.queries.iter_chunks_missing_embedding`'s pattern
    # of casting ``id::text`` for transit and ``%s::uuid`` for the bound on
    # the next page.
    stage_b_rows = 0
    last_id: str | None = None
    while True:
        if last_id is None:
            page = conn.execute(
                "SELECT id::text, content, search_extras FROM chunks "
                "ORDER BY id LIMIT %s",
                (_STAGE_B_PAGE_SIZE,),
            ).fetchall()
        else:
            page = conn.execute(
                "SELECT id::text, content, search_extras FROM chunks "
                "WHERE id > %s::uuid ORDER BY id LIMIT %s",
                (last_id, _STAGE_B_PAGE_SIZE),
            ).fetchall()
        if not page:
            break

        with conn.transaction():
            for chunk_id, content, stored_extras in page:
                computed = extract_sub_tokens(content or "")
                # Treat NULL stored_extras and empty string as equivalent —
                # the generated tsv column ``coalesce(...)``s NULL to '', so
                # writing "" back when the stored value is NULL would be an
                # observable no-op only at the row level. Skip it to keep
                # the rowcount honest.
                current = stored_extras or ""
                if computed == current:
                    continue
                conn.execute(
                    "UPDATE chunks SET search_extras = %s WHERE id = %s::uuid",
                    (computed, chunk_id),
                )
                stage_b_rows += 1

        last_id = str(page[-1][0])
        if len(page) < _STAGE_B_PAGE_SIZE:
            break

    return BackfillReport(
        stage_a_rows=stage_a_rows,
        stage_b_rows=stage_b_rows,
        total_chunks=total_chunks,
    )
