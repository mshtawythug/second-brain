"""Vector-leg candidate query for hybrid search: an iterative HNSW scan, near-exact.

Extracted from :func:`brain.search.hybrid_search` (which is over the 800-line
ceiling) together with the fix that motivated it. Design, measurements and the
decision history: ``docs/specs/2026-10-13-search-vector-leg-design.md``.

**The defect.** The leg used to be ``ORDER BY c.embedding <=> q LIMIT 50`` with
pgvector's default session settings. The HNSW index served it, so:

1. a scan returned at most ``hnsw.ef_search`` rows (default **40**);
2. every ``WHERE`` condition (metadata predicate and cosine floor alike) applied
   *after* the scan, so filtered queries got 14-33 rows;
3. recall@50 against an exact scan was 0.757 mean, minimum 0.00.

**The fix, and why it is not the exact scan.** The first version made the leg
an exact scan (recall 1.0 by construction). Under recorded load its own time
was a p90 of 249-259 ms at 21k chunks, growing linearly, which is over the 150 ms
budget. This version keeps the index and fixes all three defects:

- ``hnsw.iterative_scan = relaxed_order`` keeps scanning until ``limit`` rows
  survive the *metadata* filters (bounded by ``hnsw.max_scan_tuples``, default
  20,000, about the whole corpus), so filters no longer starve the pool. That
  bound is also a **latency** risk: a highly selective metadata filter can
  walk up to that many tuples before ``limit`` rows match;
- ``hnsw.ef_search = 1000`` (pgvector's maximum) gives a mean recall@50 of
  0.9987 (minimum 0.98) against the exact scan over the 31 golden queries.
  ef=400 measured 0.9903 with a minimum of 0.90, too little margin;
- ``enable_seqscan = off``, because raising ``ef_search`` raises the index
  scan's estimated cost and the planner would otherwise switch to a sequential
  scan. A plan that does not use the index (some filtered queries) is exact,
  never wrong.

Relaxed ordering can return rows slightly out of distance order, so the scan
sits in a ``MATERIALIZED`` CTE and is re-sorted outside it by ``dist + 0, id``.
**The cosine floor is applied OUTSIDE the CTE** (``WHERE dist <= 1 - floor``),
as pgvector's README directs for distance filters. Inside, it would become a
condition the iterative scan keeps walking to satisfy, so an off-corpus query
with nothing above the floor would traverse the index up to
``hnsw.max_scan_tuples``. Review measured that on a synthetic 21k x 1024 table:
751 ms inside versus 86 ms outside, with the same ids returned. Because the floor
is a distance threshold, the rows that pass it are a prefix of the distance
order, so "top ``limit``, then floor" returns the same set as "floor, then top
``limit``".
The ``+ 0`` keeps the outer sort from being satisfied by the index (pgvector's
documented pattern for Postgres 17+). The ``id`` tie-break makes the order total.

**Settings are scoped to this one query and never leak.** They are set with
``set_config(…, is_local => true)`` inside the leg's own ``conn.transaction()``.
For an autocommit caller that is a real transaction, so the settings end at its
COMMIT. Inside a caller's transaction it is a savepoint, and a *released*
savepoint keeps local settings, so the prior values are read first and restored
explicitly after the query. If the query raises, the savepoint rollback reverts
them.

**Why the floor is written ``dist <= 1 - floor``.** A zero-norm embedding has
a cosine distance of NaN. Postgres orders NaN above every number and evaluates
``NaN >= x`` as TRUE, so the algebraically equal ``1 - dist >= floor`` would
admit such a row whenever a plan does not use the index. pgvector does not index
zero vectors for cosine, but an exact plan reads every row, and its ascending
distance order puts NaN last, inside a short pool. ``NaN <= x`` is FALSE.
Prod held 0 such chunks on 2026-10-13.

**Revisit** when ``SearchDiagnostics.vector_ms`` p90 exceeds 150 ms over the
golden queries *or* over the off-corpus probe queries, or when the recall@50
check falls below 0.99. Recall depends on HNSW graph quality, which shifts with
ingests and reindexes. All three procedures are read-only and are in spec §5.
"""
from collections.abc import Callable
from time import perf_counter
from typing import Any

import psycopg

from .search_predicate import SearchPredicate

