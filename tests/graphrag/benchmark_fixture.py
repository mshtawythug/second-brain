"""Synthetic GraphRAG benchmark-graph generator (wave G1-d; G2 P95 perf gate).

Populates the relational source-of-truth (migration 012) — and optionally the
Apache AGE graph — with a deterministic synthetic graph sized by four knobs:
``entities`` / ``cooccur_edges`` / ``mentions`` / ``tenants`` (spec §16 perf
gate). The headline target G2 measures against is
:data:`BENCHMARK_SPEC_FULL` — **50k entities / 500k CO_OCCURS / 1M mentions /
10 tenants** (spec §16, §17.3).

**This module is NOT a test** (its filename does not match ``test_*``) and is
never collected by the default suite. The default suite runs only the tiny
:mod:`tests.test_graphrag_benchmark_fixture` smoke (≈50 entities), which shares
the EXACT same code path — only ``BenchmarkSpec`` differs — so the generator is
fully covered without ever loading 50k rows in CI.

**How G2 invokes the full-scale load.** From an explicit perf-gate test/script
(e.g. ``pytest -m benchmark`` — a marker registered but excluded from the
default ``addopts``), open an **autocommit** connection to the AGE test instance
and call::

    from tests.graphrag.benchmark_fixture import (
        BENCHMARK_SPEC_FULL, generate_benchmark_graph,
    )
    result = generate_benchmark_graph(conn, BENCHMARK_SPEC_FULL, materialize_age=True)
    # then measure backend.traverse(...) / scope_person(...) P95 latency.

``materialize_age=True`` mirrors the relational graph into AGE via the SAME
``GraphBackend`` primitives the reconcile path uses (``upsert_entities`` +
``upsert_mention_edges`` + ``refresh_cooccur_edges``). At 500k edges that
per-edge Cypher path is slow; tightening it (bulk load) is part of G2's gate
work — G1-d only scaffolds the generator + a correctness smoke.

**Determinism.** Every id is a UUID5 derived from ``(seed, tenant, kind,
index)`` so a given :class:`BenchmarkSpec` always yields the same graph. All
data is synthetic (``Person {i}`` / ``person-<tenant>-<i>``) — no PII.

**Constraints honoured** (migration 012): tenant-scoped composite FKs (entities
loaded before their mentions/edges), ``CHECK (src_id < dst_id)`` canonicalization
(endpoints sorted by UUID), ``weight ∈ (0, 1]``, and per-table PK uniqueness
(unique ``(document_id, entity_id)`` mention pairs and unique unordered entity
pairs by construction).
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import psycopg

from brain.db import DEFAULT_GRAPH_NAME
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.backends.base import GraphBackend
from brain.graph_rag.schema import EntityMention, GraphEntity

__all__ = [
    "BENCHMARK_SPEC_FULL",
    "BenchmarkLoadResult",
    "BenchmarkSpec",
    "generate_benchmark_graph",
]

# Stable namespace for the deterministic UUID5 ids (arbitrary fixed UUID).
_NAMESPACE = uuid.UUID("6f1c0e2a-0b3d-4c5e-8a7b-9d0e1f2a3b4c")


@dataclass(frozen=True)
class BenchmarkSpec:
    """Size knobs for :func:`generate_benchmark_graph`.

    ``entities`` / ``cooccur_edges`` / ``mentions`` / ``tenants`` are split as
    evenly as possible across the tenants. ``documents`` is the total
    ``Document`` count (mention endpoints); when ``None`` it defaults to
    ``max(mentions // 10, entities, tenants)`` (≈10 mentions/doc). ``seed`` makes
    the whole graph reproducible.

    Per-tenant feasibility (validated at generation): ``mentions <= entities *
    documents`` (unique mention pairs) and ``cooccur_edges <= entities*(entities
    -1)//2`` (unique entity pairs); ``entities >= 2`` whenever edges are
    requested.
    """

    entities: int
    cooccur_edges: int
    mentions: int
    tenants: int = 1
    documents: int | None = None
    seed: int = 1234


@dataclass(frozen=True)
class BenchmarkLoadResult:
    """Actual row counts written by :func:`generate_benchmark_graph`."""

    tenants: int
    documents: int
    entities: int
    mentions: int
    contributions: int
    relationships: int
    age_materialized: bool = False


# The spec §16/§17.3 perf-gate target. NOT loaded by the default suite — G2's
# explicit perf gate calls ``generate_benchmark_graph(conn, BENCHMARK_SPEC_FULL,
# materialize_age=True)``.
BENCHMARK_SPEC_FULL = BenchmarkSpec(
    entities=50_000,
    cooccur_edges=500_000,
    mentions=1_000_000,
    tenants=10,
    documents=100_000,
)


def _split(total: int, parts: int) -> list[int]:
    """Split ``total`` into ``parts`` near-even non-negative ints summing to it."""
    if parts < 1:
        raise ValueError(f"parts must be >= 1, got {parts}")
    base, extra = divmod(total, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def _uuid(seed: int, tenant: str, kind: str, index: int) -> uuid.UUID:
    """Deterministic UUID5 for a (seed, tenant, kind, index) tuple."""
    return uuid.uuid5(_NAMESPACE, f"{seed}:{tenant}:{kind}:{index}")


def _default_documents(spec: BenchmarkSpec) -> int:
    """Total document count when ``spec.documents`` is unset (~10 mentions/doc)."""
    if spec.documents is not None:
        return spec.documents
    return max(spec.mentions // 10, spec.entities, spec.tenants, 1)


def _validate_tenant(*, entities: int, documents: int, mentions: int, edges: int) -> None:
    """Reject a per-tenant slice that cannot satisfy the schema constraints."""
    if mentions > entities * documents:
        raise ValueError(
            f"mentions ({mentions}) exceed entities*documents "
            f"({entities}*{documents}) for a tenant — cannot form unique "
            "(entity, document) pairs"
        )
    if edges > 0 and entities < 2:
        raise ValueError(
            f"cooccur_edges ({edges}) requested but tenant has < 2 entities "
            f"({entities})"
        )
    if edges > 0 and mentions < entities:
        # Edge endpoints span all ``entities``; every one must be a mentioned
        # entity for the relational recompute to be coherent (no orphan endpoint).
        raise ValueError(
            f"cooccur_edges ({edges}) require every entity mentioned: "
            f"mentions ({mentions}) must be >= entities ({entities})"
        )
    max_pairs = entities * (entities - 1) // 2
    if edges > max_pairs:
        raise ValueError(
            f"cooccur_edges ({edges}) exceed the max distinct entity pairs "
            f"({max_pairs}) for {entities} entities in a tenant"
        )


def generate_benchmark_graph(
    conn: psycopg.Connection,
    spec: BenchmarkSpec,
    *,
    backend: GraphBackend | None = None,
    materialize_age: bool = False,
    age_bulk: bool = False,
    tenant_names: Sequence[str] | None = None,
) -> BenchmarkLoadResult:
    """Load a deterministic synthetic graph into the relational tables (+ AGE).

    Writes ``documents`` / ``graph_entities`` / ``graph_entity_mentions`` /
    ``graph_edge_contributions`` / ``graph_relationships`` per tenant via COPY,
    in FK-safe order. When ``materialize_age`` is set, also mirrors each tenant's
    entities, mention edges, and CO_OCCURS edges into the Apache AGE graph.

    Two AGE materialization paths:

    * ``age_bulk=False`` (default) — mirror via the ``GraphBackend`` *primitives*
      (``upsert_entities`` + ``upsert_mention_edges`` + ``refresh_cooccur_edges``),
      exactly as the reconcile path does. Correct + fully covered by the smoke,
      but its per-edge Cypher issues millions of round-trips at
      :data:`BENCHMARK_SPEC_FULL` — far beyond the gate's generation budget.
    * ``age_bulk=True`` — the G2 perf-gate fast path: :class:`_BulkAgeLoader`
      COPYs straight into AGE's backing tables, assigning each vertex/edge a
      deterministic ``graphid``. Produces a graph byte-identical (in queryable
      structure + properties) to the primitive path — proven by the
      bulk≡primitive equivalence smoke — but loads the full-scale corpus in
      minutes, not hours. The labels + property indexes must exist first, so
      ``backend.bootstrap`` is always called before either path runs.

    Pass an **autocommit** connection (the AGE bootstrap + per-edge Cypher need
    it, and per-tenant commits keep memory bounded). Returns the actual counts
    written.

    ``tenant_names`` overrides the default ``bench-t{ti}`` tenant ids (one name
    per tenant, ``len == spec.tenants``) — used by the fuse perf gate to append a
    single ``"default"`` tenant on top of the already-loaded corpus (the only
    tenant fuse is gated to; spec §17d dec 6). With ``age_bulk=True`` the bulk
    loader resumes graphids from the live id-sequences, so this APPEND does not
    collide with the existing graph. When ``None`` the historic ``bench-t{ti}``
    naming is used (unchanged).
    """
    if tenant_names is not None and len(tenant_names) != spec.tenants:
        raise ValueError(
            f"tenant_names has {len(tenant_names)} names but spec.tenants is "
            f"{spec.tenants}"
        )
    if materialize_age and backend is None:
        backend = AgeBackend()
    if materialize_age:
        # Labels/indexes must exist before any vertex/edge write. Idempotent.
        assert backend is not None
        backend.bootstrap(conn)

    bulk_loader: _BulkAgeLoader | None = None
    if materialize_age and age_bulk:
        graph_name = getattr(backend, "graph_name", DEFAULT_GRAPH_NAME)
        bulk_loader = _BulkAgeLoader(conn, graph_name)

    entity_splits = _split(spec.entities, spec.tenants)
    edge_splits = _split(spec.cooccur_edges, spec.tenants)
    mention_splits = _split(spec.mentions, spec.tenants)
    doc_splits = _split(_default_documents(spec), spec.tenants)

    total = BenchmarkLoadResult(
        tenants=spec.tenants,
        documents=0,
        entities=0,
        mentions=0,
        contributions=0,
        relationships=0,
        age_materialized=materialize_age,
    )
    for ti in range(spec.tenants):
        tenant = tenant_names[ti] if tenant_names is not None else f"bench-t{ti}"
        docs, ents, ments, contribs, rels = _load_tenant(
            conn,
            spec=spec,
            tenant=tenant,
            entities=entity_splits[ti],
            documents=doc_splits[ti],
            mentions=mention_splits[ti],
            edges=edge_splits[ti],
            # Primitive path only when AGE is on and bulk is off.
            backend=backend if (materialize_age and not age_bulk) else None,
            bulk_loader=bulk_loader,
        )
        total = BenchmarkLoadResult(
            tenants=spec.tenants,
            documents=total.documents + docs,
            entities=total.entities + ents,
            mentions=total.mentions + ments,
            contributions=total.contributions + contribs,
            relationships=total.relationships + rels,
            age_materialized=materialize_age,
        )
    if bulk_loader is not None:
        # setval the per-label id sequences + ANALYZE the backing tables once,
        # after all tenants are loaded (planner stats matter at scale).
        bulk_loader.finalize()
    return total


def _load_tenant(
    conn: psycopg.Connection,
    *,
    spec: BenchmarkSpec,
    tenant: str,
    entities: int,
    documents: int,
    mentions: int,
    edges: int,
    backend: GraphBackend | None,
    bulk_loader: _BulkAgeLoader | None = None,
) -> tuple[int, int, int, int, int]:
    """Load one tenant's slice; return (docs, entities, mentions, contribs, rels).

    Relational COPY always runs. AGE materialization is at most one of:
    ``bulk_loader`` (the fast direct-COPY path) or ``backend`` (the per-edge
    primitive path); ``generate_benchmark_graph`` passes exactly one (or
    neither, for relational-only loads).
    """
    _validate_tenant(
        entities=entities, documents=documents, mentions=mentions, edges=edges
    )
    entity_ids = [_uuid(spec.seed, tenant, "entity", i) for i in range(entities)]
    doc_ids = [_uuid(spec.seed, tenant, "doc", i) for i in range(documents)]

    _copy_documents(conn, tenant, doc_ids)
    _copy_entities(conn, tenant, entity_ids)
    mention_pairs = _mention_pairs(mentions, documents, entities)
    _copy_mentions(conn, tenant, entity_ids, doc_ids, mention_pairs)
    edge_pairs = _edge_pairs(edges, entities)
    _copy_relationships(conn, tenant, entity_ids, edge_pairs)
    _copy_contributions(conn, tenant, entity_ids, doc_ids, edge_pairs)

    if bulk_loader is not None:
        bulk_loader.load_tenant(
            tenant=tenant,
            entity_ids=entity_ids,
            doc_ids=doc_ids,
            mention_pairs=mention_pairs,
            edge_pairs=edge_pairs,
        )
    elif backend is not None:
        _materialize_tenant_age(
            conn,
            backend,
            tenant=tenant,
            entity_ids=entity_ids,
            doc_ids=doc_ids,
            mention_pairs=mention_pairs,
        )
    return (documents, entities, mentions, len(edge_pairs), len(edge_pairs))


# --------------------------------------------------------------------------- #
# Deterministic pair generation (unique by construction)
# --------------------------------------------------------------------------- #
def _mention_pairs(mentions: int, documents: int, entities: int) -> list[tuple[int, int]]:
    """Unique ``(doc_idx, entity_idx)`` pairs spreading both axes.

    ``entity_idx = k % entities`` round-robins entities, so when ``mentions >=
    entities`` EVERY entity is mentioned at least once — which keeps the data
    coherent for the relational recompute (every edge endpoint, all of which are
    ``< entities``, is a mentioned entity). ``doc_idx = (k // entities + k %
    entities) % documents`` is a diagonal sweep that also fans the mentions
    across the documents. The two together are unique for ``mentions <=
    documents * entities`` (collisions require an index gap that is a multiple of
    both ``entities`` and ``documents``).
    """
    return [
        ((k // entities + k % entities) % documents, k % entities)
        for k in range(mentions)
    ]


def _edge_pairs(edges: int, entities: int) -> list[tuple[int, int]]:
    """Unique unordered ``(a_idx, b_idx)`` entity-index pairs (a != b).

    ``a = c % E``; ``gap = 1 + c // E`` (kept well below ``E/2`` for the sizes we
    target, so the wrapped neighbour ``b = (a + gap) % E`` never duplicates an
    earlier unordered pair). Canonicalisation to ``src_id < dst_id`` happens at
    write time by sorting the mapped UUIDs.
    """
    pairs: list[tuple[int, int]] = []
    for c in range(edges):
        a = c % entities
        gap = 1 + c // entities
        b = (a + gap) % entities
        pairs.append((a, b))
    return pairs


# Aggregate edge label (matches ``refresh_cooccur_edges`` / migration 012).
_CO_OCCURS_REL_TYPE = "co_occurs"


def _edge_attrs(index: int) -> tuple[float, int, int]:
    """Deterministic ``(weight, co_count, doc_count)`` for the ``index``-th edge.

    ``weight ∈ (0, 1]`` (so ~20% fall below the default 0.20 floor — exercises
    the traversal weight filter), ``co_count ∈ [1, 5]``, ``doc_count ∈ [1, 3]``.
    Shared by the relational ``graph_relationships`` COPY and the AGE
    ``CO_OCCURS`` bulk load so the two mirrors carry identical edge properties.
    """
    return (((index % 100) + 1) / 100.0, (index % 5) + 1, (index % 3) + 1)


def _canonical_endpoints(
    entity_ids: Sequence[uuid.UUID], a: int, b: int
) -> tuple[uuid.UUID, uuid.UUID]:
    """Return the ``(src, dst)`` endpoint UUIDs sorted ascending (CHECK src<dst)."""
    src, dst = sorted((entity_ids[a], entity_ids[b]))
    return src, dst


# --------------------------------------------------------------------------- #
# Relational COPY loaders
# --------------------------------------------------------------------------- #
def _copy_documents(
    conn: psycopg.Connection, tenant: str, doc_ids: Sequence[uuid.UUID]
) -> None:
    with conn.cursor().copy(
        "COPY documents (id, title, content, content_hash, content_type) FROM STDIN"
    ) as cp:
        for i, doc_id in enumerate(doc_ids):
            cp.write_row(
                (
                    str(doc_id),
                    f"Bench doc {tenant} {i}",
                    f"Synthetic benchmark body for {tenant} doc {i}.",
                    doc_id.hex,  # globally-unique content_hash
                    "note",
                )
            )


def _copy_entities(
    conn: psycopg.Connection, tenant: str, entity_ids: Sequence[uuid.UUID]
) -> None:
    with conn.cursor().copy(
        "COPY graph_entities (id, tenant_id, entity_type, name, canonical_key) "
        "FROM STDIN"
    ) as cp:
        for i, entity_id in enumerate(entity_ids):
            cp.write_row(
                (str(entity_id), tenant, "person", f"Person {i}", f"person-{tenant}-{i}")
            )


def _copy_mentions(
    conn: psycopg.Connection,
    tenant: str,
    entity_ids: Sequence[uuid.UUID],
    doc_ids: Sequence[uuid.UUID],
    mention_pairs: Sequence[tuple[int, int]],
) -> None:
    with conn.cursor().copy(
        "COPY graph_entity_mentions "
        "(tenant_id, entity_id, document_id, mention_count, source) FROM STDIN"
    ) as cp:
        for doc_idx, entity_idx in mention_pairs:
            cp.write_row(
                (
                    tenant,
                    str(entity_ids[entity_idx]),
                    str(doc_ids[doc_idx]),
                    1,
                    "people",
                )
            )


def _copy_relationships(
    conn: psycopg.Connection,
    tenant: str,
    entity_ids: Sequence[uuid.UUID],
    edge_pairs: Sequence[tuple[int, int]],
) -> None:
    with conn.cursor().copy(
        "COPY graph_relationships "
        "(tenant_id, src_id, dst_id, rel_type, weight, co_count, doc_count) "
        "FROM STDIN"
    ) as cp:
        for c, (a, b) in enumerate(edge_pairs):
            src, dst = _canonical_endpoints(entity_ids, a, b)
            weight, co_count, doc_count = _edge_attrs(c)
            cp.write_row(
                (tenant, str(src), str(dst), _CO_OCCURS_REL_TYPE, weight, co_count, doc_count)
            )


def _copy_contributions(
    conn: psycopg.Connection,
    tenant: str,
    entity_ids: Sequence[uuid.UUID],
    doc_ids: Sequence[uuid.UUID],
    edge_pairs: Sequence[tuple[int, int]],
) -> None:
    with conn.cursor().copy(
        "COPY graph_edge_contributions "
        "(tenant_id, document_id, src_id, dst_id, cooccur_count) FROM STDIN"
    ) as cp:
        for c, (a, b) in enumerate(edge_pairs):
            src, dst = _canonical_endpoints(entity_ids, a, b)
            cp.write_row(
                (tenant, str(doc_ids[c % len(doc_ids)]), str(src), str(dst), 1)
            )


# --------------------------------------------------------------------------- #
# AGE materialization (mirrors the reconcile path's primitives)
# --------------------------------------------------------------------------- #
def _materialize_tenant_age(
    conn: psycopg.Connection,
    backend: GraphBackend,
    *,
    tenant: str,
    entity_ids: Sequence[uuid.UUID],
    doc_ids: Sequence[uuid.UUID],
    mention_pairs: Sequence[tuple[int, int]],
) -> None:
    """Mirror one tenant's relational graph into AGE via the backend primitives."""
    backend.upsert_entities(
        conn,
        tenant,
        [
            GraphEntity(
                id=str(entity_id),
                entity_type="person",
                name=f"Person {i}",
                canonical_key=f"person-{tenant}-{i}",
                tenant_id=tenant,
            )
            for i, entity_id in enumerate(entity_ids)
        ],
    )
    # Group mentions by document so MENTIONED_IN edges are rebuilt per doc.
    by_doc: dict[int, list[int]] = {}
    for doc_idx, entity_idx in mention_pairs:
        by_doc.setdefault(doc_idx, []).append(entity_idx)
    for doc_idx, entity_indices in by_doc.items():
        document_id = str(doc_ids[doc_idx])
        backend.upsert_mention_edges(
            conn,
            tenant,
            document_id,
            [
                EntityMention(
                    entity_id=str(entity_ids[ei]),
                    document_id=document_id,
                    source="people",
                    tenant_id=tenant,
                    mention_count=1,
                )
                for ei in entity_indices
            ],
            document_props={"content_type": "note"},
        )
    # Materialize CO_OCCURS from the relational mirror just loaded.
    backend.refresh_cooccur_edges(conn, tenant)


