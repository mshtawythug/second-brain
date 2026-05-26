"""Apache AGE implementation of :class:`GraphBackend` (spec §4 D1/D10, §5b, §6a).

The graph lives in one AGE graph inside Postgres; this backend translates the
narrow Protocol into parameterized openCypher. It is the wave-G0 retrieval
canary — its traversal/scope Cypher was validated against a live AGE 1.5.0
instance before any later wave depended on it.

The instance-state-free helpers (module constants, agtype parsing, input
validation, the inline property-map builder, the property-index DDL builder, and
the **relational** ``scope_person`` two-hop) live in
:mod:`brain.graph_rag.backends._age_helpers` (the G2 wave-boundary file-size
split); this module owns the ``AgeBackend`` class — session handling, Cypher
execution, and the Protocol methods — and imports those helpers by their original
names. The split is behavior-preserving.

Empirical AGE behaviour this module is built around (proven on live AGE 1.5.0,
not assumed):

* **Call shape.** ``ag_catalog.cypher('<graph>', $$ <query> $$, <params>) AS
  (<col> ag_catalog.agtype)``. The graph name is a ``name`` constant and the
  query a ``cstring`` constant — *neither* can be a ``%s`` placeholder. Only the
  third ``params`` argument is bound, as ``%s::ag_catalog.agtype`` over a JSON
  string; Cypher references those values as ``$key``. We therefore embed the
  (validated) graph name as a literal and route every dynamic *value* through
  the agtype params map — values are never string-formatted into Cypher.
* **search_path.** Any non-trivial Cypher (a ``WHERE`` comparison, a ``MERGE``)
  needs the ``ag_catalog`` operators/casts (``=``→boolean, ``@>`` containment).
  We ``SET search_path = ag_catalog, "$user", public`` for the duration of those
  statements and ``RESET`` immediately (see :meth:`_age_session`) — never a
  global leak, matching ``tests/conftest._reset_age_graph`` and
  :func:`brain.db.load_age`. Catalog DDL (``create_graph``/``create_vlabel``)
  and the property-index DDL are fully ``ag_catalog``-qualified and need no
  search_path.
* **MERGE + SET.** A ``SET`` in the same statement as a ``MERGE`` that *creates*
  the element does **not** persist (and AGE rejects ``ON CREATE SET`` /
  ``ON MATCH SET``). So property-bearing upserts are two statements: ``MERGE``
  to get-or-create, then a separate ``MATCH ... SET x += {map}`` that always
  lands on the match path. Aggregate edges are delete-then-``CREATE`` with
  inline properties (``CREATE`` always persists).
* **Traversal returns paths, scoring is Python.** AGE has no reliable list
  comprehension (``[r IN ... | ...]``) / ``reduce`` / ``all``. The traversal
  returns ``relationships(path)`` + ``nodes(path)`` as agtype; Python parses
  them, enforces the per-element tenant filter, applies the edge-weight floor,
  and computes ``∏ weights`` affinity (spec §6a).
"""
from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.pq import TransactionStatus

from ...db import DEFAULT_GRAPH_NAME, bootstrap_age
from ...errors import GraphBackendError
from ..schema import EntityMention, GraphEntity
from ._age_helpers import (
    _DOCUMENT_RESERVED_KEYS,
    _EDGE_LABELS,
    _ENTITY_RESERVED_KEYS,
    _GRAPH_NAME_RE,
    _PROPERTY_INDEXES,
    _TRAVERSE_MAX_PATHS_FLOOR,
    _TRAVERSE_MAX_PATHS_MULTIPLIER,
    _VERTEX_LABELS,
    _agtype_loads,
    _all_same_tenant,
    _edge_weight,
    _inline_set_map,
    _property_index_ddl,
    _require_autocommit,
    _require_positive_int,
    _require_weight_floor,
    _scope_person_relational,
)
from .base import PersonScope, TraversalHit


