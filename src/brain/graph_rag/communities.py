"""Global community detection over the tenant entity graph (wave G3-b, spec §17c).

The callable CORE of ``brain graphrag communities build|refresh`` (the CLI/MCP
surfaces land in G3-f). No summaries, no embeddings, no retrieval — just the
networkx-Louvain partition + Jaccard stable-identity + dirty/delta gating and
the relational persistence of :class:`~brain.graph_rag.schema.CommunityRecord` /
:class:`~brain.graph_rag.schema.CommunityMember` rows.

**RELATIONAL-only (§17c Q2).** The input graph is built from a single
tenant-scoped read of ``graph_relationships`` (the lift-weighted edge mirror,
migration 012) — NOT an AGE traversal — and the output communities live entirely
in ``graph_communities`` / ``graph_community_members`` (migration 013). AGE keeps
only ``Entity``/``Document`` + ``MENTIONED_IN``/``CO_OCCURS``; there is no
``Community`` vertex.

**Dirty gate (§17c Q3).** :func:`build_communities` computes the tenant's
``source_graph_hash`` — a deterministic hash over the ordered edge set — and
SKIPS (no-op) when the stored communities' ``(build_version, source_graph_hash)``
already matches and ``force`` is False. ``communities refresh`` (force=True)
bypasses the gate. Detection / Jaccard matching run ONLY after the gate fires;
there is no per-query Louvain (batched at build/refresh per LazyGraphRAG §4 D2).
Because the fingerprint is carried ON the community rows, a tenant with zero
materialized communities (empty graph, or every partition below ``min_size``) is
always re-evaluated — which is cheap (Louvain over an empty/tiny graph).

**Stable identity (§17c Q3/Q7).** Each newly-detected community's member set is
greedy-best-Jaccard matched (threshold ``BRAIN_GRAPH_COMMUNITY_JACCARD``) against
the EXISTING stored communities for the tenant; a match at or above the threshold
(and with non-zero overlap) reuses that ``community_key`` — preserving the
summary row — while an unmatched community mints a fresh UUID. ``members_hash``
(a deterministic hash over the sorted member entity ids) is the per-community
identity. The Jaccard helper is the shared :func:`brain.set_similarity.jaccard`
(§17c Q7).

**Summary delta-gate (§17c Q3/Q10).** A reused row is UPDATEd in place, which
NEVER touches ``summary`` / ``summary_model`` / ``summary_at`` /
``summary_embedding``: a still-valid summary is preserved across rebuilds, and a
membership change is recorded via the updated ``members_hash`` so G3-c can refresh
the (now stale) summary later. Minted rows start with NULL summary fields.

**Determinism.** Louvain is randomized, so the configured ``seed`` is threaded
through and the partition + member ordering are deterministically sorted
(communities by ``members_hash``; members by descending weighted degree then
entity id). Combined with the dirty gate (a second build on an unchanged graph
SKIPS) and Jaccard key reuse, repeated builds on the same graph converge to
byte-stable rows (timestamps aside).
"""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import networkx as nx
import psycopg
from networkx.algorithms.community import louvain_communities

from ..config import Config
from ..errors import GraphTenantError
from ..set_similarity import jaccard
from .schema import CommunityMember, CommunityRecord

__all__ = [
    "BUILD_VERSION",
    "CommunityBuildResult",
    "DetectedCommunity",
    "build_communities",
    "compute_members_hash",
    "compute_source_graph_hash",
    "detect_communities",
    "list_communities",
    "match_communities",
]

# Detection algorithm version. MUST match the ``graph_communities.build_version``
# DB default (migration 013) so the dirty gate's stored-fingerprint comparison
# is meaningful. Bump when the partitioning semantics change (a new Louvain
# variant, a different weighting input) so a rebuild is forced corpus-wide.
BUILD_VERSION = "networkx-louvain-v1"