# --------------------------------------------------------------------------- #
# AGE bulk load (the G2 perf-gate fast path — direct COPY into backing tables)
# --------------------------------------------------------------------------- #
# AGE labels mirrored by the bulk loader, in the order the AgeBackend bootstrap
# creates them (vertex labels before edge labels). The loader looks up each
# label's numeric id + id-sequence from ``ag_catalog.ag_label`` (never hard-codes
# them), so it stays correct regardless of catalog ordering.
_AGE_VERTEX_LABELS = ("Entity", "Document")
_AGE_EDGE_LABELS = ("MENTIONED_IN", "CO_OCCURS")
_AGE_LABELS = (*_AGE_VERTEX_LABELS, *_AGE_EDGE_LABELS)

# An AGE ``graphid`` packs the 16-bit label id in the high bits and a per-label
# entry sequence in the low 48 bits: ``graphid = (label_id << 48) | entry``
# (validated against live AGE 1.5.0). The bulk loader assigns entries itself.
_GRAPHID_SEQ_BITS = 48


class _BulkAgeLoader:
    """Fast AGE materializer: COPY straight into the graph's backing tables.

    The primitive path (:func:`_materialize_tenant_age`) mirrors the reconcile
    primitives one Cypher statement at a time — correct but far too slow at
    :data:`BENCHMARK_SPEC_FULL` (millions of round-trips). This loader instead
    streams rows into ``"<graph>"."Entity"`` / ``"Document"`` / ``"MENTIONED_IN"``
    / ``"CO_OCCURS"`` via ``COPY ... FROM STDIN``, assigning each vertex/edge a
    deterministic ``graphid``. The resulting graph is queryable by the SAME
    ``AgeBackend.traverse`` / ``scope_person`` Cypher the primitive path feeds
    (the property maps are identical) — proven by the bulk≡primitive equivalence
    smoke in ``tests/test_graphrag_benchmark_fixture.py``.

    The labels + property indexes must already exist (the caller runs
    ``backend.bootstrap`` first). Per-label entry counters are shared across
    tenants because a label's backing table holds every tenant's rows, so each
    entry (and therefore each graphid) must be unique across the whole load.

    Connection must be autocommit (matching the rest of the generator).
    """

    def __init__(self, conn: psycopg.Connection, graph_name: str) -> None:
        self._conn = conn
        self._graph = graph_name
        self._label_id, self._seq_name = self._load_label_meta()
        # Next unused entry (low-48-bit id) per label, RESUMED from each label's
        # current id-sequence value so a bulk load can APPEND to an existing graph
        # (e.g. the fuse perf gate adds a 'default' tenant on top of the loaded
        # benchmark corpus) without re-emitting — and colliding on — a graphid.
        # On a fresh graph the sequence is un-called (last_value=1,
        # is_called=false), so this resolves to 1 — identical to from-scratch.
        self._next: dict[str, int] = self._load_seq_starts()

    def _load_seq_starts(self) -> dict[str, int]:
        """Resume each label's next entry from its id-sequence current value.

        AGE packs a per-label entry counter into the low 48 bits of every
        ``graphid``. :meth:`finalize` ``setval``s the sequence past the
        bulk-assigned entries, so a SECOND bulk load (appending another tenant)
        must start where the sequence left off, not at 1, or it would re-emit
        colliding graphids. A fresh label sequence is ``is_called=false`` with
        ``last_value=1`` → next entry 1 (the from-scratch case, unchanged).
        """
        starts: dict[str, int] = {}
        for label in _AGE_LABELS:
            row = self._conn.execute(
                f'SELECT last_value, is_called FROM "{self._graph}".'
                f'"{self._seq_name[label]}"'
            ).fetchone()
            if row is None:
                starts[label] = 1
                continue
            last_value, is_called = int(row[0]), bool(row[1])
            starts[label] = last_value + 1 if is_called else last_value
        return starts

    def _load_label_meta(self) -> tuple[dict[str, int], dict[str, str]]:
        """Read each label's numeric id + id-sequence name from ``ag_label``."""
        rows = self._conn.execute(
            "SELECT l.name, l.id, l.seq_name FROM ag_catalog.ag_label l "
            "JOIN ag_catalog.ag_graph g ON l.graph = g.graphid "
            "WHERE g.name = %s AND l.name = ANY(%s)",
            (self._graph, list(_AGE_LABELS)),
        ).fetchall()
        label_id = {str(name): int(lid) for name, lid, _ in rows}
        seq_name = {str(name): str(seq) for name, _, seq in rows}
        missing = [label for label in _AGE_LABELS if label not in label_id]
        if missing:
            raise RuntimeError(
                f"AGE labels {missing} missing in graph {self._graph!r}; call "
                "backend.bootstrap before the bulk load"
            )
        return label_id, seq_name

    def _graphid(self, label: str, entry: int) -> int:
        """Pack ``(label_id, entry)`` into an AGE ``graphid`` integer."""
        return (self._label_id[label] << _GRAPHID_SEQ_BITS) | entry

    def _alloc(self, label: str, count: int) -> list[int]:
        """Reserve ``count`` consecutive graphids for ``label`` (cross-tenant)."""
        start = self._next[label]
        self._next[label] = start + count
        return [self._graphid(label, start + offset) for offset in range(count)]

    def _copy(self, label: str, columns: str, rows: Iterable[tuple]) -> None:
        """COPY ``rows`` into the fully-qualified ``<graph>.<label>`` table."""
        statement = f'COPY "{self._graph}"."{label}" ({columns}) FROM STDIN'
        with self._conn.cursor().copy(statement) as cp:
            for row in rows:
                cp.write_row(row)

    def load_tenant(
        self,
        *,
        tenant: str,
        entity_ids: Sequence[uuid.UUID],
        doc_ids: Sequence[uuid.UUID],
        mention_pairs: Sequence[tuple[int, int]],
        edge_pairs: Sequence[tuple[int, int]],
    ) -> None:
        """Bulk-load one tenant's Entity/Document vertices + edges via COPY."""
        entity_gids = self._alloc("Entity", len(entity_ids))
        doc_gids = self._alloc("Document", len(doc_ids))

        # Entity vertices — properties match AgeBackend.upsert_entities.
        self._copy(
            "Entity",
            "id, properties",
            (
                (
                    entity_gids[i],
                    json.dumps(
                        {
                            "entity_uuid": str(entity_id),
                            "tenant_id": tenant,
                            "name": f"Person {i}",
                            "entity_type": "person",
                            "canonical_key": f"person-{tenant}-{i}",
                        }
                    ),
                )
                for i, entity_id in enumerate(entity_ids)
            ),
        )
        # Document vertices — properties match upsert_mention_edges' MERGE + the
        # ``document_props={"content_type": "note"}`` the primitive path passes.
        self._copy(
            "Document",
            "id, properties",
            (
                (
                    doc_gids[i],
                    json.dumps(
                        {
                            "document_uuid": str(doc_id),
                            "tenant_id": tenant,
                            "content_type": "note",
                        }
                    ),
                )
                for i, doc_id in enumerate(doc_ids)
            ),
        )
        # MENTIONED_IN edges (Entity -> Document) — one per mention pair.
        mention_gids = self._alloc("MENTIONED_IN", len(mention_pairs))
        mention_props = json.dumps(
            {"tenant_id": tenant, "mention_count": 1, "source": "people"}
        )
        self._copy(
            "MENTIONED_IN",
            "id, start_id, end_id, properties",
            (
                (
                    mention_gids[k],
                    entity_gids[entity_idx],
                    doc_gids[doc_idx],
                    mention_props,
                )
                for k, (doc_idx, entity_idx) in enumerate(mention_pairs)
            ),
        )
        # CO_OCCURS edges (Entity -> Entity, canonical src<dst) — properties
        # match refresh_cooccur_edges (weight/co_count/doc_count/rel_type).
        cooccur_gids = self._alloc("CO_OCCURS", len(edge_pairs))
        self._copy(
            "CO_OCCURS",
            "id, start_id, end_id, properties",
            (
                self._cooccur_row(
                    cooccur_gids[c], tenant, entity_ids, entity_gids, c, a, b
                )
                for c, (a, b) in enumerate(edge_pairs)
            ),
        )

    def _cooccur_row(
        self,
        edge_gid: int,
        tenant: str,
        entity_ids: Sequence[uuid.UUID],
        entity_gids: Sequence[int],
        index: int,
        a: int,
        b: int,
    ) -> tuple[int, int, int, str]:
        """Build one CO_OCCURS COPY row (canonical src<dst by UUID)."""
        # Canonicalize to src<dst by UUID, mirroring the relational mirror, and
        # map back to the matching entity graphid for each endpoint.
        src_idx, dst_idx = (a, b) if entity_ids[a] < entity_ids[b] else (b, a)
        weight, co_count, doc_count = _edge_attrs(index)
        props = json.dumps(
            {
                "tenant_id": tenant,
                "weight": weight,
                "co_count": co_count,
                "doc_count": doc_count,
                "rel_type": _CO_OCCURS_REL_TYPE,
            }
        )
        return (edge_gid, entity_gids[src_idx], entity_gids[dst_idx], props)

    def finalize(self) -> None:
        """Advance each label's id sequence past the bulk-assigned entries + ANALYZE.

        ``setval`` keeps any later ``MERGE`` (e.g. an equivalence-smoke traversal
        that touches the graph) from re-issuing an already-used graphid. ``ANALYZE``
        gives the planner real row stats — essential for sane traversal plans at
        full scale.
        """
        for label in _AGE_LABELS:
            last_entry = self._next[label] - 1
            if last_entry >= 1:
                self._conn.execute(
                    f'SELECT setval(\'"{self._graph}"."{self._seq_name[label]}"\', '
                    "%s, true)",
                    (last_entry,),
                )
            self._conn.execute(f'ANALYZE "{self._graph}"."{label}"')