#: ``hnsw.ef_search`` for the leg. 1000 is pgvector's maximum; see the module
#: docstring for the recall measurements that chose it.
HNSW_EF_SEARCH = 1000

#: ``(name, value)`` pairs set locally around the leg's query, in this order.
_SCAN_SETTINGS: tuple[tuple[str, str], ...] = (
    ("hnsw.ef_search", str(HNSW_EF_SEARCH)),
    ("hnsw.iterative_scan", "relaxed_order"),
    ("enable_seqscan", "off"),
)


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
    and the floor are bound as ``%s`` parameters. The query is shaped for the
    HNSW index (inner ``ORDER BY … <=> … LIMIT``); run it via
    :func:`run_vector_leg`, which applies the scan settings it relies on.

    Raises:
        ValueError: ``limit`` is not a positive ``int``.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive int, got {limit!r}")
    # Metadata filters go INSIDE the CTE: the iterative scan must keep walking
    # until `limit` rows match them. The distance floor goes OUTSIDE: inside, an
    # off-corpus query (nothing above the floor) would walk the whole index, up
    # to hnsw.max_scan_tuples. pgvector's README gives the same placement rule.
    where = f"WHERE {predicate.where_sql}" if predicate.has_filters else ""
    params: list[Any] = [
        query_embedding,
        *predicate.where_params,
        query_embedding,
        vector_sim_floor,
    ]
    sql = f"""
            WITH nn AS MATERIALIZED (
                SELECT c.id, c.document_id, c.chunk_index, c.content,
                       c.embedding <=> %s::vector AS dist
                FROM chunks c
                {predicate.join_clause}
                {where}
                ORDER BY c.embedding <=> %s::vector
                LIMIT {limit}
            )
            SELECT id, document_id, chunk_index, content, 1 - dist AS score
            FROM nn
            WHERE dist <= 1 - %s
            ORDER BY dist + 0, id"""
    return sql, params


def run_vector_leg(
    conn: psycopg.Connection[Any],
    *,
    predicate: SearchPredicate,
    query_embedding: list[float],
    vector_sim_floor: float,
    limit: int,
    clock: Callable[[], float] = perf_counter,
) -> tuple[list[Any], float]:
    """Execute the vector leg under its scan settings; return ``(rows, elapsed_ms)``.

    The elapsed time covers only the candidate query, which is what makes
    ``SearchDiagnostics.vector_ms`` (the revisit trigger's input) a measurement
    of this leg, rather than of the settings round trips or the whole ``sql_ms``
    phase. It is returned in **milliseconds**: the unit conversion lives here, so
    a test can inject ``clock`` (seconds, like :func:`time.perf_counter`) and
    assert the exact value. See the module docstring for how the settings are
    scoped.
    """
    sql, params = vector_leg_sql(
        predicate=predicate,
        query_embedding=query_embedding,
        vector_sim_floor=vector_sim_floor,
        limit=limit,
    )
    names = [name for name, _ in _SCAN_SETTINGS]
    with conn.transaction():
        prior = conn.execute(
            "SELECT current_setting(%s, true), current_setting(%s, true), "
            "current_setting(%s, true)",
            names,
        ).fetchone()
        conn.execute(
            "SELECT set_config(%s, %s, true), set_config(%s, %s, true), "
            "set_config(%s, %s, true)",
            [part for pair in _SCAN_SETTINGS for part in pair],
        )
        started = clock()
        rows = conn.execute(sql, params, prepare=predicate.prepare_flag).fetchall()
        elapsed_ms = (clock() - started) * 1000.0
        _restore_settings(conn, names, prior)
    return rows, elapsed_ms


def _restore_settings(
    conn: psycopg.Connection[Any], names: list[str], prior: tuple[Any, ...] | None
) -> None:
    """Put each setting back to the value it had before the leg ran.

    A ``NULL`` prior value means the ``hnsw.*`` variable did not exist yet:
    pgvector's library is not preloaded, so a fresh session has no ``hnsw.*``
    settings until its first vector operation. ``set_config(name, NULL, true)``
    resets a setting to its session default (verified 2026-10-13: a local 777
    returned to 40), which is the correct restore target in that case.
    """
    values = list(prior) if prior is not None else [None] * len(names)
    conn.execute(
        "SELECT " + ", ".join(["set_config(%s, %s, true)"] * len(names)),
        [part for pair in zip(names, values, strict=True) for part in pair],
    )