@dataclass(frozen=True)
class DetectedCommunity:
    """One community produced by :func:`detect_communities` (pre-persistence).

    ``members`` are the ranked :class:`~brain.graph_rag.schema.CommunityMember`s
    WITHOUT an assigned ``community_key`` (the key is matched/minted at persist).
    ``members_hash`` is the per-community identity over the sorted member ids;
    the aggregate stats describe the community subgraph.
    """

    members_hash: str
    members: tuple[CommunityMember, ...]
    member_count: int
    edge_count: int
    total_weight: float

    @property
    def member_ids(self) -> frozenset[str]:
        """The community's entity-id set (for Jaccard stable-identity matching)."""
        return frozenset(member.entity_id for member in self.members)


@dataclass(frozen=True)
class CommunityBuildResult:
    """Tally of a :func:`build_communities` run.

    ``communities_total`` is the number of communities materialized for the
    tenant after the run (the stored count when ``skipped``). ``created`` /
    ``reused`` / ``deleted`` partition the change: minted keys, Jaccard-reused
    keys, and removed keys (no longer present). ``dirty`` is True when the
    tenant's ``source_graph_hash`` differs from the stored fingerprint (the graph
    genuinely changed since the last build) — independent of ``force``, so a
    forced rebuild of an unchanged graph reports ``dirty=False`` with
    ``skipped=False``. ``skipped`` is True only when the dirty gate fired (graph
    unchanged AND not forced) and no work ran.
    """

    tenant_id: str
    source_graph_hash: str = ""
    communities_total: int = 0
    created: int = 0
    reused: int = 0
    deleted: int = 0
    dirty: bool = False
    skipped: bool = False


