"""Vector-leg candidate SQL for hybrid search: an exact cosine scan, never HNSW.

Extracted from :func:`brain.search.hybrid_search` (which is over the 800-line
ceiling) together with the fix that motivated it. Design and measurements:
``docs/specs/2026-10-13-search-vector-leg-design.md``.

**Why the ORDER BY is ``score DESC, c.id`` and not ``c.embedding <=> q``.** The
index-operator form is exactly the shape the HNSW index ``chunks_embedding_idx``
serves, so the planner used it, and three documented pgvector properties
followed:

1. An HNSW scan returns at most ``hnsw.ef_search`` rows (default **40**), so a
   ``LIMIT 50`` got ~40 on the live corpus.
2. Every ``WHERE`` condition (the metadata predicate and the cosine floor alike)
   is applied *after* the index scan, so filtered queries kept only the matching
   fraction of those ~40: 14-33 rows on the golden queries.
3. The scan is approximate. Recall@50 against an exact scan was 0.757 mean, with
   a minimum of 0.00.

An HNSW index can only serve ``ORDER BY embedding <=> q``. Ordering by the score
*expression* therefore makes the index unusable for this query regardless of
``ef_search``, statistics or session settings: the scan is exact, every filter
is exact, and the pool is the true top ``limit`` among chunks with a defined
cosine. ``c.id`` makes the order total, so equal-cosine chunks rank
deterministically.

**Why the floor is written ``(e <=> q) <= 1 - floor``.** A zero-norm embedding
has a cosine distance of NaN. Postgres orders NaN above every number and
evaluates ``NaN >= x`` as TRUE, so the algebraically equal
``1 - (e <=> q) >= floor`` would admit such a row and ``score DESC`` would rank
it first. ``NaN <= x`` is FALSE, so this form excludes it. The HNSW path never
returned these rows, because pgvector does not index zero vectors for cosine;
the exact scan must exclude them itself. Prod held 0 such chunks on 2026-10-13.

**Rejected alternatives, measured** (spec §3):

- *Raising ``hnsw.ef_search``* flips the planner to a sequential scan unless
  ``enable_seqscan`` is also forced off. ``SET LOCAL`` would leak into the
  caller's transaction, and recall still depends on graph quality.
- *A per-document chunk cap* (FTS's ``PER_DOC_CHUNK_CAP``) scored worse than the
  exact scan alone on all three eval means.

**Cost:** about 100 ms per search at 21k chunks, growing linearly. Revisit (tuned
HNSW plus a recall health check) when ``SearchDiagnostics.sql_ms`` p90 over the
31 golden queries exceeds 250 ms. ``sql_ms`` includes this leg plus the FTS leg,
the count and the doc fetch, so it is a conservative proxy, since no tool reports
the leg alone. The read-only procedure is in spec §5 (147.5 ms on 2026-10-13).
"""
from typing import Any

from .search_predicate import SearchPredicate


def vector_leg_sql(
    *,
    predicate: SearchPredicate,
    query_embedding: list[float],
    vector_sim_floor: float,
    limit: int,
) -> tuple[str, list[Any]]:
    """Return ``(sql, params)`` for the vector leg's candidate query.

    Rows are ``(chunk id, document id, chunk index, content, cosine score)``,
    best first. ``limit`` is the only value formatted into the SQL text, and it is
    validated as a positive ``int`` first. The embedding, every predicate value
    and the floor are bound as ``%s`` parameters.

    Raises:
        ValueError: ``limit`` is not a positive ``int``.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive int, got {limit!r}")
    floor_pred = "(c.embedding <=> %s::vector) <= 1 - %s"
    params: list[Any]
    if predicate.has_filters:
        where = f"WHERE {predicate.where_sql} AND {floor_pred}"
        params = [
            query_embedding,
            *predicate.where_params,
            query_embedding,
            vector_sim_floor,
        ]
    else:
        where = f"WHERE {floor_pred}"
        params = [query_embedding, query_embedding, vector_sim_floor]
    sql = f"""
            SELECT c.id, c.document_id, c.chunk_index, c.content,
                   1 - (c.embedding <=> %s::vector) AS score
            FROM chunks c
            {predicate.join_clause}
            {where}
            ORDER BY score DESC, c.id
            LIMIT {limit}"""
    return sql, params
