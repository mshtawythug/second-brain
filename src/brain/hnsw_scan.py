"""Scoped pgvector HNSW scan settings shared by every chunk-vector candidate query.

One definition of the "design D" settings and of the protocol that applies them
to a single query without leaking, used by :mod:`brain.search_vector_leg`,
:mod:`brain.related_candidates` and :mod:`brain.connect`. The measurements behind
the values are in ``docs/specs/2026-10-13-search-vector-leg-design.md`` (search)
and ``docs/specs/2026-10-15-related-connect-vector-leg-design.md`` (related docs,
``connect``).

**Why every vector leg needs them.** With pgvector's default session settings the
planner serves ``ORDER BY embedding <=> q LIMIT n`` from the HNSW index, which
hands over at most ``hnsw.ef_search`` rows (default **40**) and applies every
``WHERE`` condition afterwards. A filtered leg therefore under-returns, and even
an unfiltered one is only as good as a 40-wide graph search. The three settings:

- ``hnsw.ef_search = 1000`` (pgvector's maximum): the recall decision;
- ``hnsw.iterative_scan``: keep scanning until ``n`` rows survive the filters
  (bounded by ``hnsw.max_scan_tuples``, default 20,000). ``relaxed_order`` for a
  candidate pool that is re-sorted afterwards; ``strict_order`` when the caller's
  own logic needs the rows in distance order as they arrive (``connect``);
- ``enable_seqscan = off``: a higher ``ef_search`` raises the index scan's
  estimated cost, and the planner would otherwise switch to a sequential scan.

**Scoping.** :func:`scoped_settings` sets them with ``set_config(…, is_local =>
true)`` inside its own ``conn.transaction()``. For an autocommit caller that is a
real transaction, so they end at its COMMIT. Inside a caller's transaction it is
a savepoint, and a *released* savepoint keeps local settings, so the prior values
are read first and restored explicitly when the block exits normally. If the
block raises, the savepoint rollback reverts them and the restore is skipped.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

import psycopg

#: ``hnsw.ef_search`` for every vector leg. 1000 is pgvector's maximum.
HNSW_EF_SEARCH = 1000

IterativeScan = Literal["relaxed_order", "strict_order"]

_ITERATIVE_SCANS: frozenset[str] = frozenset({"relaxed_order", "strict_order"})

#: ``(name, value)`` pairs, applied in this order.
ScanSettings = tuple[tuple[str, str], ...]


def scan_settings(iterative_scan: IterativeScan) -> ScanSettings:
    """The design-D settings with the given ``hnsw.iterative_scan`` mode.

    ``off`` is refused rather than accepted: it is the setting that made the
    filtered legs under-return.

    Raises:
        ValueError: ``iterative_scan`` is not ``relaxed_order`` or ``strict_order``.
    """
    if iterative_scan not in _ITERATIVE_SCANS:
        raise ValueError(
            f"iterative_scan must be one of {sorted(_ITERATIVE_SCANS)}, got {iterative_scan!r}"
        )
    return (
        ("hnsw.ef_search", str(HNSW_EF_SEARCH)),
        ("hnsw.iterative_scan", iterative_scan),
        ("enable_seqscan", "off"),
    )


#: For a candidate pool its query re-sorts (search, related docs).
RELAXED_SCAN_SETTINGS = scan_settings("relaxed_order")
#: For a caller that consumes rows in distance order (``connect``'s KNN loop).
STRICT_SCAN_SETTINGS = scan_settings("strict_order")


@contextmanager
def scoped_settings(
    conn: psycopg.Connection[Any], settings: ScanSettings
) -> Iterator[None]:
    """Apply ``settings`` locally for the block; restore the prior values after.

    See the module docstring for why the restore is explicit.

    Raises:
        ValueError: ``settings`` is empty.
    """
    if not settings:
        raise ValueError("scoped_settings needs at least one setting")
    names = [name for name, _ in settings]
    with conn.transaction():
        prior = conn.execute(
            "SELECT " + ", ".join(["current_setting(%s, true)"] * len(names)),
            names,
        ).fetchone()
        assert prior is not None  # a SELECT with no FROM always returns one row
        _set_local(conn, [part for pair in settings for part in pair])
        yield
        _restore_settings(conn, names, prior)


def _set_local(conn: psycopg.Connection[Any], name_value_pairs: list[Any]) -> None:
    """``set_config(name, value, is_local => true)`` for each flattened pair."""
    conn.execute(
        "SELECT " + ", ".join(["set_config(%s, %s, true)"] * (len(name_value_pairs) // 2)),
        name_value_pairs,
    )


def _restore_settings(
    conn: psycopg.Connection[Any], names: list[str], prior: tuple[Any, ...]
) -> None:
    """Put each setting back to the value it had before the block ran.

    A ``NULL`` prior value means the ``hnsw.*`` variable did not exist yet:
    pgvector's library is not preloaded, so a fresh session has no ``hnsw.*``
    settings until its first vector operation. ``set_config(name, NULL, true)``
    resets a setting to its session default (verified 2026-10-13: a local 777
    returned to 40), which is the correct restore target in that case.
    """
    _set_local(conn, [part for pair in zip(names, prior, strict=True) for part in pair])