class AgeBackend:
    """Apache AGE backend — parameterized Cypher over one tenant-scoped graph.

    Conforms structurally to :class:`brain.graph_rag.backends.base.GraphBackend`.
    Construct once (cheap; holds only the validated graph name) and pass a
    connection per call. The connection must have AGE loaded
    (:func:`brain.db.connect_age`); :meth:`bootstrap` additionally requires an
    autocommit connection (catalog DDL).
    """

    def __init__(self, graph_name: str = DEFAULT_GRAPH_NAME) -> None:
        if not _GRAPH_NAME_RE.match(graph_name):
            raise GraphBackendError(
                f"invalid AGE graph name {graph_name!r}: must be a SQL identifier "
                r"matching ^[A-Za-z_][A-Za-z0-9_]*$"
            )
        self._graph_name = graph_name

    @property
    def graph_name(self) -> str:
        """The validated AGE graph this backend operates on."""
        return self._graph_name

    # ------------------------------------------------------------------ #
    # Session / execution helpers
    # ------------------------------------------------------------------ #
    @contextmanager
    def _age_session(self, conn: psycopg.Connection[Any]) -> Iterator[None]:
        """Put ``ag_catalog`` on the search_path for the enclosed Cypher.

        Required so AGE's operators/casts (``=``, ``@>``, agtype→boolean)
        resolve. Restores the default search_path on exit — never a global leak.
        If the transaction aborted inside the block, the RESET is skipped (the
        caller's rollback restores search_path; issuing SQL on an aborted
        transaction would itself error).
        """
        conn.execute('SET search_path = ag_catalog, "$user", public')
        try:
            yield
        finally:
            if conn.info.transaction_status != TransactionStatus.INERROR:
                conn.execute("RESET search_path")

    def _cypher(
        self,
        conn: psycopg.Connection[Any],
        query: str,
        params: Mapping[str, Any] | None = None,
        *,
        columns: str = "v ag_catalog.agtype",
    ) -> list[tuple[Any, ...]]:
        """Run one ``ag_catalog.cypher`` statement and return its rows.

        ``query`` is a backend-generated Cypher template (it may reference
        ``$key`` params); ``params`` supplies those values and is the *only*
        bound input, sent as ``%s::ag_catalog.agtype`` over a JSON string.
        ``columns`` is the generated ``AS (...)`` column list. Any
        ``psycopg.Error`` is wrapped in :class:`GraphBackendError`.
        """
        graph = self._graph_name
        if params is None:
            statement = (
                f"SELECT * FROM ag_catalog.cypher('{graph}', $$ {query} $$) "
                f"AS ({columns})"
            )
            args: tuple[Any, ...] = ()
        else:
            statement = (
                f"SELECT * FROM ag_catalog.cypher('{graph}', $$ {query} $$, "
                f"%s::ag_catalog.agtype) AS ({columns})"
            )
            args = (json.dumps(params),)
        try:
            return conn.execute(statement, args).fetchall()
        except psycopg.Error as exc:
            raise GraphBackendError(
                f"Cypher execution failed on graph {graph!r}: {exc}"
            ) from exc

    def _label_exists(self, conn: psycopg.Connection[Any], label: str) -> bool:
        """True iff ``label`` already exists in this backend's graph."""
        row = conn.execute(
            "SELECT 1 FROM ag_catalog.ag_label l "
            "JOIN ag_catalog.ag_graph g ON l.graph = g.graphid "
            "WHERE g.name = %s AND l.name = %s",
            (self._graph_name, label),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ #
    # bootstrap
    # ------------------------------------------------------------------ #
    def bootstrap(self, conn: psycopg.Connection[Any]) -> None:
        """Provision graph + labels + property indexes, idempotently (spec §5b)."""
        _require_autocommit(conn, "AgeBackend.bootstrap")
        # 1. Ensure the graph (+ extension) exists. Reuses the G0-2 bootstrap,
        #    which is itself idempotent (creates the graph only when absent).
        bootstrap_age(conn, self._graph_name)
        try:
            # 2. Labels. create_vlabel/create_elabel are NOT idempotent (they
            #    raise InvalidSchemaName on a second call), so guard each via the
            #    ag_label catalog. Fully ag_catalog-qualified -> no search_path.
            for label in _VERTEX_LABELS:
                if not self._label_exists(conn, label):
                    conn.execute(
                        "SELECT ag_catalog.create_vlabel(%s, %s)",
                        (self._graph_name, label),
                    )
            for label in _EDGE_LABELS:
                if not self._label_exists(conn, label):
                    conn.execute(
                        "SELECT ag_catalog.create_elabel(%s, %s)",
                        (self._graph_name, label),
                    )
            # 3. Property indexes on the label backing tables. CREATE INDEX IF
            #    NOT EXISTS is idempotent; the expression is fully ag_catalog-
            #    qualified, so no search_path is required.
            for label, prop, idx_name in _PROPERTY_INDEXES:
                conn.execute(
                    _property_index_ddl(self._graph_name, label, prop, idx_name)
                )
        except psycopg.Error as exc:
            raise GraphBackendError(
                f"failed to bootstrap AGE labels/indexes on graph "
                f"{self._graph_name!r}: {exc}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def upsert_entities(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        entities: Sequence[GraphEntity],
    ) -> int:
        """MERGE Entity vertices, then MATCH+SET their mutable properties.

        **Atomic, all-or-nothing** (matches :meth:`refresh_cooccur_edges` /
        :meth:`_detach_delete_vertices`): the whole MERGE+SET batch runs inside one
        ``conn.transaction()`` — a real transaction on an autocommit connection, a
        SAVEPOINT when already nested (e.g. under the reconcile transaction or the
        ``build --force`` pre-pass) — so a failure partway through rolls the batch
        back rather than leaving some vertices created and others not.

        **Batched (perf-T4 G2).** A single ``UNWIND $rows`` Cypher MERGEs every
        vertex in one round-trip, then a second ``UNWIND $rows`` Cypher MATCHes
        and SETs each vertex's mutable properties. The two statements are split
        because a ``SET`` in the same statement as a freshly-creating MERGE does
        not persist (AGE quirk — see this module's preamble). Two round-trips
        per call instead of ``2 × N`` (13,190 statements for a 6,595-entity
        build becomes 2).
        """
        if not entities:
            return 0
        # Validate cross-tenant + build the per-row params upfront, BEFORE
        # opening the AGE session, so a bad input never touches the graph.
        for entity in entities:
            if entity.tenant_id != tenant_id:
                raise GraphBackendError(
                    f"cross-tenant entity upsert: entity {entity.id} carries "
                    f"tenant_id {entity.tenant_id!r} but the call scopes "
                    f"tenant_id {tenant_id!r}"
                )
        # Fixed backend-controlled keys (not a caller bag) — run the reserved-key
        # guard once on a sample so a future edit that added an identity key here
        # would still be caught, matching the per-row form's defence in depth.
        _inline_set_map(
            {"name": "", "entity_type": "", "canonical_key": ""},
            reserved=_ENTITY_RESERVED_KEYS,
            context="entity properties",
        )
        rows = [
            {
                "u": entity.id,
                "name": entity.name,
                "entity_type": entity.entity_type,
                "canonical_key": entity.canonical_key,
            }
            for entity in entities
        ]
        with self._age_session(conn), conn.transaction():
            # Pass 1: MERGE every vertex in one Cypher round-trip. AGE's UNWIND
            # iterates the agtype list parameter; the MERGE pattern reads the
            # per-row identity keys via ``row.<key>`` map access.
            self._cypher(
                conn,
                "UNWIND $rows AS row "
                "MERGE (e:Entity {entity_uuid: row.u, tenant_id: $t})",
                {"t": tenant_id, "rows": rows},
            )
            # Pass 2: MATCH each vertex (always on the match path → SET persists)
            # and update its mutable properties. The keys are fixed identifiers
            # interpolated into Cypher; the values flow as agtype params via row.
            self._cypher(
                conn,
                "UNWIND $rows AS row "
                "MATCH (e:Entity {entity_uuid: row.u, tenant_id: $t}) "
                "SET e.name = row.name, "
                "e.entity_type = row.entity_type, "
                "e.canonical_key = row.canonical_key",
                {"t": tenant_id, "rows": rows},
            )
        return len(entities)

    def upsert_mention_edges(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        document_id: str,
        mentions: Sequence[EntityMention],
        *,
        document_props: Mapping[str, Any] | None = None,
    ) -> int:
        """Split-MERGE the doc + entities, then delete+recreate MENTIONED_IN."""
        with self._age_session(conn):
            # 1. MERGE the Document vertex (split MERGE — vertex first).
            self._cypher(
                conn,
                "MERGE (d:Document {document_uuid: $d, tenant_id: $t})",
                {"d": document_id, "t": tenant_id},
            )
            if document_props:
                # Caller-supplied bag — reject any attempt to overwrite the
                # Document's identity keys before building the SET map.
                map_clause, map_params = _inline_set_map(
                    document_props,
                    reserved=_DOCUMENT_RESERVED_KEYS,
                    context="document_props",
                )
                self._cypher(
                    conn,
                    "MATCH (d:Document {document_uuid: $d, tenant_id: $t}) "
                    f"SET d += {map_clause}",
                    {"d": document_id, "t": tenant_id, **map_params},
                )
            # 2. MERGE each Entity vertex (defensive get-or-create; properties
            #    are owned by upsert_entities, so no SET here).
            for mention in mentions:
                if mention.tenant_id != tenant_id or mention.document_id != document_id:
                    raise GraphBackendError(
                        "mention does not match the call scope: "
                        f"got tenant_id={mention.tenant_id!r} "
                        f"document_id={mention.document_id!r}, expected "
                        f"tenant_id={tenant_id!r} document_id={document_id!r}"
                    )
                self._cypher(
                    conn,
                    "MERGE (e:Entity {entity_uuid: $e, tenant_id: $t})",
                    {"e": mention.entity_id, "t": tenant_id},
                )
            # 3. Delete this document's existing MENTIONED_IN edges (recreate
            #    below) — keeps re-ingest idempotent (spec §7.3).
            self._cypher(
                conn,
                "MATCH (:Entity)-[r:MENTIONED_IN {tenant_id: $t}]->"
                "(d:Document {document_uuid: $d, tenant_id: $t}) DELETE r",
                {"d": document_id, "t": tenant_id},
            )
            # 4. Recreate the edges with inline properties (CREATE persists).
            for mention in mentions:
                self._cypher(
                    conn,
                    "MATCH (e:Entity {entity_uuid: $e, tenant_id: $t}) "
                    "MATCH (d:Document {document_uuid: $d, tenant_id: $t}) "
                    "CREATE (e)-[:MENTIONED_IN "
                    "{tenant_id: $t, mention_count: $c, source: $s}]->(d)",
                    {
                        "e": mention.entity_id,
                        "d": document_id,
                        "t": tenant_id,
                        "c": mention.mention_count,
                        "s": mention.source,
                    },
                )
        return len(mentions)

    def refresh_cooccur_edges(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
    ) -> int:
        """Rematerialize CO_OCCURS from the relational ``graph_relationships`` mirror.

        Reads the (already weighted) aggregate rows for the tenant, then in AGE
        deletes the tenant's CO_OCCURS edges and recreates them. Computing the
        normalized-lift weight from contributions is upstream (G1 weighting);
        this primitive mirrors the relational aggregate into the graph.

        **Atomic, complete-or-loud-failure contract** (matches :meth:`traverse`
        / :meth:`scope_person`): the returned count is the number of edges
        ACTUALLY created, never an optimistic ``len(rows)``. Each recreate is a
        ``MATCH (a) MATCH (b) CREATE ... RETURN`` — if either endpoint Entity
        vertex is missing, both MATCHes fail to bind, the CREATE silently
        produces nothing, and ``RETURN`` yields zero rows. We DETECT that empty
        result and raise :class:`GraphBackendError` rather than over-reporting a
        write that didn't happen. (The relational ``graph_relationships`` mirror
        carries tenant-safe FKs to ``graph_entities`` rows, but the AGE *vertex*
        is provisioned separately by :meth:`upsert_entities`; a missing vertex
        means the caller refreshed edges before upserting entities — a bug worth
        surfacing, not swallowing.)

        The delete + all recreates run inside ONE explicit transaction
        (``conn.transaction()`` — a real transaction under an autocommit
        connection, a SAVEPOINT when already inside one). A detected miss (or any
        other failure mid-rebuild) raises, which rolls the whole block back: the
        tenant's CO_OCCURS set is either FULLY replaced or left exactly as it
        was — never the partial state a bare delete-then-create would leave
        (old edges already dropped, only some recreated) on an autocommit
        connection.
        """
        # Read the relational mirror first, under the default search_path so the
        # public table resolves cleanly.
        rows = conn.execute(
            "SELECT src_id, dst_id, weight, co_count, doc_count, rel_type "
            "FROM graph_relationships WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchall()
        with self._age_session(conn), conn.transaction():
            # Full recompute, ATOMIC: drop the tenant's aggregate edges, then
            # recreate — all inside one transaction so a miss below rolls the
            # delete back too (no partial replacement).
            self._cypher(
                conn,
                "MATCH ()-[r:CO_OCCURS {tenant_id: $t}]->() DELETE r",
                {"t": tenant_id},
            )
            if not rows:
                return 0
            # Batched (perf-T4 G1): one UNWIND Cypher per call instead of N
            # round-trips. AGE iterates the agtype list parameter; each row's
            # endpoint UUIDs / edge properties flow through ``row.<key>`` map
            # access. ``RETURN row.s, row.d`` emits one result row per (a,b)
            # pair where BOTH MATCHes bound and the CREATE ran — when either
            # MATCH misses, the UNWIND row simply produces nothing. Comparing
            # the returned (src,dst) set against the input set lets us preserve
            # the original complete-or-loud-failure miss-detection contract.
            edge_rows = [
                {
                    "s": str(src_id),
                    "d": str(dst_id),
                    "w": float(weight),
                    "co": int(co_count),
                    "dc": int(doc_count),
                    "rt": rel_type,
                }
                for src_id, dst_id, weight, co_count, doc_count, rel_type in rows
            ]
            result = self._cypher(
                conn,
                "UNWIND $rows AS row "
                "MATCH (a:Entity {entity_uuid: row.s, tenant_id: $t}) "
                "MATCH (b:Entity {entity_uuid: row.d, tenant_id: $t}) "
                "CREATE (a)-[:CO_OCCURS {tenant_id: $t, weight: row.w, "
                "co_count: row.co, doc_count: row.dc, rel_type: row.rt}]->(b) "
                "RETURN row.s, row.d",
                {"t": tenant_id, "rows": edge_rows},
                columns="s ag_catalog.agtype, d ag_catalog.agtype",
            )
            created = len(result)
            if created < len(edge_rows):
                # At least one MATCH missed → the relational mirror references
                # an entity with no AGE vertex. Identify the FIRST missing pair
                # for a precise message (matches the prior per-row form's error
                # shape), then raise. Raising aborts conn.transaction() → the
                # DELETE and any CREATEs in this rebuild roll back, so the prior
                # CO_OCCURS set is preserved intact (all-or-nothing).
                returned = {(_agtype_loads(s), _agtype_loads(d)) for s, d in result}
                first_missing = next(
                    ((r["s"], r["d"]) for r in edge_rows
                     if (r["s"], r["d"]) not in returned),
                    (None, None),
                )
                raise GraphBackendError(
                    "refresh_cooccur_edges could not create a CO_OCCURS edge "
                    f"for tenant {tenant_id!r}: an endpoint Entity vertex is "
                    f"missing (src={first_missing[0]} dst={first_missing[1]}). "
                    "The relational graph_relationships mirror references an "
                    "entity with no AGE vertex — run upsert_entities before "
                    "refreshing edges."
                )
        return created

    # ------------------------------------------------------------------ #
    # Reads / traversal (the §6a canary)
    # ------------------------------------------------------------------ #
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
        """Bounded CO_OCCURS traversal; Python scores affinity (spec §6a).

        The seed is never returned (excluded in Cypher AND skipped in Python).
        The functional ``frontier_cap`` is applied in Python *after* affinity
        scoring + dedupe + the tenant re-check, so the highest-affinity entities
        are returned deterministically — a Cypher ``LIMIT`` before scoring would
        drop arbitrary (possibly best) paths since AGE cannot order by affinity
        (no reduce/list-comprehension). Ties on affinity break by ``entity_uuid``
        ascending, so the top-``frontier_cap`` selection is fully reproducible.

        **Correctness-or-failure contract:** traverse() scores EVERY within-depth
        path. To stay bounded it fetches at most ``max(frontier_cap *
        _TRAVERSE_MAX_PATHS_MULTIPLIER, _TRAVERSE_MAX_PATHS_FLOOR)`` paths and
        *detects* overflow (it asks AGE for one extra row); if more paths exist
        than the bound it raises :class:`GraphBackendError` rather than returning
        a silently-truncated, possibly-wrong best-per-entity. So a result is
        either complete/correct or a loud failure. Tightening the bound / perf at
        scale is deferred to G2's P95 gate.

        Tenant safety: every path edge is filtered inline (``{tenant_id: $t}``)
        and both queried endpoints are constrained in the ``WHERE``. AGE has no
        ``all()``/list-comprehension to constrain *intermediate* nodes in-query,
        so a Python per-element re-check over ``nodes(path)``/``relationships
        (path)`` drops any path touching a foreign element BEFORE the cap is
        applied — no foreign vertex/edge is ever returned. (Relational
        composite-FK integrity already prevents a same-tenant edge from pointing
        at a foreign vertex, so the re-check is defence in depth, not the sole
        guard.)
        """
        _require_positive_int(depth, "depth")
        _require_positive_int(frontier_cap, "frontier_cap")
        _require_weight_floor(min_edge_weight)
        # ``depth`` is baked into the fixed ``*1..N`` template (a variable-length
        # pattern cannot use a bound depth). ``max_paths`` is the correctness
        # bound: we fetch one EXTRA row (``LIMIT max_paths + 1``) so that >
        # max_paths rows means the result would be incomplete → raise instead of
        # silently truncating. Both values are validated ints.
        max_paths = max(
            frontier_cap * _TRAVERSE_MAX_PATHS_MULTIPLIER,
            _TRAVERSE_MAX_PATHS_FLOOR,
        )
        fetch_limit = max_paths + 1
        query = (
            f"MATCH path = (s:Entity)-[:CO_OCCURS*1..{depth} "
            "{tenant_id: $t}]-(n:Entity) "
            "WHERE s.entity_uuid = $seed AND s.tenant_id = $t AND n.tenant_id = $t "
            "AND n.entity_uuid <> $seed "
            "RETURN n.entity_uuid AS eid, relationships(path) AS rels, "
            f"nodes(path) AS nds, length(path) AS hops LIMIT {fetch_limit}"
        )
        with self._age_session(conn):
            rows = self._cypher(
                conn,
                query,
                {"seed": seed_entity_uuid, "t": tenant_id},
                columns=(
                    "eid ag_catalog.agtype, rels ag_catalog.agtype, "
                    "nds ag_catalog.agtype, hops ag_catalog.agtype"
                ),
            )
        if len(rows) > max_paths:
            # More within-depth paths than we can safely score → returning now
            # would be a silently-truncated (possibly wrong) best-per-entity.
            # Fail loudly instead (perf redesign is G2's P95 gate).
            raise GraphBackendError(
                f"traversal exceeded safe path bound ({max_paths}) at "
                f"depth={depth} from seed {seed_entity_uuid!r}; narrow the scope "
                "(lower depth) — perf redesign for large frontiers is deferred to "
                "G2"
            )

        # Score every fetched path in Python, then dedupe to the best per entity,
        # sort by affinity (ties broken by entity_uuid for determinism), and ONLY
        # THEN apply the functional frontier cap.
        best: dict[str, TraversalHit] = {}
        for eid_raw, rels_raw, nds_raw, hops_raw in rows:
            eid = _agtype_loads(eid_raw)
            # Belt + suspenders: never return the seed even if a cycle slips past
            # the Cypher ``n.entity_uuid <> $seed`` guard.
            if eid == seed_entity_uuid:
                continue
            rels = _agtype_loads(rels_raw) or []
            nodes = _agtype_loads(nds_raw) or []
            hops = int(_agtype_loads(hops_raw))
            # Per-element tenant re-check (incl. intermediate nodes) BEFORE the
            # cap, so a foreign path can never consume frontier budget.
            if not _all_same_tenant(rels, nodes, tenant_id):
                continue
            weights = [_edge_weight(edge) for edge in rels]
            if any(w < min_edge_weight for w in weights):
                continue
            affinity = 1.0
            for w in weights:
                affinity *= w
            current = best.get(eid)
            if current is None or affinity > current.affinity:
                best[eid] = TraversalHit(
                    entity_uuid=eid,
                    affinity=affinity,
                    hops=hops,
                    tenant_id=tenant_id,
                )
        # Affinity DESC, ties broken by entity_uuid ASC → reproducible ordering
        # (and therefore a reproducible top-frontier_cap selection).
        hits = sorted(best.values(), key=lambda h: (-h.affinity, h.entity_uuid))
        return hits[:frontier_cap]

    def scope_person(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        seed_entity_uuid: str,
        *,
        frontier_cap: int,
    ) -> PersonScope:
        """Person -> docs -> co-mentioned entities, tenant-scoped (spec §6b).

        Delegates to the instance-state-free
        :func:`brain.graph_rag.backends._age_helpers._scope_person_relational`
        (an indexed RELATIONAL two-hop over ``graph_entity_mentions`` — NOT
        Cypher — see there for the full rationale + the complete-or-loud-failure
        overflow contract). Kept as a method so the backend satisfies the
        :class:`~brain.graph_rag.backends.base.GraphBackend` Protocol.
        """
        return _scope_person_relational(
            conn, tenant_id, seed_entity_uuid, frontier_cap=frontier_cap
        )

    # ------------------------------------------------------------------ #
    # Targeted vertex GC (reconcile orphan cleanup — spec §7)
    # ------------------------------------------------------------------ #
    def detach_delete_entities(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        entity_uuids: Sequence[str],
    ) -> int:
        """DETACH DELETE specific Entity vertices for a tenant (orphan GC)."""
        return self._detach_delete_vertices(
            conn, "Entity", "entity_uuid", tenant_id, entity_uuids
        )

    def detach_delete_documents(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        document_uuids: Sequence[str],
    ) -> int:
        """DETACH DELETE specific Document vertices for a tenant (+ their edges)."""
        return self._detach_delete_vertices(
            conn, "Document", "document_uuid", tenant_id, document_uuids
        )

    def _detach_delete_vertices(
        self,
        conn: psycopg.Connection[Any],
        label: str,
        id_prop: str,
        tenant_id: str,
        uuids: Sequence[str],
    ) -> int:
        """Count-then-``DETACH DELETE`` each listed vertex of ``label``.

        ``label`` / ``id_prop`` are backend constants (``Entity``/``entity_uuid``
        or ``Document``/``document_uuid``), embedded in the generated Cypher; the
        uuid + tenant are bound agtype params. Each vertex is counted first so
        the returned total reflects vertices that ACTUALLY existed (a uuid with
        no matching vertex contributes zero) — AGE's ``DELETE`` returns no rows
        to count, so a preceding ``RETURN count(v)`` is the reliable signal.
        An empty ``uuids`` sequence is a no-op.

        **Atomic, all-or-nothing** (matches :meth:`refresh_cooccur_edges`): the
        whole count/delete loop runs inside one ``conn.transaction()`` — a real
        transaction on an autocommit connection, a SAVEPOINT when already nested
        inside the reconcile transaction. A failure partway through rolls the
        block back, so a direct caller can never observe a partial deletion.
        """
        if not uuids:
            return 0
        deleted = 0
        with self._age_session(conn), conn.transaction():
            for vertex_uuid in uuids:
                rows = self._cypher(
                    conn,
                    f"MATCH (v:{label} {{{id_prop}: $u, tenant_id: $t}}) "
                    "RETURN count(v)",
                    {"u": vertex_uuid, "t": tenant_id},
                    columns="c ag_catalog.agtype",
                )
                present = int(_agtype_loads(rows[0][0])) if rows else 0
                if present:
                    self._cypher(
                        conn,
                        f"MATCH (v:{label} {{{id_prop}: $u, tenant_id: $t}}) "
                        "DETACH DELETE v",
                        {"u": vertex_uuid, "t": tenant_id},
                    )
                    deleted += present
        return deleted

    # ------------------------------------------------------------------ #
    # Teardown
    # ------------------------------------------------------------------ #
    def clear_tenant(self, conn: psycopg.Connection[Any], tenant_id: str) -> int:
        """Atomically DETACH DELETE all of one tenant's Entity + Document vertices.

        The clear-then-rebuild primitive (spec §7) ``build --force`` runs FIRST,
        so the AGE mirror is rebuilt fresh from the relational source-of-truth
        with no stale survivors — a ``Document`` vertex for a doc dropped from the
        relational source, an AGE-only ``Entity`` with no catalog row, and their
        ``MENTIONED_IN`` / ``CO_OCCURS`` edges are all removed. Only vertices
        carrying ``tenant_id`` are touched (tenant-safe even with shared
        ``entity_uuid`` / ``document_uuid`` values).

        **Atomic, all-or-nothing** (matches :meth:`refresh_cooccur_edges` /
        :meth:`_detach_delete_vertices`): the count + both DETACH DELETEs run
        inside one ``conn.transaction()`` — a real transaction on an autocommit
        connection, a SAVEPOINT when nested — so a failure between the two
        deletes rolls the whole clear back rather than leaving the Entity set
        gone but the Document set intact. Returns the number of vertices deleted.
        """
        with self._age_session(conn), conn.transaction():
            deleted = self._count_vertices(conn, "Entity", tenant_id) + (
                self._count_vertices(conn, "Document", tenant_id)
            )
            # DETACH DELETE removes attached MENTIONED_IN / CO_OCCURS edges too.
            self._cypher(
                conn,
                "MATCH (e:Entity {tenant_id: $t}) DETACH DELETE e",
                {"t": tenant_id},
            )
            self._cypher(
                conn,
                "MATCH (d:Document {tenant_id: $t}) DETACH DELETE d",
                {"t": tenant_id},
            )
        return deleted

    def drop_graph(self, conn: psycopg.Connection[Any], tenant_id: str) -> int:
        """Tenant-aware teardown — DETACH DELETE the tenant's vertices.

        Delegates to :meth:`clear_tenant` (teardown and clear-before-rebuild are
        the same operation): kept as a distinct, intention-revealing name for
        decommission callers, and inherits ``clear_tenant``'s atomicity.
        """
        return self.clear_tenant(conn, tenant_id)

    def count_entities(self, conn: psycopg.Connection[Any], tenant_id: str) -> int:
        """Count the tenant's ``Entity`` vertices (doctor drift check; spec §7).

        Read-only public primitive the ``brain doctor`` graph-drift check uses to
        compare the AGE mirror against the relational ``graph_entities`` count.
        Opens its own :meth:`_age_session` (the caller need not). Any
        ``psycopg.Error`` surfaces as :class:`GraphBackendError`.
        """
        with self._age_session(conn):
            return self._count_vertices(conn, "Entity", tenant_id)

    def count_cooccur_edges(
        self, conn: psycopg.Connection[Any], tenant_id: str
    ) -> int:
        """Count the tenant's ``CO_OCCURS`` edges (doctor drift check; spec §7).

        Read-only public primitive the ``brain doctor`` graph-drift check uses to
        compare the AGE mirror against the relational ``graph_relationships``
        count. Opens its own :meth:`_age_session`. Any ``psycopg.Error`` surfaces
        as :class:`GraphBackendError`.
        """
        with self._age_session(conn):
            rows = self._cypher(
                conn,
                "MATCH ()-[r:CO_OCCURS {tenant_id: $t}]->() RETURN count(r)",
                {"t": tenant_id},
                columns="c ag_catalog.agtype",
            )
        return int(_agtype_loads(rows[0][0])) if rows else 0

    def _count_vertices(
        self, conn: psycopg.Connection[Any], label: str, tenant_id: str
    ) -> int:
        """Count vertices of a fixed ``label`` carrying ``tenant_id``.

        ``label`` is a backend constant (``Entity``/``Document``), embedded in
        the generated Cypher; only ``tenant_id`` is a bound parameter. Must run
        inside an :meth:`_age_session`.
        """
        rows = self._cypher(
            conn,
            f"MATCH (v:{label} {{tenant_id: $t}}) RETURN count(v)",
            {"t": tenant_id},
            columns="c ag_catalog.agtype",
        )
        return int(_agtype_loads(rows[0][0])) if rows else 0
