"""Pure (instance-state-free) helpers + constants for the AGE backend (G2 split).

Extracted from :mod:`brain.graph_rag.backends.age` (the G2 wave-boundary
file-size split, mirroring the G1 :mod:`brain.graph_rag.aggregates` / G2-c
:mod:`brain.graph_rag.relational` extractions) so
:class:`~brain.graph_rag.backends.age.AgeBackend` stays a focused class under the
file-size cap. Everything here is free of ``AgeBackend`` instance state — module
constants, agtype parsing, input validation, the inline Cypher property-map
builder, the BTREE property-index DDL builder, and the **relational**
``scope_person`` two-hop query. The :meth:`AgeBackend.scope_person` Protocol
method delegates to :func:`_scope_person_relational` here; the rest of the
backend imports the constants + validators by their original names, so this is a
pure move with no behavior change.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg import sql

from ...errors import GraphBackendError
from .base import PersonScope

_logger = logging.getLogger(__name__)

# AGE graph names are SQL identifiers (they back a Postgres schema). The name is
# a controlled config value, never user input, but it is embedded as a literal
# in the ``cypher()`` call (it cannot be a bound parameter), so we validate it
# defensively before use — an invalid name is a caller bug, not a DB failure.
_GRAPH_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Cypher property keys must be plain identifiers. AGE rejects ``SET x += $param``
# (a bare param as the map — "SET clause expects a map"), so dynamic property
# maps are built inline with the *keys* interpolated (validated against this
# pattern to bar injection) and the *values* routed through agtype params.
_PROP_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Reserved identity keys per label — a ``SET x += {...}`` over a caller property
# bag must NEVER overwrite these (doing so would move/strand a vertex across its
# tenant/UUID identity, breaking the scoped MERGE + delete/create idempotency).
# Note: ``canonical_key`` is deliberately NOT reserved for entities — it is a
# legitimately mutable property that ``upsert_entities`` owns (the AGE vertex
# identity is ``(entity_uuid, tenant_id)``, not canonical_key).
_ENTITY_RESERVED_KEYS = frozenset({"tenant_id", "entity_uuid"})
_DOCUMENT_RESERVED_KEYS = frozenset({"tenant_id", "document_uuid"})

# AGE serializes graph elements with a trailing ``::edge`` / ``::vertex`` /
# ``::path`` type annotation appended to otherwise-valid JSON. Strip it before
# ``json.loads``.
_AGTYPE_ANNOTATION_RE = re.compile(r"::(?:edge|vertex|path|numeric)\b")

# Vertex/edge labels (spec §5b). Vertex labels back the tables the property
# indexes target, so they are created before the edge labels and indexes.
_VERTEX_LABELS = ("Entity", "Document")
_EDGE_LABELS = ("MENTIONED_IN", "CO_OCCURS")

# Property indexes (spec §5b): (label, property, index-name). Index names live
# in the graph's own schema, so they need no graph prefix.
_PROPERTY_INDEXES = (
    ("Entity", "tenant_id", "idx_entity_tenant_id"),
    ("Entity", "entity_uuid", "idx_entity_entity_uuid"),
    ("Entity", "canonical_key", "idx_entity_canonical_key"),
    ("CO_OCCURS", "weight", "idx_cooccur_weight"),
)

# Traversal correctness bound. The *functional* frontier cap is applied in
# Python AFTER affinity scoring (a Cypher ``LIMIT`` before scoring would
# silently drop the highest-affinity entities — AGE cannot ``ORDER BY`` true
# affinity = ∏ weights, having no list-comprehension/reduce). So traverse() must
# score EVERY within-depth path. To stay bounded against a pathological fan-out
# it fetches at most this many paths and DETECTS overflow: if more paths exist
# than the bound, it raises rather than returning a silently-truncated (possibly
# wrong) best-per-entity. The bound is generous; tightening/perf is G2's P95
# gate. Result: traverse() is either correct or a loud failure, never silently
# wrong.
_TRAVERSE_MAX_PATHS_MULTIPLIER = 50
_TRAVERSE_MAX_PATHS_FLOOR = 5000


def _agtype_loads(raw: Any) -> Any:
    """Parse one agtype column value (a ``str`` or ``None``) into Python.

    Strips AGE's ``::edge``/``::vertex``/``::path`` annotations, then
    ``json.loads`` — which covers scalars (``"x"``→str, ``3``→int,
    ``1.5``→float, ``null``→None) and the list/dict shapes of
    ``relationships``/``nodes`` results.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):  # pragma: no cover - psycopg yields agtype as str
        return raw
    cleaned = _AGTYPE_ANNOTATION_RE.sub("", raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GraphBackendError(
            f"could not parse agtype result: {raw!r}"
        ) from exc


def _require_autocommit(conn: psycopg.Connection[Any], operation: str) -> None:
    """Guard operations whose AGE catalog DDL needs autocommit (psycopg v3)."""
    if not conn.autocommit:
        raise GraphBackendError(
            f"{operation} requires an autocommit connection — AGE catalog DDL "
            "does not run reliably inside an open transaction under psycopg v3"
        )


def _require_positive_int(value: int, name: str) -> None:
    """Validate a value is a genuine positive int before Cypher interpolation.

    ``depth`` / ``frontier_cap`` are interpolated into the generated Cypher
    template, and a type hint is NOT an injection boundary — so verify the
    value is an actual ``int`` (``bool`` is an ``int`` subclass; exclude it)
    and ``>= 1`` *before* it ever reaches an f-string. A non-int is a caller
    bug, surfaced as :class:`GraphBackendError`.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise GraphBackendError(
            f"{name} must be an int, got {type(value).__name__}"
        )
    if value < 1:
        raise GraphBackendError(f"{name} must be >= 1, got {value}")


def _require_weight_floor(value: float) -> None:
    """Validate ``min_edge_weight`` is a real number in ``[0, 1]``.

    Not interpolated into Cypher (compared in Python), but a non-numeric
    value would raise a raw ``TypeError`` at comparison time — convert that
    to a typed :class:`GraphBackendError` here. ``bool`` is rejected as a
    nonsensical weight floor.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GraphBackendError(
            f"min_edge_weight must be a number, got {type(value).__name__}"
        )
    if not 0.0 <= value <= 1.0:
        raise GraphBackendError(
            f"min_edge_weight must be in [0, 1], got {value}"
        )


def _inline_set_map(
    props: Mapping[str, Any],
    *,
    reserved: frozenset[str],
    context: str,
) -> tuple[str, dict[str, Any]]:
    """Build an inline Cypher property map ``{k: $pk, ...}`` + its params.

    AGE rejects ``SET x += $param`` (a bare param map), so a dynamic map is
    emitted inline with the *keys* interpolated (validated as identifiers to
    bar injection) and the *values* bound as agtype params (``p0``, ``p1``,
    ...). ``reserved`` is the set of identity keys this map must NOT contain
    — a caller property bag that tries to overwrite e.g. ``tenant_id`` /
    ``document_uuid`` is a caller bug, rejected with :class:`GraphBackendError`
    (``context`` names the offending bag for the message).
    """
    parts: list[str] = []
    params: dict[str, Any] = {}
    for index, (key, value) in enumerate(props.items()):
        if key in reserved:
            raise GraphBackendError(
                f"{context} may not overwrite the reserved identity key "
                f"{key!r}"
            )
        if not _PROP_KEY_RE.match(key):
            raise GraphBackendError(
                f"invalid property key {key!r}: must be a Cypher identifier "
                r"matching ^[A-Za-z_][A-Za-z0-9_]*$"
            )
        param_key = f"p{index}"
        parts.append(f"{key}: ${param_key}")
        params[param_key] = value
    return "{" + ", ".join(parts) + "}", params


def _all_same_tenant(
    rels: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    tenant_id: str,
) -> bool:
    """True iff every edge and node in the path carries ``tenant_id``."""
    return all(
        element.get("properties", {}).get("tenant_id") == tenant_id
        for element in (*rels, *nodes)
    )


def _edge_weight(edge: Mapping[str, Any]) -> float:
    """Extract a CO_OCCURS edge's ``weight`` property (required)."""
    props = edge.get("properties", {})
    weight = props.get("weight")
    if weight is None:
        raise GraphBackendError(
            f"CO_OCCURS edge missing required 'weight' property: {edge!r}"
        )
    return float(weight)


def _property_index_ddl(
    graph_name: str, label: str, prop: str, idx_name: str
) -> sql.Composed:
    """Build the BTREE property-index DDL for ``<graph>.<label>(prop)``."""
    return sql.SQL(
        "CREATE INDEX IF NOT EXISTS {idx} ON {tbl} USING btree "
        "(ag_catalog.agtype_access_operator(properties, {prop}::ag_catalog.agtype))"
    ).format(
        idx=sql.Identifier(idx_name),
        tbl=sql.Identifier(graph_name, label),
        prop=sql.Literal(f'"{prop}"'),
    )


def _scope_person_relational(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    seed_entity_uuid: str,
    *,
    frontier_cap: int,
) -> PersonScope:
    """Person -> docs -> co-mentioned entities, tenant-scoped (spec §6b).

    **Indexed RELATIONAL two-hop over ``graph_entity_mentions`` — NOT Cypher.**
    The earlier Cypher form (a ``MENTIONED_IN`` self-join) made AGE materialize a
    tenant-wide join *before* restricting to the seed: AGE cannot estimate
    ``agtype``-property selectivity, so its planner badly under-counts the seed
    filter and builds hundreds of thousands of full vertex objects, OOM-killing
    Postgres at the G2-k full-scale corpus (50k entities / 1M mentions / 10
    tenants). The relational two-hop is the same shape the rest of the themes
    path already uses (:mod:`brain.graph_rag.themes` computes df / lift / edges
    relationally from ``graph_entity_mentions`` / ``graph_edge_contributions``),
    and it is fully btree-index-supported:

    * **hop 1** — the seed's documents: ``graph_entity_mentions`` filtered by
      ``(tenant_id, entity_id = seed)``, served by the PK
      ``(tenant_id, entity_id, document_id)``.
    * **hop 2** — entities co-mentioned in those documents: the ``co``
      self-join on ``(tenant_id, document_id)``, served by
      ``idx_gem_document (tenant_id, document_id)`` (migration 012).

    So per-query cost scales with the SEED's document count × per-document
    fan-out, never the whole tenant's mention set, and the planner has sound
    row estimates (no ``agtype`` opacity). The result is identical in shape
    to the prior Cypher: the distinct co-mentioned entity set (seed
    excluded) + the distinct connecting documents (those where the seed and
    at least one other entity are both mentioned), tenant-scoped on every
    clause and deterministically sorted.

    **Why this backend reads relational here while :meth:`AgeBackend.traverse`
    stays on Cypher:** ``scope_person`` returns a *set* whose only graph
    structure is a one-hop-shared-document join — exactly what an indexed
    relational query does best, and what :meth:`AgeBackend.refresh_cooccur_edges`
    already reads relationally too. The variable-length affinity walk in
    :meth:`AgeBackend.traverse` is the genuine graph workload and remains on AGE
    Cypher (it passes the G2-k local P95 gate). A future
    :class:`~brain.graph_rag.backends.base.GraphBackend` is free to implement
    ``scope_person`` against its own store.

    **Bounded ranked-truncation contract** (DIFFERS from
    :meth:`AgeBackend.traverse`): a scope is a *set*, and the cheap relational
    two-hop above is the same indexed query downstream df/lift/grouping already
    runs — so when a hub person's co-mention set exceeds ``frontier_cap`` the
    right answer is to keep the STRONGEST ``frontier_cap`` worth of scope, not to
    crash. The common (under-cap) path fetches one extra row
    (``LIMIT frontier_cap + 1``) and, when within bounds, returns the COMPLETE
    set unchanged — identical behavior + cost to the prior form, no extra query.
    Only on overflow does it fall through to :func:`_scope_person_truncated`,
    which ranks co-entities by co-mention frequency (ties → newest shared doc →
    entity id) and keeps the strongest prefix whose cumulative ``(co-entity,
    document)`` row count stays within ``frontier_cap`` — preserving the exact
    downstream bound the safe-cap was introduced to guarantee (the df/lift/
    grouping work stays ≤ ``frontier_cap`` rows), and logging ONE actionable
    WARNING (kept/total counts + the ``BRAIN_GRAPH_FRONTIER_CAP`` knob). The
    returned ``entity_uuids`` / ``document_uuids`` are sorted ascending so the
    result is deterministic regardless of row order.

    **Why this truncates while :meth:`AgeBackend.traverse` still raises:**
    ``traverse`` runs a variable-length Cypher affinity walk whose cap protects
    against genuine *path explosion* — it cannot ``ORDER BY`` true affinity in
    AGE, so it must score every within-depth path and a pre-score ``LIMIT`` would
    silently drop the highest-affinity entities. Here the ranking key
    (co-mention frequency) is computable directly in indexed SQL, so a correct,
    deterministic top-``frontier_cap`` selection is cheap — truncation is safe
    and is exactly what the headline themes/audio surfaces need for hub people.
    """
    # frontier_cap bounds the fetched rows (overflow detection). It is a
    # bound parameter, not interpolated, but the positive-int contract
    # mirrors the prior form.
    _require_positive_int(frontier_cap, "frontier_cap")
    # Fetch one extra row to DETECT overflow (see contract above). The seed
    # uuid is bound (parameterized) on every clause — no string formatting of
    # inputs into SQL. ``entity_id`` / ``document_id`` are uuid columns; the
    # str params follow the established scalar convention (e.g.
    # ``brain.graph_rag.relational.read_doc_mentions``).
    fetch_limit = frontier_cap + 1
    try:
        rows = conn.execute(
            "SELECT DISTINCT co.entity_id::text AS eid, "
            "seed.document_id::text AS did "
            "FROM graph_entity_mentions AS seed "
            "JOIN graph_entity_mentions AS co "
            "  ON co.tenant_id = seed.tenant_id "
            "  AND co.document_id = seed.document_id "
            "WHERE seed.tenant_id = %s AND seed.entity_id = %s "
            "  AND co.entity_id <> %s "
            "LIMIT %s",
            (tenant_id, seed_entity_uuid, seed_entity_uuid, fetch_limit),
        ).fetchall()
    except psycopg.Error as exc:
        raise GraphBackendError(
            f"relational person scope failed for seed {seed_entity_uuid!r} "
            f"in tenant {tenant_id!r}: {exc}"
        ) from exc
    if len(rows) > frontier_cap:
        # More co-mention rows than the bound → keep the strongest frontier_cap
        # worth of scope (deterministic ranked truncation + WARNING), never a
        # crash. Bounded exactly as before: the truncated scope feeds ≤
        # frontier_cap rows downstream.
        return _scope_person_truncated(
            conn, tenant_id, seed_entity_uuid, frontier_cap=frontier_cap
        )
    entity_uuids: set[str] = set()
    document_uuids: set[str] = set()
    for eid, did in rows:
        entity_uuids.add(str(eid))
        document_uuids.add(str(did))
    # Sort ascending so the (set-valued) scope is returned deterministically.
    return PersonScope(
        seed_entity_uuid=seed_entity_uuid,
        entity_uuids=tuple(sorted(entity_uuids)),
        document_uuids=tuple(sorted(document_uuids)),
        tenant_id=tenant_id,
    )


def _scope_person_truncated(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    seed_entity_uuid: str,
    *,
    frontier_cap: int,
) -> PersonScope:
    """Deterministically truncate an over-cap person scope to its strongest part.

    Reached only from :func:`_scope_person_relational` when the seed's distinct
    ``(co-entity, document)`` co-mention rows exceed ``frontier_cap``. Ranks the
    co-mentioned entities by co-mention frequency with the seed (DESC), breaking
    ties by newest shared document (DESC) then entity id (ASC) — a total order,
    so the selection is reproducible across runs. It then keeps the strongest
    prefix of entities whose cumulative co-mention-row count stays within
    ``frontier_cap`` (always keeping at least the top entity), and resolves their
    connecting documents (themselves capped to ``frontier_cap``, newest-first, to
    bound the rare single-dominant-entity case). One actionable WARNING is logged
    — never silent, never a crash. The result preserves the downstream envelope
    the safe-cap guaranteed: at most ``frontier_cap`` entity rows and at most
    ``frontier_cap`` documents reach the df/lift/grouping stage.
    """
    # Rank query: one row per co-entity with its shared-doc count + newest shared
    # doc. Bounded by the SEED's co-entity neighborhood (the legitimate scope,
    # index-served via the gem PK + idx_gem_document), never a tenant-wide scan —
    # and we only ever keep a ≤ frontier_cap prefix of it. All inputs bound
    # (parameterized); the seed uuid casts against the uuid columns as elsewhere.
    try:
        ranked = conn.execute(
            "SELECT co.entity_id::text AS eid, "
            "COUNT(DISTINCT seed.document_id) AS comention_count, "
            "MAX(d.ingested_at) AS newest_doc "
            "FROM graph_entity_mentions AS seed "
            "JOIN graph_entity_mentions AS co "
            "  ON co.tenant_id = seed.tenant_id "
            "  AND co.document_id = seed.document_id "
            "JOIN documents AS d ON d.id = seed.document_id "
            "WHERE seed.tenant_id = %s AND seed.entity_id = %s "
            "  AND co.entity_id <> %s "
            "GROUP BY co.entity_id "
            "ORDER BY comention_count DESC, newest_doc DESC, eid ASC",
            (tenant_id, seed_entity_uuid, seed_entity_uuid),
        ).fetchall()
    except psycopg.Error as exc:
        raise GraphBackendError(
            f"relational person scope ranking failed for seed "
            f"{seed_entity_uuid!r} in tenant {tenant_id!r}: {exc}"
        ) from exc
    total_rows = sum(int(count) for _eid, count, _newest in ranked)
    # Greedy prefix by the (co-entity, document) row budget. Each kept entity is
    # included with ALL its shared docs (a partial entity would distort its
    # in-scope df), so we stop before the first entity that would overflow the
    # cap. The top entity is always kept (its docs are bounded below) so a single
    # dominant co-entity never collapses the scope to empty.
    kept_entities: list[str] = []
    kept_rows = 0
    for eid, count, _newest in ranked:
        count_int = int(count)
        if kept_entities and kept_rows + count_int > frontier_cap:
            break
        kept_entities.append(str(eid))
        kept_rows += count_int
    document_uuids = _scope_connecting_documents(
        conn, tenant_id, seed_entity_uuid, kept_entities, frontier_cap=frontier_cap
    )
    _logger.warning(
        "graphrag person scope for seed %s exceeded frontier_cap=%d: kept the %d "
        "strongest co-entities (%d of %d co-mention rows); raise "
        "BRAIN_GRAPH_FRONTIER_CAP to widen the scope.",
        seed_entity_uuid,
        frontier_cap,
        len(kept_entities),
        kept_rows,
        total_rows,
    )
    return PersonScope(
        seed_entity_uuid=seed_entity_uuid,
        entity_uuids=tuple(sorted(kept_entities)),
        document_uuids=document_uuids,
        tenant_id=tenant_id,
    )


def _scope_connecting_documents(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    seed_entity_uuid: str,
    kept_entities: list[str],
    *,
    frontier_cap: int,
) -> tuple[str, ...]:
    """Connecting documents for the kept co-entities, bounded + deterministic.

    The distinct documents in which the seed and at least one ``kept_entities``
    member are both mentioned. In the common truncated case this is already ≤
    ``frontier_cap`` (the greedy budget bounds the kept rows); only the rare
    single-dominant-entity case (one co-entity sharing more than ``frontier_cap``
    docs with the seed) can exceed it, and there we keep the ``frontier_cap``
    newest documents (``ingested_at`` DESC, id ASC) so the document array also
    stays within the cap. Returned sorted ascending for determinism.
    """
    if not kept_entities:
        return ()
    try:
        rows = conn.execute(
            "SELECT DISTINCT seed.document_id::text AS did, "
            "d.ingested_at AS doc_ts "
            "FROM graph_entity_mentions AS seed "
            "JOIN graph_entity_mentions AS co "
            "  ON co.tenant_id = seed.tenant_id "
            "  AND co.document_id = seed.document_id "
            "JOIN documents AS d ON d.id = seed.document_id "
            "WHERE seed.tenant_id = %s AND seed.entity_id = %s "
            "  AND co.entity_id = ANY(%s)",
            (tenant_id, seed_entity_uuid, kept_entities),
        ).fetchall()
    except psycopg.Error as exc:
        raise GraphBackendError(
            f"relational person scope documents failed for seed "
            f"{seed_entity_uuid!r} in tenant {tenant_id!r}: {exc}"
        ) from exc
    if len(rows) <= frontier_cap:
        return tuple(sorted(str(did) for did, _doc_ts in rows))
    # Single dominant co-entity overflowed the cap alone: keep the newest
    # frontier_cap docs. Stable two-pass sort (id ASC, then ingested_at DESC)
    # gives a total newest-first order with an id tiebreak.
    by_id = sorted(rows, key=lambda row: str(row[0]))
    newest_first = sorted(by_id, key=lambda row: row[1], reverse=True)
    kept_docs = [str(did) for did, _doc_ts in newest_first[:frontier_cap]]
    return tuple(sorted(kept_docs))
