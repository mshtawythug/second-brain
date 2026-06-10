"""Storage/traversal backend Protocol for the GraphRAG layer (spec §4 D10).

A :class:`GraphBackend` is the narrow, tenant-scoped interface the rest of the
GraphRAG layer (reconcile in G1, retrieval in G2) depends on. The default
implementation is :class:`brain.graph_rag.backends.age.AgeBackend` (Apache AGE
inside Postgres, openCypher). Keeping the surface narrow is what makes the
Neo4j/Memgraph kill-switch (spec §16) a drop-in replacement: a second backend
conforms to this Protocol and the callers do not change.

Every method is **tenant-scoped** — ``tenant_id`` is a required argument on each
one (spec §4 D9). AGE has no native tenant isolation, so the AGE implementation
enforces the tenant on every matched vertex *and* edge; a backend that cannot
make that guarantee must not claim conformance.

The graph mirrors the relational source-of-truth (migration 012); the durable
app identity of an entity is its ``graph_entities.id`` UUID, carried on the AGE
vertex as the ``entity_uuid`` property. Backend methods speak ``entity_uuid`` /
``document_uuid`` (both equal the relational UUIDs), never AGE-internal IDs.

This module defines only the Protocol and its return value objects; it imports
no DB driver beyond the connection type, so it stays a pure contract that fakes
can satisfy in unit tests.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import psycopg

from ..schema import EntityMention, GraphEntity

__all__ = ["GraphBackend", "PersonScope", "TraversalHit"]


@dataclass(frozen=True)
class TraversalHit:
    """One entity reached by :meth:`GraphBackend.traverse`, with its best path.

    ``entity_uuid`` is the reached entity's durable id (= ``graph_entities.id``).
    ``affinity`` is the product of the winning path's edge weights
    (normalized lift ∈ (0, 1]; a 1-hop neighbour's affinity is just that edge's
    weight). ``hops`` is the length of the winning (highest-affinity) path. The
    seed itself is never returned. ``tenant_id`` records the scope the traversal
    ran under — every element of the path was verified to carry it.
    """

    entity_uuid: str
    affinity: float
    hops: int
    tenant_id: str = "default"


@dataclass(frozen=True)
class PersonScope:
    """The scoped subgraph for "themes with X" expansion (spec §6b step 1).

    Produced by :meth:`GraphBackend.scope_person`: starting from a seed person,
    follow ``MENTIONED_IN`` into the person's documents and back out to the
    entities co-mentioned in those documents. ``entity_uuids`` is the distinct
    set of co-mentioned entities (the seed excluded); ``document_uuids`` is the
    distinct set of connecting documents. Both are tenant-scoped. The grouping
    stage (G2) consumes this set; SQL provides the lift weights and evidence.
    """

    seed_entity_uuid: str
    entity_uuids: tuple[str, ...]
    document_uuids: tuple[str, ...]
    tenant_id: str = "default"


@runtime_checkable
class GraphBackend(Protocol):
    """Tenant-scoped graph storage + bounded traversal (spec §4 D10, §8).

    Implementations own a single named graph and translate these calls into
    their engine's native operations. All write methods are idempotent: calling
    them twice with the same arguments converges to the same graph state (the
    relational source-of-truth is authoritative; the graph is a recomputable
    mirror, spec §7).

    Connection contract: every method takes the caller's ``conn`` so the caller
    owns the transaction (reconcile in G1 wraps the relational rewrite and the
    graph sync in one unit). The connection MUST already have the engine's
    session bootstrap applied (for AGE: ``LOAD 'age'`` via
    :func:`brain.db.connect_age` / :func:`brain.db.load_age`). :meth:`bootstrap`
    additionally requires an autocommit connection (graph/label DDL).
    """

    def bootstrap(self, conn: psycopg.Connection[Any]) -> None:
        """Idempotently provision the graph, its labels, and property indexes.

        Ensures the graph exists, creates the vertex labels (``Entity``,
        ``Document``) and edge labels (``MENTIONED_IN``, ``CO_OCCURS``), and
        builds the property indexes (spec §5b). Safe to re-run. Requires an
        autocommit connection (catalog DDL).
        """
        ...

    def upsert_entities(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        entities: Sequence[GraphEntity],
    ) -> int:
        """MERGE ``Entity`` vertices keyed on ``(tenant_id, entity_uuid)``.

        Creates missing vertices and updates the mutable properties
        (``name``/``entity_type``/``canonical_key``) of existing ones.
        Embeddings stay relational (spec §5) and are never written here. Returns
        the number of entities processed.
        """
        ...

    def upsert_mention_edges(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        document_id: str,
        mentions: Sequence[EntityMention],
        *,
        document_props: Mapping[str, Any] | None = None,
    ) -> int:
        """Rebuild one document's ``MENTIONED_IN`` edges (split MERGE; spec §7.3).

        MERGEs the ``Document`` vertex and each mentioned ``Entity`` vertex on
        their own, then deletes and recreates this document's ``MENTIONED_IN``
        edges. Never MERGEs the whole ``(e)-[:MENTIONED_IN]->(d)`` pattern in
        one clause — AGE re-creates the endpoint vertices when it does. Returns
        the number of mention edges written.
        """
        ...

    def refresh_cooccur_edges(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
    ) -> int:
        """Rematerialize the tenant's ``CO_OCCURS`` edges from the relational mirror.

        Deletes the tenant's existing ``CO_OCCURS`` edges and recreates them
        from ``graph_relationships`` (the derived aggregate mirror, recomputed
        from contributions upstream; spec §5/§7). Full recompute is correct
        because aggregates derive from the source-of-truth. Returns the number
        of aggregate edges written.
        """
        ...

    def traverse(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        seed_entity_uuid: str,
        *,
        depth: int,
        frontier_cap: int,
        min_edge_weight: float = 0.0,
    ) -> list[TraversalHit]:
        """Bounded variable-length ``CO_OCCURS`` traversal from a seed (spec §6a).

        Walks ``CO_OCCURS`` paths of length 1..``depth`` from the seed, enforces
        ``tenant_id`` on every vertex and edge, scores each reached entity by
        path affinity (product of edge weights) keeping the best path per
        entity, drops paths with any edge below ``min_edge_weight``, and caps
        the frontier. The seed itself is never returned. Returns hits ordered by
        affinity descending, ties broken deterministically.

        **Complete-or-failure contract:** the cap is applied *after* affinity
        scoring, so an implementation must score every within-depth path rather
        than truncate before scoring. To stay bounded it MUST detect when more
        paths exist than it can safely score and raise :class:`GraphBackendError`
        rather than returning a silently-truncated (possibly wrong) result — a
        result is always either complete/correct or a loud failure.
        """
        ...

    def scope_person(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        seed_entity_uuid: str,
        *,
        frontier_cap: int,
    ) -> PersonScope:
        """Scope a person to co-mentioned entities + documents (spec §6b step 1).

        Tenant-scoped: ``person -> MENTIONED_IN -> Document <- MENTIONED_IN <-
        co-entity``, excluding the seed. Returns the distinct co-mentioned
        entity set and the connecting document set, deterministically ordered.

        **Bounded ranked-truncation contract:** when more than ``frontier_cap``
        co-mention rows exist the implementation MUST keep the STRONGEST
        ``frontier_cap`` worth of scope — co-entities ranked by co-mention
        frequency with the seed (ties → newest shared document → entity id, a
        total order for reproducibility) — rather than crash, so the headline
        themes/audio surfaces stay usable for hub people. The truncation MUST
        stay within the same bound the cap guaranteed (≤ ``frontier_cap`` rows
        reach downstream) and MUST log a single actionable WARNING naming the
        ``BRAIN_GRAPH_FRONTIER_CAP`` knob — never a silent partial result.
        """
        ...

    def detach_delete_entities(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        entity_uuids: Sequence[str],
    ) -> int:
        """``DETACH DELETE`` specific ``Entity`` vertices for a tenant (spec §7.4).

        The orphan-GC primitive the reconcile layer (G1) calls after the
        relational source-of-truth has dropped a person's last mention: the
        catalog row is gone, so its now-bare AGE vertex (no remaining
        ``MENTIONED_IN`` / ``CO_OCCURS`` edges) must be removed too, or the
        graph drifts from the relational mirror. Only the listed
        ``entity_uuids`` carrying ``tenant_id`` are removed; an empty sequence
        is a no-op. Returns the number of vertices actually deleted (reflects
        reality — a uuid with no matching vertex does not inflate the count).
        Idempotent: re-deleting an already-gone vertex matches nothing and
        contributes zero.
        """
        ...

    def detach_delete_documents(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        document_uuids: Sequence[str],
    ) -> int:
        """``DETACH DELETE`` specific ``Document`` vertices for a tenant (spec §7).

        The document-removal primitive the reconcile layer (G1) calls from
        ``remove_document`` (and from ``reconcile_document`` when an edit drops a
        document to zero person mentions): ``DETACH DELETE`` removes the matched
        ``Document`` vertices together with their attached ``MENTIONED_IN``
        edges. Only the listed ``document_uuids`` carrying ``tenant_id`` are
        removed; an empty sequence is a no-op. Returns the number of vertices
        actually deleted. Idempotent.
        """
        ...

    def clear_tenant(self, conn: psycopg.Connection[Any], tenant_id: str) -> int:
        """Atomically clear ALL of one tenant's vertices + edges (spec §7 rebuild).

        ``DETACH DELETE`` every ``Entity`` and ``Document`` vertex carrying
        ``tenant_id`` (which removes their attached ``MENTIONED_IN`` /
        ``CO_OCCURS`` edges) — including any AGE-only state with no relational
        counterpart (a ``Document`` vertex for a doc removed from the relational
        source, a stale AGE-only ``Entity``). The clear-then-rebuild primitive
        the ``brain graphrag build --force`` path calls FIRST so the AGE mirror
        is rebuilt fresh from the relational source-of-truth with no stale
        survivors. Other tenants are untouched even when they share
        ``entity_uuid`` / ``document_uuid`` values (the tenant property is matched
        on every vertex). Atomic — runs inside its own ``conn.transaction()`` (a
        real transaction on an autocommit connection, a SAVEPOINT when nested),
        so a mid-clear failure rolls back wholesale rather than leaving a
        half-cleared mirror. Returns the number of vertices deleted.
        """
        ...

    def drop_graph(self, conn: psycopg.Connection[Any], tenant_id: str) -> int:
        """Tenant-aware teardown: remove all of one tenant's vertices + edges.

        ``DETACH DELETE`` every ``Entity`` and ``Document`` vertex carrying
        ``tenant_id`` (which removes their attached edges). Other tenants are
        untouched. Returns the number of vertices deleted. Equivalent to
        :meth:`clear_tenant` (teardown vs clear-before-rebuild are the same
        operation); kept as a distinct, intention-revealing name for decommission
        callers.
        """
        ...