# --------------------------------------------------------------------------- #
# Pure helpers (no DB) — hashing, detection, stable-identity matching.
# --------------------------------------------------------------------------- #
def compute_members_hash(member_ids: Iterable[str]) -> str:
    """Deterministic per-community identity hash over the member entity ids.

    Sorts the ids (so call-order never matters) and SHA-256s the newline-joined
    string. Entity ids are UUID text (no newline), so the join is unambiguous.
    """
    joined = "\n".join(sorted(str(mid) for mid in member_ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def compute_source_graph_hash(edges: Iterable[tuple[str, str, float]]) -> str:
    """Deterministic dirty fingerprint over the tenant's edge set (§17c Q3).

    Sorts the ``(src, dst, weight)`` triples by ``(src, dst)`` (the endpoints are
    already canonical ``src < dst`` per migration 012) and SHA-256s the
    tab/newline-joined rendering. ``weight`` is rendered with ``repr`` — Python's
    shortest round-trippable float string, which is injective over floats — so a
    genuine weight change always moves the hash and an equal weight never
    spuriously does (same rationale as ``weighting.suppress_ver``). An empty edge
    set hashes the empty string (a fixed constant), so a graph with no
    relationships has a stable, well-defined fingerprint.
    """
    parts = [
        f"{src}\t{dst}\t{weight!r}"
        for src, dst, weight in sorted(edges, key=lambda edge: (edge[0], edge[1]))
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def detect_communities(
    edges: Sequence[tuple[str, str, float]],
    *,
    resolution: float,
    seed: int,
    min_size: int,
    max_communities: int | None = None,
) -> list[DetectedCommunity]:
    """Partition the weighted edge set into communities (pure; no DB).

    Builds an undirected weighted :class:`networkx.Graph` from ``edges``, runs
    ``louvain_communities`` with the configured ``resolution`` + ``seed``, DROPS
    partitions smaller than ``min_size`` (sub-threshold groups are not
    materialized — spec §6c / the ``BRAIN_GRAPH_COMMUNITY_MIN_SIZE`` knob), and
    computes each surviving community's identity hash + ranked members + subgraph
    stats. When ``max_communities`` is set and exceeded, the LARGEST communities
    are kept (by member count, then total weight, then ``members_hash`` for a
    deterministic tie-break) — the ops cap bounds the downstream summary +
    embedding cost (§17c Q8). The returned list is sorted by ``members_hash`` for
    byte-stable output independent of Louvain's internal partition order.
    """
    graph: nx.Graph = nx.Graph()
    for src, dst, weight in edges:
        graph.add_edge(str(src), str(dst), weight=float(weight))
    if graph.number_of_nodes() == 0:
        return []

    partitions = louvain_communities(
        graph, weight="weight", resolution=resolution, seed=seed
    )

    detected: list[DetectedCommunity] = []
    for nodes in partitions:
        if len(nodes) < min_size:
            continue
        detected.append(_build_detected(graph, nodes))

    # Ops safety valve (§17c Q8): keep the largest communities, deterministically.
    if max_communities is not None and len(detected) > max_communities:
        detected = sorted(
            detected,
            key=lambda dc: (-dc.member_count, -dc.total_weight, dc.members_hash),
        )[:max_communities]

    # Byte-stable output order, independent of Louvain's partition ordering.
    detected.sort(key=lambda dc: dc.members_hash)
    return detected


def _build_detected(graph: nx.Graph, nodes: Iterable[str]) -> DetectedCommunity:
    """Assemble one :class:`DetectedCommunity` from a Louvain partition.

    Members are ranked by descending weighted degree within the community
    subgraph, ties broken by entity id (ascending) so the ordering is
    deterministic. ``edge_count`` / ``total_weight`` describe the induced
    subgraph.
    """
    member_ids = sorted(str(node) for node in nodes)
    sub = graph.subgraph(member_ids)
    weighted_degree = {str(node): float(deg) for node, deg in sub.degree(weight="weight")}
    ranked_ids = sorted(member_ids, key=lambda mid: (-weighted_degree.get(mid, 0.0), mid))
    members = tuple(
        CommunityMember(
            entity_id=mid,
            member_rank=rank,
            member_weight=weighted_degree.get(mid, 0.0),
        )
        for rank, mid in enumerate(ranked_ids)
    )
    total_weight = float(sum(float(weight) for _, _, weight in sub.edges(data="weight")))
    return DetectedCommunity(
        members_hash=compute_members_hash(member_ids),
        members=members,
        member_count=len(member_ids),
        edge_count=sub.number_of_edges(),
        total_weight=total_weight,
    )


def match_communities(
    detected: Sequence[DetectedCommunity],
    existing: Sequence[tuple[str, frozenset[str]]],
    *,
    threshold: float,
) -> tuple[list[str | None], list[str]]:
    """Greedy best-Jaccard stable-identity match (§17c Q3/Q7; pure, no DB).

    For each detected community (processed in the caller's deterministic order),
    selects the UNCLAIMED existing community with the highest Jaccard overlap of
    member sets (ties broken by ``community_key`` ascending). A best match at or
    above ``threshold`` AND with non-zero overlap REUSES that key (and claims it,
    so two new communities never collapse onto one old key); otherwise the
    community is new (``None`` → the caller mints a UUID). Requiring strictly
    positive overlap means a zero-overlap pairing is never reused even at
    ``threshold == 0`` — a disjoint set is a different community.

    Returns ``(assigned_keys, deleted_keys)``: ``assigned_keys`` is parallel to
    ``detected`` (reused key string or ``None`` to mint); ``deleted_keys`` are
    the existing keys never claimed (no longer present → delete + CASCADE members).
    """
    existing_list = list(existing)
    claimed: set[int] = set()
    assigned: list[str | None] = []
    for community in detected:
        target = community.member_ids
        best: tuple[float, str, int] | None = None
        for index, (key, members) in enumerate(existing_list):
            if index in claimed:
                continue
            score = jaccard(target, members)
            if best is None or score > best[0] or (score == best[0] and key < best[1]):
                best = (score, key, index)
        if best is not None and best[0] >= threshold and best[0] > 0.0:
            assigned.append(best[1])
            claimed.add(best[2])
        else:
            assigned.append(None)
    deleted = [
        key for index, (key, _members) in enumerate(existing_list) if index not in claimed
    ]
    return assigned, deleted


# --------------------------------------------------------------------------- #
# Persistence — tenant-scoped, atomic (mirrors aggregates.py / relational.py).
# --------------------------------------------------------------------------- #
def build_communities(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    tenant: str,
    force: bool = False,
) -> CommunityBuildResult:
    """Detect + persist the tenant's communities (the G3-b core).

    Reads the tenant's ``graph_relationships`` edge set, computes the
    ``source_graph_hash`` dirty fingerprint, and — unless ``force`` — SKIPS when
    the stored ``(build_version, source_graph_hash)`` already matches (§17c Q3).
    Otherwise runs Louvain (seeded + sorted for determinism), Jaccard-matches the
    partition against the existing communities to preserve ``community_key``s
    (and their summaries), and atomically replaces the tenant's
    ``graph_communities`` + ``graph_community_members`` rows inside a single
    ``conn.transaction()`` (the connection may be autocommit; the explicit
    transaction brackets the read-modify-write as a unit, mirroring
    :func:`brain.graph_rag.aggregates.refresh_aggregates`).

    Community knobs are threaded from ``cfg``:
    ``graph_community_resolution`` / ``_seed`` / ``_min_size`` / ``_jaccard`` /
    ``_max``. ``tenant`` must be non-empty (the caller resolves it via
    :func:`brain.graph_rag.tenancy.resolve_tenant`); an empty tenant is a caller
    bug and raises :class:`brain.errors.GraphTenantError` before any DB work.
    """
    if not tenant:
        raise GraphTenantError(
            "build_communities requires a non-empty tenant_id "
            "(resolve via brain.graph_rag.tenancy.resolve_tenant first)"
        )

    with conn.transaction():
        edges = _read_edges(conn, tenant)
        source_graph_hash = compute_source_graph_hash(edges)
        matches = _fingerprint_matches(conn, tenant, source_graph_hash)
        dirty = not matches

        if matches and not force:
            # Dirty gate fired: graph unchanged and not forced → no-op.
            return CommunityBuildResult(
                tenant_id=tenant,
                source_graph_hash=source_graph_hash,
                communities_total=_stored_count(conn, tenant),
                dirty=False,
                skipped=True,
            )

        detected = detect_communities(
            edges,
            resolution=cfg.graph_community_resolution,
            seed=cfg.graph_community_seed,
            min_size=cfg.graph_community_min_size,
            max_communities=cfg.graph_community_max,
        )
        existing = _read_existing_communities(conn, tenant)
        assigned, deleted_keys = match_communities(
            detected,
            [(key, members) for key, members, _hash in existing],
            threshold=cfg.graph_community_jaccard,
        )
        created, reused = _persist(
            conn,
            tenant,
            source_graph_hash=source_graph_hash,
            detected=detected,
            assigned=assigned,
            deleted_keys=deleted_keys,
        )
        return CommunityBuildResult(
            tenant_id=tenant,
            source_graph_hash=source_graph_hash,
            communities_total=len(detected),
            created=created,
            reused=reused,
            deleted=len(deleted_keys),
            dirty=dirty,
            skipped=False,
        )


def list_communities(
    conn: psycopg.Connection[Any],
    tenant: str,
    *,
    limit: int | None = None,
) -> list[CommunityRecord]:
    """Read the tenant's materialized communities (the admin-listing read; G3-f).

    Returns the stored :class:`~brain.graph_rag.schema.CommunityRecord` rows for
    ``tenant``, ordered by ``member_count`` DESC then ``community_key`` so the
    largest communities surface first (deterministic tie-break), capped by
    ``limit`` when given. Read-only — the raw ``summary_embedding`` vector is not
    selected (a storage handle, not a wire value). ``tenant`` must be non-empty
    (the caller resolves it via :func:`brain.graph_rag.tenancy.resolve_tenant`).
    """
    if not tenant:
        raise GraphTenantError(
            "list_communities requires a non-empty tenant_id "
            "(resolve via brain.graph_rag.tenancy.resolve_tenant first)"
        )
    base = (
        "SELECT community_key::text, source_graph_hash, members_hash, level, "
        "build_version, member_count, edge_count, total_weight, summary, "
        "summary_model, summary_at "
        "FROM graph_communities WHERE tenant_id = %s "
        "ORDER BY member_count DESC, community_key"
    )
    if limit is not None:
        rows = conn.execute(base + " LIMIT %s", (tenant, limit)).fetchall()
    else:
        rows = conn.execute(base, (tenant,)).fetchall()
    return [
        CommunityRecord(
            community_key=str(row[0]),
            source_graph_hash=str(row[1]),
            members_hash=str(row[2]),
            tenant_id=tenant,
            level=int(row[3]),
            build_version=str(row[4]),
            member_count=int(row[5]),
            edge_count=int(row[6]),
            total_weight=float(row[7]),
            summary=row[8],
            summary_model=row[9],
            summary_at=row[10],
        )
        for row in rows
    ]


def _read_edges(
    conn: psycopg.Connection[Any], tenant: str
) -> list[tuple[str, str, float]]:
    """Read the tenant's ``graph_relationships`` edge set, ordered for hashing."""
    rows = conn.execute(
        "SELECT src_id::text, dst_id::text, weight FROM graph_relationships "
        "WHERE tenant_id = %s ORDER BY src_id, dst_id",
        (tenant,),
    ).fetchall()
    return [(str(src), str(dst), float(weight)) for src, dst, weight in rows]


def _fingerprint_matches(
    conn: psycopg.Connection[Any], tenant: str, source_graph_hash: str
) -> bool:
    """True iff the stored communities all carry the current fingerprint.

    The dirty gate (§17c Q3). Reads the DISTINCT ``(build_version,
    source_graph_hash)`` for the tenant's communities; a match requires exactly
    one distinct pair equal to ``(BUILD_VERSION, source_graph_hash)``. Zero rows
    (no prior build / a zero-community tenant) is NOT a match — the build always
    re-evaluates, which is cheap.
    """
    rows = conn.execute(
        "SELECT DISTINCT build_version, source_graph_hash FROM graph_communities "
        "WHERE tenant_id = %s",
        (tenant,),
    ).fetchall()
    return rows == [(BUILD_VERSION, source_graph_hash)]


def _stored_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    """Count the tenant's materialized communities (for the skipped report)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM graph_communities WHERE tenant_id = %s",
        (tenant,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _read_existing_communities(
    conn: psycopg.Connection[Any], tenant: str
) -> list[tuple[str, frozenset[str], str]]:
    """Read existing communities as ``(community_key, member_ids, members_hash)``.

    Used both for Jaccard stable-identity matching (member sets) and to decide
    which keys are reused vs deleted. Two reads (communities + members) joined in
    Python keep the SQL trivial and tenant-scoped.
    """
    community_rows = conn.execute(
        "SELECT community_key::text, members_hash FROM graph_communities "
        "WHERE tenant_id = %s",
        (tenant,),
    ).fetchall()
    member_rows = conn.execute(
        "SELECT community_key::text, entity_id::text FROM graph_community_members "
        "WHERE tenant_id = %s",
        (tenant,),
    ).fetchall()
    members_by_key: dict[str, set[str]] = {}
    for key, entity_id in member_rows:
        members_by_key.setdefault(str(key), set()).add(str(entity_id))
    return [
        (str(key), frozenset(members_by_key.get(str(key), set())), str(members_hash))
        for key, members_hash in community_rows
    ]


def _persist(
    conn: psycopg.Connection[Any],
    tenant: str,
    *,
    source_graph_hash: str,
    detected: Sequence[DetectedCommunity],
    assigned: Sequence[str | None],
    deleted_keys: Sequence[str],
) -> tuple[int, int]:
    """Atomically replace the tenant's communities + members. Returns (created, reused).

    Order (within the caller's transaction):

    1. DELETE communities no longer present (CASCADE clears their members).
    2. For every REUSED community, delete its members and set a temporary, unique
       ``members_hash`` sentinel. This two-pass dance dodges a transient
       ``UNIQUE (tenant_id, level, members_hash)`` violation: a final hash being
       assigned to one row could otherwise momentarily collide with another
       reused row's not-yet-updated old hash.
    3. For every reused community, write the FINAL ``members_hash`` + stats +
       fingerprint. The summary columns are intentionally absent from the SET
       clause, so a reused row's summary/embedding is PRESERVED (delta-gate;
       §17c Q3/Q10) while a membership change is recorded via ``members_hash``.
    4. INSERT minted communities (NULL summary fields).
    5. INSERT members for every kept community (reused + minted).
    """
    if deleted_keys:
        conn.execute(
            "DELETE FROM graph_communities "
            "WHERE tenant_id = %s AND community_key::text = ANY(%s)",
            (tenant, list(deleted_keys)),
        )

    reused_keys = [key for key in assigned if key is not None]

    # Pass 2a: clear members + park reused rows on a unique sentinel hash.
    for reused_key in reused_keys:
        conn.execute(
            "DELETE FROM graph_community_members "
            "WHERE tenant_id = %s AND community_key = %s",
            (tenant, reused_key),
        )
        conn.execute(
            "UPDATE graph_communities "
            "SET members_hash = %s WHERE tenant_id = %s AND community_key = %s",
            (f"pending:{reused_key}", tenant, reused_key),
        )

    created = 0
    reused = 0
    # ``members_to_insert`` collects (community_key, member) once the key is known.
    members_to_insert: list[tuple[str, CommunityMember]] = []
    for community, key in zip(detected, assigned, strict=True):
        if key is None:
            community_key = str(uuid.uuid4())
            _insert_community(
                conn,
                tenant,
                community_key=community_key,
                source_graph_hash=source_graph_hash,
                community=community,
            )
            created += 1
        else:
            community_key = key
            _update_reused_community(
                conn,
                tenant,
                community_key=community_key,
                source_graph_hash=source_graph_hash,
                community=community,
            )
            reused += 1
        members_to_insert.extend((community_key, member) for member in community.members)

    for community_key, member in members_to_insert:
        conn.execute(
            "INSERT INTO graph_community_members "
            "(tenant_id, community_key, entity_id, member_rank, member_weight) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                tenant,
                community_key,
                member.entity_id,
                member.member_rank,
                member.member_weight,
            ),
        )

    return created, reused


def _insert_community(
    conn: psycopg.Connection[Any],
    tenant: str,
    *,
    community_key: str,
    source_graph_hash: str,
    community: DetectedCommunity,
) -> None:
    """INSERT a freshly-minted community row (NULL summary fields)."""
    conn.execute(
        "INSERT INTO graph_communities "
        "(tenant_id, community_key, level, build_version, source_graph_hash, "
        " members_hash, member_count, edge_count, total_weight) "
        "VALUES (%s, %s, 0, %s, %s, %s, %s, %s, %s)",
        (
            tenant,
            community_key,
            BUILD_VERSION,
            source_graph_hash,
            community.members_hash,
            community.member_count,
            community.edge_count,
            community.total_weight,
        ),
    )


def _update_reused_community(
    conn: psycopg.Connection[Any],
    tenant: str,
    *,
    community_key: str,
    source_graph_hash: str,
    community: DetectedCommunity,
) -> None:
    """UPDATE a reused community in place, PRESERVING its summary columns.

    The summary/embedding columns are deliberately omitted from the SET clause
    (the delta-gate; §17c Q3/Q10): a still-valid summary survives the rebuild and
    a membership change is recorded only via ``members_hash`` for G3-c to act on.
    """
    conn.execute(
        "UPDATE graph_communities SET "
        "build_version = %s, source_graph_hash = %s, members_hash = %s, "
        "member_count = %s, edge_count = %s, total_weight = %s, updated_at = NOW() "
        "WHERE tenant_id = %s AND community_key = %s",
        (
            BUILD_VERSION,
            source_graph_hash,
            community.members_hash,
            community.member_count,
            community.edge_count,
            community.total_weight,
            tenant,
            community_key,
        ),
    )
