"""Frozen value objects for the GraphRAG layer (no DB logic).

Two families live here:

* **Row mirrors** — :class:`GraphEntity`, :class:`EntityMention`,
  :class:`EdgeContribution`, :class:`Edge` map 1:1 onto the migration-012 tables
  (``graph_entities``, ``graph_entity_mentions``, ``graph_edge_contributions``,
  ``graph_relationships``). The raw ``embedding`` vector is deliberately *not*
  carried on :class:`GraphEntity` — like :class:`brain.queries.DocumentRow` and
  :class:`brain.search.SearchResult`, these are read-side value objects, not
  storage handles.
* **Retrieval value objects** — :class:`ThemeGroup`, :class:`GraphContext`,
  :class:`GraphExplanation` are the wire shape returned by graph retrieval
  (spec §6/§9, §4 D8). They are populated by the G2 retrieval code; the field
  set here is the v1 contract and later waves may extend it additively.

All tenantized rows / queries carry ``tenant_id`` (spec §4 D9). It defaults to
``"default"`` — the fixed tenant used by single-user local deployments — so the
local construction path is unchanged. ``ThemeGroup`` is the one exception: it is
a derived grouping over an already tenant-scoped subgraph, not a row mirror, so
it carries no ``tenant_id`` of its own.

All classes are ``frozen=True`` dataclasses. ``SearchResult`` is referenced only
under ``TYPE_CHECKING`` (with ``from __future__ import annotations``) so this
module never imports :mod:`brain.search` at runtime — keeping it free of any
import cycle with the ingest pipeline that later wires graph reconciliation in.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..search import SearchResult


@dataclass(frozen=True)
class GraphEntity:
    """An entity node — mirrors a ``graph_entities`` row.

    ``entity_type`` is one of ``person``/``org``/``project``/``topic``/``tool``
    (DB ``CHECK``-enforced). ``canonical_key`` is the dedup key (a resolved
    person-key for people, ``lower(name)`` for concepts) and is unique per
    ``(tenant_id, entity_type, canonical_key)``. ``tenant_id`` scopes the row to
    one tenant (spec §4 D9); single-user local deployments use the fixed default
    tenant ``"default"``. ``doc_count`` is *derived* from mentions and refreshed
    by the aggregate rebuild — never authoritative on write.
    """

    id: str
    entity_type: str
    name: str
    canonical_key: str
    tenant_id: str = "default"
    description: str | None = None
    doc_count: int = 0
    properties: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class EntityMention:
    """A per-document entity mention — mirrors a ``graph_entity_mentions`` row.

    Source-of-truth row: re-ingest deletes and reinserts a document's mentions.
    ``source`` records provenance — ``"people"`` for the people pipeline or
    ``"extractor:<model>@<ver>"`` for the concept extractor. ``tenant_id`` scopes
    the row to one tenant (part of the row's primary key; spec §4 D9).
    """

    entity_id: str
    document_id: str
    source: str
    tenant_id: str = "default"
    mention_count: int = 1


@dataclass(frozen=True)
class EdgeContribution:
    """A per-document raw co-occurrence — mirrors ``graph_edge_contributions``.

    Source-of-truth row holding the *raw* window co-occurrence count between two
    entities within one document. Endpoints are canonicalized ``src_id < dst_id``
    (DB ``CHECK``-enforced). No generic suppression or weighting is applied here;
    those are derive/query-time concerns. ``tenant_id`` scopes the row to one
    tenant (part of the row's primary key; spec §4 D9).
    """

    document_id: str
    src_id: str
    dst_id: str
    tenant_id: str = "default"
    cooccur_count: int = 1


@dataclass(frozen=True)
class Edge:
    """A derived aggregate relationship — mirrors a ``graph_relationships`` row.

    ``weight`` is the normative association metric: normalized lift in ``(0, 1]``
    (recomputed from contributions; doubles as the BFS path affinity). ``co_count``
    is ``SUM`` of contribution counts and ``doc_count`` the distinct-document
    count. Endpoints are canonicalized ``src_id < dst_id`` (DB ``CHECK``-enforced).
    ``tenant_id`` scopes the row to one tenant (part of the row's primary key;
    spec §4 D9).
    """

    src_id: str
    dst_id: str
    weight: float
    tenant_id: str = "default"
    rel_type: str = "co_occurs"
    co_count: int = 0
    doc_count: int = 0
    updated_at: datetime | None = None


@dataclass(frozen=True)
class CommunityMember:
    """A community ↔ entity membership — mirrors ``graph_community_members``.

    Wave G3 (spec §17c Q1). One row per entity in a detected community.
    ``member_rank`` orders entities within the community (0-based, most-central
    first) and ``member_weight`` is the entity's weighted degree inside the
    community subgraph; both are DB ``CHECK``-enforced non-negative. At detection
    time the owning ``community_key`` is not yet assigned (a reused key is matched
    by Jaccard, a new one minted at persist), so it defaults to the empty string
    until the persistence layer fills it in. ``tenant_id`` scopes the row to one
    tenant (part of the row's primary key; spec §4 D9).
    """

    entity_id: str
    member_rank: int = 0
    member_weight: float = 0.0
    community_key: str = ""
    tenant_id: str = "default"


@dataclass(frozen=True)
class CommunityRecord:
    """A detected community — mirrors a ``graph_communities`` row (wave G3).

    Single-level only (``level`` pinned to 0; spec §17c Q1 / §15). ``community_key``
    is the durable, stable identity preserved across rebuilds by Jaccard matching
    (spec §17c Q3/Q7). ``source_graph_hash`` is the tenant-graph dirty fingerprint
    (an edge hash over ordered ``graph_relationships``); ``members_hash`` is the
    per-community identity hash over the sorted member entity ids. The aggregate
    stats (``member_count``/``edge_count``/``total_weight``) describe the
    community subgraph. The ``summary*`` fields are populated lazily/eagerly at
    build/refresh by G3-c (NULL here at detection); like :class:`GraphEntity` the
    raw ``summary_embedding`` vector is deliberately not carried (read-side value
    object, not a storage handle). ``tenant_id`` scopes the row to one tenant
    (part of the row's primary key; spec §4 D9).
    """

    community_key: str
    source_graph_hash: str
    members_hash: str
    tenant_id: str = "default"
    level: int = 0
    build_version: str = "networkx-louvain-v1"
    member_count: int = 0
    edge_count: int = 0
    total_weight: float = 0.0
    summary: str | None = None
    summary_model: str | None = None
    summary_at: datetime | None = None


@dataclass(frozen=True)
class ThemeGroup:
    """A cluster of related entities for the "themes with X" headline (spec §6b).

    Produced by the scoped-subgraph grouping over X's documents. ``entities`` are
    the group's key entities, ``doc_ids`` the representative X-documents, and
    ``score`` the group's total in-scope normalized lift (the ranking metric).
    ``summary`` is an optional on-demand ``summarize_group()`` Ollama synthesis
    (top-K groups only, when ``--synthesize``/MCP ``synthesize=true``).
    """

    group_id: int
    entities: list[GraphEntity] = field(default_factory=list)
    doc_ids: list[str] = field(default_factory=list)
    score: float = 0.0
    summary: str | None = None


@dataclass(frozen=True)
class CommunityGroup:
    """A detected community surfaced by global retrieval (spec §6c / §17c Q4-Q5).

    The ranked unit of the **global** path (wave G3-d): each group is one
    ``graph_communities`` community that surfaced from the community-level RRF
    (FTS over ``summary_tsv`` fused with vector cosine over ``summary_embedding``;
    :func:`brain.graph_rag.global_._retrieve_global`). Distinct from
    :class:`ThemeGroup` (a derived entity cluster over a person's scoped
    subgraph) — a ``CommunityGroup`` mirrors a persisted, pre-summarized
    community.

    ``community_key`` is the durable community identity, ``level`` the (single)
    detection level (pinned 0; spec §17c Q1), ``member_count`` the full community
    size, and ``score`` the fused RRF score (the ranking metric). ``summary`` is
    the eager community summary (NULL when Ollama was unavailable at build —
    the community then ranked on its FTS leg only). ``entities`` are the
    representative member entities (top by ``member_rank``) and ``doc_ids`` the
    representative documents that most mention the community's entities.
    """

    community_key: str
    level: int = 0
    member_count: int = 0
    score: float = 0.0
    summary: str | None = None
    entities: list[GraphEntity] = field(default_factory=list)
    doc_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GraphExplanation:
    """Per-query graph-retrieval diagnostic (spec §6a/§9; §4 D8).

    The graph analogue of :class:`brain.search.SearchExplanation`. Records the
    seeds, resolved scope, traversal parameters, and pruning telemetry so a
    caller can see *why* a :class:`GraphContext` looks the way it does. Populated
    by the G2 retrieval code; provisional fields default to safe empties so the
    object is constructible before that wave lands. ``tenant_id`` records the
    tenant the query was scoped to (spec §4 D9 — every graph query injects it).
    """

    mode: str
    tenant_id: str = "default"
    seed_entity_ids: list[str] = field(default_factory=list)
    person_keys: list[str] = field(default_factory=list)
    depth: int = 0
    frontier_cap: int = 0
    min_edge_weight: float = 0.0
    nodes_visited: int = 0
    edges_considered: int = 0
    generic_df_cap: int | None = None
    matched_filters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphContext:
    """The distinct envelope returned by graph retrieval (spec §4 D8, §6, §9).

    Themes/communities are *not* document hits, so graph retrieval returns this
    shape rather than faking :class:`brain.search.SearchResult` parity. ``mode``
    is the resolved retrieval mode (``local``/``themes``/``global``); ``person``
    is set for scoped "themes with X" queries. ``docs`` reuses ``SearchResult``
    for the document-hit portion. ``themes`` is populated for ``themes`` mode,
    ``communities`` for ``global`` mode (wave G3-d; spec §17c Q5), and
    ``entities`` for ``local`` mode. ``tenant_id`` records the tenant the query
    was scoped to (spec §4 D9 — every graph query injects it).

    Degradation signals (``requested_mode`` / ``degraded_from`` /
    ``degradation_reason``) are **KEPT DORMANT** (spec §17c Q6): they are now
    **always** ``None``. They date to the wave-G2 era, when an ``auto`` thematic
    query with no resolvable person degraded ``global`` → ``local`` and recorded
    the substitution here. The G3-e router flip made ``global`` a real, executing
    mode, so that degradation no longer happens and no code path populates these
    fields. They are retained on the dataclass purely for wire/JSON stability
    (the MCP + CLI ``--json`` shape keeps the keys) — not removed.
    """

    session_id: str
    mode: str
    query: str
    tenant_id: str = "default"
    person: str | None = None
    themes: list[ThemeGroup] = field(default_factory=list)
    communities: list[CommunityGroup] = field(default_factory=list)
    entities: list[GraphEntity] = field(default_factory=list)
    docs: list[SearchResult] = field(default_factory=list)
    explanation: GraphExplanation | None = None
    requested_mode: str | None = None
    degraded_from: str | None = None
    degradation_reason: str | None = None


@dataclass(frozen=True)
class EntitySummary:
    """Lightweight entity row for listing — projected from ``graph_entities``.

    Returned by :func:`brain.graph_rag.relational.list_entities` for the
    ``brain graphrag entities`` admin surface. Does not carry the raw
    ``embedding`` vector (a storage handle, not a wire value — same convention
    as :class:`GraphEntity` and :class:`CommunityRecord`).
    """

    entity_type: str
    name: str
    canonical_key: str
    doc_count: int
    description: str | None = None


@dataclass(frozen=True)
class GraphStats:
    """At-a-glance graph overview for ``brain graphrag stats``.

    Produced by :func:`brain.graph_rag.relational.graph_stats` from the
    tenant's relational tables. ``counts_by_type`` maps each ``entity_type``
    present in ``graph_entities`` to its row count; ``total_entities`` is their
    sum. ``top_entities`` are the top-10 entities by ``doc_count`` (the same
    slice ``brain graphrag entities --limit 10`` would return with sort=docs).
    """

    counts_by_type: Mapping[str, int]
    total_entities: int
    total_relationships: int
    total_communities: int
    top_entities: tuple[EntitySummary, ...]
