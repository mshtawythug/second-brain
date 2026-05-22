"""Derived aggregate layer for the GraphRAG people graph (wave G1, spec §7 step 4).

Extracted from :mod:`brain.graph_rag.reconcile` (wave-G1 boundary refactor) to
keep that module focused on the per-document reconcile orchestration. This module
owns the **tenant-wide derived aggregate** concerns that both the per-document
reconcile and the corpus-wide refresh share:

* :func:`refresh_aggregates` — the public corpus-wide aggregate recompute backing
  ``brain graphrag refresh`` (spec §7 step 4 / §8 / §9).
* :func:`_recompute_aggregates` — full-tenant rebuild of ``graph_relationships``
  from ``graph_edge_contributions`` (normalized lift + generic suppression,
  G1-a :mod:`~brain.graph_rag.weighting`). Reused verbatim by
  :func:`brain.graph_rag.reconcile.reconcile_document` /
  :func:`~brain.graph_rag.reconcile.remove_document` so the weighting logic lives
  in exactly one place.
* :func:`_gc_orphan_persons` / :func:`_gc_orphan_concepts` — GC of now-zero-mention
  catalog rows, scoped to ``entity_type = 'person'`` and to the concept
  ``entity_type``s respectively. :func:`refresh_aggregates` runs BOTH (an
  aspect-agnostic corpus-wide refresh), matching ``remove_document`` and
  ``build --force``.

Because the aggregates derive *purely* from the per-document source-of-truth, a
full recompute is always correct and cascade-safe; affected-only incremental
refresh is explicitly out of scope for v1 (spec §15).

``RefreshResult`` and ``refresh_aggregates`` are re-exported from
:mod:`brain.graph_rag.reconcile` (and :mod:`brain.graph_rag`) so existing
callers (``brain graphrag refresh`` CLI, tests) keep importing them from there
unchanged — this extraction is behavior-preserving, with no public-API move.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import psycopg

from .backends.base import GraphBackend
from .weighting import edge_weight, generic_df_cap

if TYPE_CHECKING:
    from .reconcile import ReconcileConfig

__all__ = [
    "RefreshResult",
    "refresh_aggregates",
]


@dataclass(frozen=True)
class RefreshResult:
    """Outcome of a corpus-wide :func:`refresh_aggregates` call (spec §7 step 4).

    Unlike ``ReconcileResult`` (which describes one document) this is the
    tenant-wide aggregate recompute, so it carries no ``document_id``.
    ``relationship_count`` is the number of aggregate ``graph_relationships``
    edges written for the tenant after the recompute; ``orphans_removed`` is the
    number of now-zero-mention catalog rows GC'd in the same pass across BOTH
    aspects (person + concept).
    """

    tenant_id: str
    relationship_count: int = 0
    orphans_removed: int = 0


def refresh_aggregates(
    conn: psycopg.Connection[Any],
    *,
    backend: GraphBackend,
    config: ReconcileConfig | None = None,
) -> RefreshResult:
    """Corpus-wide aggregate recompute for one tenant (spec §7 step 4, §8/§9).

    The corpus-wide weight/edge recompute of the tenant's derived aggregates from
    the per-document source-of-truth, **without re-resolving any document's
    persons** (that is
    :func:`brain.graph_rag.reconcile.reconcile_document`'s job). It is the
    explicit response to a corpus-wide weighting / suppression change — a new
    ``generic_df_ratio`` ⇒ a new ``suppress_ver`` — that must propagate to every
    edge at once. Backs the ``brain graphrag refresh`` CLI. For a dropped or
    corrupted AGE mirror (entity vertices missing) use
    ``brain graphrag build --force`` — the authoritative full rebuild — instead.

    Reuses the SAME private recompute + GC helpers as ``reconcile_document`` and
    ``remove_document`` (no duplicated weighting logic): recompute every
    ``graph_relationships`` edge from ``graph_edge_contributions`` (normalized
    lift + generic suppression, G1-a), GC any now-orphaned person AND concept
    catalog rows (aspect-agnostic, matching ``remove_document``), then
    rematerialize the AGE ``CO_OCCURS`` edges from the refreshed mirror and
    DETACH DELETE the orphan vertices of both aspects.

    Runs entirely inside ``with conn.transaction()`` (see the reconcile module's
    "Connection contract"), so the relational recompute and the AGE rematerialize
    commit or roll back together. Idempotent: a second call with the same config
    + source of truth converges to the identical graph (a stable no-op when
    nothing changed).

    Precondition: the tenant's ``Entity`` vertices already exist in AGE (run
    ``brain graphrag build --backfill`` first).
    :meth:`~brain.graph_rag.backends.base.GraphBackend.refresh_cooccur_edges`
    raises :class:`brain.errors.GraphBackendError` if a surviving relationship
    references an entity with no AGE vertex — surfacing a refresh-before-build
    mistake rather than silently under-materializing.
    """
    # Late import keeps the module-level dependency one-way (reconcile → this
    # module) so the extraction introduces no import cycle; reconcile is fully
    # loaded by the time refresh_aggregates is ever called.
    if config is None:
        from .reconcile import ReconcileConfig

        config = ReconcileConfig()
    tenant_id = config.tenant_id
    with conn.transaction():
        relationship_count = _recompute_aggregates(
            conn, tenant_id, config.generic_df_ratio
        )
        # GC orphans of BOTH aspects, matching remove_document (reconcile.py) and
        # build --force (build.py): a corpus-wide refresh is aspect-agnostic, so a
        # now-zero-mention concept catalog row + its AGE vertex must be cleaned up
        # too, not just persons (spec §7 step 4 "DETACH DELETE zero-mention Entity
        # vertices"). The concept GC is a no-op when no concept entities exist, so
        # this is safe for the person-only default.
        orphan_ids = [
            *_gc_orphan_persons(conn, tenant_id),
            *_gc_orphan_concepts(conn, tenant_id),
        ]
        backend.refresh_cooccur_edges(conn, tenant_id)
        if orphan_ids:
            backend.detach_delete_entities(conn, tenant_id, orphan_ids)
        return RefreshResult(
            tenant_id=tenant_id,
            relationship_count=relationship_count,
            orphans_removed=len(orphan_ids),
        )


def _recompute_aggregates(
    conn: psycopg.Connection[Any], tenant_id: str, generic_df_ratio: float
) -> int:
    """Full-tenant recompute of ``graph_relationships`` from contributions.

    Refreshes every entity's derived ``doc_count``, then rebuilds the tenant's
    aggregate edges: per pair, ``weight`` is the normalized lift over the pair's
    co-document count and the endpoints' document frequencies, suppressed (row
    omitted) when either endpoint exceeds the generic-frequency cap
    (``round(generic_df_ratio × corpus_N)``). Returns the number of aggregate
    edges written. Cascade-safe full recompute (spec §7 step 4).
    """
    # Refresh the derived per-entity doc_count from the mentions source-of-truth.
    conn.execute(
        """
        UPDATE graph_entities ge
        SET doc_count = COALESCE((
            SELECT COUNT(DISTINCT m.document_id)
            FROM graph_entity_mentions m
            WHERE m.tenant_id = ge.tenant_id AND m.entity_id = ge.id
        ), 0)
        WHERE ge.tenant_id = %s
        """,
        (tenant_id,),
    )

    df = {
        str(entity_id): int(count)
        for entity_id, count in conn.execute(
            "SELECT entity_id::text, COUNT(DISTINCT document_id) "
            "FROM graph_entity_mentions WHERE tenant_id = %s GROUP BY entity_id",
            (tenant_id,),
        ).fetchall()
    }
    corpus_row = conn.execute(
        "SELECT COUNT(DISTINCT document_id) "
        "FROM graph_entity_mentions WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchone()
    corpus_n = int(corpus_row[0]) if corpus_row is not None else 0
    cap = generic_df_cap(corpus_n, generic_df_ratio)

    pair_rows = conn.execute(
        "SELECT src_id::text, dst_id::text, SUM(cooccur_count), "
        "COUNT(DISTINCT document_id) "
        "FROM graph_edge_contributions WHERE tenant_id = %s "
        "GROUP BY src_id, dst_id",
        (tenant_id,),
    ).fetchall()

    conn.execute(
        "DELETE FROM graph_relationships WHERE tenant_id = %s", (tenant_id,)
    )

    written = 0
    for src_id, dst_id, co_count, pair_doc_count in pair_rows:
        weight = edge_weight(
            co_doc_count=int(pair_doc_count),
            src_doc_count=df[str(src_id)],
            dst_doc_count=df[str(dst_id)],
            cap=cap,
        )
        if weight is None:
            continue  # generic-suppressed: do not materialize this edge
        conn.execute(
            "INSERT INTO graph_relationships "
            "(tenant_id, src_id, dst_id, rel_type, weight, co_count, doc_count) "
            "VALUES (%s, %s, %s, 'co_occurs', %s, %s, %s)",
            (
                tenant_id,
                str(src_id),
                str(dst_id),
                weight,
                int(co_count),
                int(pair_doc_count),
            ),
        )
        written += 1
    return written


def _gc_orphan_persons(conn: psycopg.Connection[Any], tenant_id: str) -> list[str]:
    """Delete + return the ids of zero-mention person catalog rows (spec §7.4).

    Scoped to ``entity_type = 'person'`` — the person aspect. Deleting the
    ``graph_entities`` row cascades to any stale relationship/contribution rows
    (none expected post-recompute). The returned ids are handed to the backend
    to DETACH DELETE the matching AGE vertices.
    """
    return _gc_orphan_entities(conn, tenant_id, ["person"])


def _gc_orphan_concepts(conn: psycopg.Connection[Any], tenant_id: str) -> list[str]:
    """Delete + return the ids of zero-mention concept catalog rows (spec §7.4).

    Scoped to the four concept ``entity_type``s (``topic``/``project``/``org``/
    ``tool``) — the concept aspect (wave G2-c). People are GC'd separately by
    :func:`_gc_orphan_persons` so each aspect cleans only its own orphans, never
    double-counting. Same cascade + DETACH-DELETE contract as the person GC.
    """
    # Late import keeps the module-level dependency one-way (this module does not
    # import the concept package at load) and avoids any import-order coupling.
    from .concepts import CONCEPT_ENTITY_TYPES

    return _gc_orphan_entities(conn, tenant_id, list(CONCEPT_ENTITY_TYPES))


def _gc_orphan_entities(
    conn: psycopg.Connection[Any], tenant_id: str, entity_types: list[str]
) -> list[str]:
    """Delete + return zero-mention catalog rows of the given ``entity_types``.

    The shared GC core behind :func:`_gc_orphan_persons` /
    :func:`_gc_orphan_concepts`. An entity with no remaining
    ``graph_entity_mentions`` row is orphaned; deleting it cascades to any stale
    relationship/contribution rows. ``entity_types`` is scoped via ``= ANY(%s)``
    so each aspect GCs only its own rows.
    """
    rows = conn.execute(
        """
        SELECT ge.id::text FROM graph_entities ge
        WHERE ge.tenant_id = %s
          AND ge.entity_type = ANY(%s)
          AND NOT EXISTS (
              SELECT 1 FROM graph_entity_mentions m
              WHERE m.tenant_id = ge.tenant_id AND m.entity_id = ge.id
          )
        """,
        (tenant_id, entity_types),
    ).fetchall()
    orphan_ids = [str(row[0]) for row in rows]
    if orphan_ids:
        conn.execute(
            "DELETE FROM graph_entities "
            "WHERE tenant_id = %s AND id::text = ANY(%s)",
            (tenant_id, orphan_ids),
        )
    return orphan_ids
