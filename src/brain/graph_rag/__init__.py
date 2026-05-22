"""GraphRAG: entity-centric graph retrieval alongside the vector/hybrid RAG.

The public API grows per wave. Value objects live in
:mod:`brain.graph_rag.schema`; the storage/traversal backends (``GraphBackend``
Protocol + the default Apache ``AgeBackend``) live in
:mod:`brain.graph_rag.backends`; the incremental reconcile (person aspect, wave
G1-b) lives in :mod:`brain.graph_rag.reconcile`. Concept extraction +
``graph_rag_search`` arrive in G2.
"""
from .backends import AgeBackend, GraphBackend, PersonScope, TraversalHit
from .build import BuildResult, build_graph
from .communities import (
    BUILD_VERSION,
    CommunityBuildResult,
    DetectedCommunity,
    build_communities,
    compute_members_hash,
    compute_source_graph_hash,
    detect_communities,
    list_communities,
    match_communities,
)
from .communities_summary import (
    CommunitySummaryResult,
    summarize_communities,
)
from .extract import (
    EntityExtractor,
    ExtractedEntity,
    OllamaExtractor,
    make_extractor,
)
from .grouping import group_themes
from .reconcile import (
    CONCEPTS_ASPECT,
    PersonResolver,
    ReconcileConfig,
    ReconcileResult,
    RefreshResult,
    ResolvedPerson,
    default_person_resolver,
    reconcile_document,
    refresh_aggregates,
    remove_document,
)
from .retrieve import (
    AUTO_MODE,
    FUSE_MODE,
    GLOBAL_MODE,
    LOCAL_MODE,
    THEMES_MODE,
    graph_rag_search,
)

__all__ = [
    "AUTO_MODE",
    "BUILD_VERSION",
    "CONCEPTS_ASPECT",
    "FUSE_MODE",
    "GLOBAL_MODE",
    "LOCAL_MODE",
    "THEMES_MODE",
    "AgeBackend",
    "BuildResult",
    "CommunityBuildResult",
    "CommunitySummaryResult",
    "DetectedCommunity",
    "EntityExtractor",
    "ExtractedEntity",
    "GraphBackend",
    "OllamaExtractor",
    "PersonResolver",
    "PersonScope",
    "ReconcileConfig",
    "ReconcileResult",
    "RefreshResult",
    "ResolvedPerson",
    "TraversalHit",
    "build_communities",
    "build_graph",
    "compute_members_hash",
    "compute_source_graph_hash",
    "default_person_resolver",
    "detect_communities",
    "graph_rag_search",
    "group_themes",
    "list_communities",
    "make_extractor",
    "match_communities",
    "reconcile_document",
    "refresh_aggregates",
    "remove_document",
    "summarize_communities",
]
