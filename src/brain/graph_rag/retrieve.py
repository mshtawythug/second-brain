"""Entity-centric (local) graph retrieval + the auto-router dispatch (``graph_rag_search``).

This module owns the **public dispatch entry** :func:`graph_rag_search`, the
**local** retrieval path, and seed resolution. The **themes-with-X** path (spec
§6b) lives in the sibling :mod:`brain.graph_rag.themes`, and the entity-row +
doc-result helpers shared by both paths live in
:mod:`brain.graph_rag._retrieval_common` — the G2 wave-boundary file-size split
(mirroring the G1 :mod:`brain.graph_rag.aggregates` / G2-c
:mod:`brain.graph_rag.relational` extractions). Behavior is unchanged by the
split; ``graph_rag_search`` and the mode constants keep their import paths.

Two retrieval paths share ``GraphContext`` assembly + snippet reuse:

* **Local** (spec §6a; wave G2-d): resolve a free-text query to one or more seed
  ``Entity`` vertices in the relational catalog, run a bounded ``CO_OCCURS``
  traversal from each seed via the injected
  :class:`~brain.graph_rag.backends.base.GraphBackend`, map the reached entities
  back to documents through the tenant-scoped ``graph_entity_mentions`` mirror,
  rank those documents deterministically, and attach a best snippet per document
  by reusing the existing FTS snippet path (:mod:`brain.search`).
* **Themes with X** (spec §6b; wave G2-f — the HEADLINE): implemented in
  :mod:`brain.graph_rag.themes` and dispatched here. It scopes to a person ``X``
  via :meth:`~brain.graph_rag.backends.base.GraphBackend.scope_person`, computes
  the **in-scope normalized lift**, suppresses generic entities, excludes the
  seed X + the owner, groups the scoped subgraph
  (:func:`brain.graph_rag.grouping.group_themes`), and returns ranked
  :class:`~brain.graph_rag.schema.ThemeGroup`s.

Scope (spec §12, waves G2-d/G2-f/G2-g/G3-e/G4-c): the LOCAL path + the
**auto-router** (wave G2-g; the pure :func:`brain.graph_rag.router.route`) live
here; ``route`` dispatches ``mode='auto'`` to local, themes, or — after the
**G3-e flip** (spec §17c Q6) — **global**: a thematic query with no resolvable
person now routes to the real community path
(:func:`brain.graph_rag.global_._retrieve_global`), not the former G2
``global→local`` degradation. An **explicit** ``mode='global'`` dispatches to
that same global path. An **explicit** ``mode='fuse'`` (wave G4-c; spec §17d Q1)
dispatches to :func:`brain.graph_rag.fuse._retrieve_fuse`, which RRF-merges the
local doc leg with the vector/FTS hybrid leg; ``fuse`` is explicit-only — the
auto router never targets it. The G2 degradation signals (``requested_mode`` /
``degraded_from`` / ``degradation_reason`` on the :class:`GraphContext`) are
KEEP-DORMANT — defined but never populated.

Design invariants (mirroring the rest of the GraphRAG layer):

* **Tenant-scoped (spec §4 D9).** ``tenant_id`` is resolved once via
  :func:`brain.graph_rag.tenancy.resolve_tenant` and injected into every query:
  seed resolution, the backend traversal, and the mention→document mapping all
  filter by it, so a second tenant's entities/docs can never leak into a result.
  (``documents`` is not tenantized in G0, but the doc id set is derived solely
  from tenant-scoped ``graph_entity_mentions`` rows, so it is inherently scoped.)
* **Caps injected (spec §6/§10).** ``depth`` / ``frontier_cap`` /
  ``min_edge_weight`` default from :class:`~brain.config.Config`
  (``graph_depth`` / ``graph_frontier_cap`` / ``graph_min_edge_weight``) and are
  overridable per call.
* **Deterministic ordering.** Seed resolution, reached-entity merge, document
  ranking, and snippet selection all break ties on a stable key so repeated runs
  return byte-identical results.
* **Never-raise on empty.** An unresolvable query (no matching seed entity)
  returns an empty-but-valid ``GraphContext`` rather than raising. A backend
  cap-exceed (:class:`~brain.errors.GraphBackendError`) is *not* swallowed — the
  backend's complete-or-loud-failure contract is respected (spec §6a).
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import psycopg

from ._retrieval_common import _build_doc_results, _fetch_entities, _row_to_entity
from .fuse import _retrieve_fuse
from .global_ import _retrieve_global
from .router import (
    AUTO_MODE,
    FUSE_MODE,
    GLOBAL_MODE,
    LOCAL_MODE,
    THEMES_MODE,
    KnownPerson,
    route,
)
from .schema import GraphContext, GraphEntity, GraphExplanation
from .tenancy import resolve_tenant
from .themes import _GroupSummarizer, _retrieve_themes

if TYPE_CHECKING:
    from ..config import Config
    from ..ingest import Embedder
    from .backends.base import GraphBackend

# ``LOCAL_MODE`` / ``THEMES_MODE`` (and ``AUTO_MODE`` / ``GLOBAL_MODE``) are
# defined in :mod:`brain.graph_rag.router` (the mode vocabulary's canonical home)
# and re-exported here so existing ``from brain.graph_rag import LOCAL_MODE``
# imports keep working.
__all__ = [
    "AUTO_MODE",
    "FUSE_MODE",
    "GLOBAL_MODE",
    "LOCAL_MODE",
    "THEMES_MODE",
    "graph_rag_search",
]

# Upper bound on seed entities resolved from one query. Exact matches are
# naturally few; a substring query (e.g. a short prefix) can match many, so the
# winning tier is capped — by rank — to keep the per-seed traversal fan-out
# bounded. Deterministic: the cap slices an already-ordered candidate list.
_MAX_SEED_ENTITIES = 10

# Default number of ranked documents returned when the caller passes no ``limit``.
_DEFAULT_DOC_LIMIT = 10

# Document-ranking weight assigned to a *seed* entity's mentions. Seeds are the
# query's direct subject, so a document mentioning a seed always outranks one
# reached only through a co-occurrence neighbour (whose weight is its path
# affinity ∈ (0, 1]).
_SEED_WEIGHT = 1.0


def graph_rag_search(
    conn: psycopg.Connection[Any],
    cfg: Config,
    query: str,
    *,
    backend: GraphBackend,
    tenant: str | None = None,
    depth: int | None = None,
    frontier_cap: int | None = None,
    min_edge_weight: float | None = None,
    limit: int | None = None,
    mode: str = LOCAL_MODE,
    session_id: str | None = None,
    person: str | None = None,
    theme_limit: int | None = None,
    synthesize: bool = False,
    enricher: _GroupSummarizer | None = None,
    embedder: Embedder | None = None,
    exclude_confidential: bool = False,
) -> GraphContext:
    """Graph retrieval (spec §6; ``local`` G2-d, ``themes`` G2-f, router G2-g).

    ``mode='auto'`` (the router default for the CLI/MCP surfaces) runs the pure
    heuristic :func:`brain.graph_rag.router.route`: a **thematic** query (closed
    regex grammar) with a resolvable person (explicit ``person`` first, else a
    token-boundary scan of the tenant's known person entities) dispatches to
    ``themes``; a thematic query with **no** resolvable person dispatches to the
    real ``global`` community path (the **G3-e flip**, spec §17c Q6 — no longer
    the G2 ``global→local`` degradation, so no degradation signals are stamped);
    a non-thematic query dispatches to ``local``.

    ``mode='global'`` (explicit) and the auto thematic-no-person branch both
    dispatch to :func:`brain.graph_rag.global_._retrieve_global` (wave G3-d/G3-e;
    spec §6c) via one shared call site: it ranks the tenant's pre-detected
    **communities** (``graph_communities``) via RRF over an FTS leg
    (``summary_tsv``) fused with a vector leg (``summary_embedding``), and returns
    a :class:`GraphContext` with ranked ``communities`` (each a
    :class:`CommunityGroup`) + representative ``docs``. The community count
    defaults from ``cfg.graph_community_limit``. The vector leg needs an embedder:
    the caller passes a PRE-WARMED ``embedder`` instance (perf-T4 G5 — the only
    path that uses it; local/themes stay embedder-free) used to embed the query;
    a missing/failed embed logs a WARN and runs FTS-only (never-raise; spec §17c
    Q9).

    ``mode='fuse'`` (explicit only — never an auto target; wave G4-c, spec §17d
    Q1) dispatches to :func:`brain.graph_rag.fuse._retrieve_fuse`: it runs the
    ``local`` doc leg + the vector/FTS :func:`brain.search.hybrid_search` leg and
    RRF-merges their **document-id rankings** (``k=60``, the same constant as the
    other rankers), returning a :class:`GraphContext` (``mode='fuse'``) whose
    ``docs`` are the fused :class:`~brain.search.SearchResult`s (wire-stable) with
    per-doc leg provenance in ``explanation.matched_filters['fuse_doc_provenance']``.
    The pre-warmed ``embedder`` instance feeds the hybrid leg's vector arm; a
    missing/failed embedder degrades it to FTS-only and a fully-dead hybrid leg
    degrades fuse to graph-only (never-raise). Hybrid search's ranking is
    consumed unchanged (spec §4 D7).

    ``mode='local'`` resolves ``query`` to seed entities, traverses
    ``CO_OCCURS`` from each seed via ``backend``, maps the reached entities to
    documents, and returns a :class:`GraphContext` with the seed + reached
    ``entities`` and ranked ``docs`` (each a :class:`brain.search.SearchResult`
    with a best snippet).

    ``mode='themes'`` is the scope-first "themes in my conversations with X"
    headline (spec §6b; implemented in :mod:`brain.graph_rag.themes`): it
    **requires** ``person`` (the ``X`` to scope to), scopes via
    :meth:`GraphBackend.scope_person`, computes the in-scope normalized lift,
    suppresses generic entities, **excludes X and the owner**, groups the scoped
    subgraph, and returns ranked ``themes`` (each a :class:`ThemeGroup` with
    representative X-docs + snippets). ``theme_limit`` defaults from
    ``cfg.graph_theme_limit``. ``synthesize=True`` attaches a best-effort
    per-group Ollama summary via the injected ``enricher`` (default OFF; never
    required for retrieval — a missing/failed Ollama yields ``summary=None`` + a
    WARN, spec §17b decision 7).

    Caps (``depth`` / ``frontier_cap`` / ``min_edge_weight``) default from
    ``cfg`` and are overridable; ``limit`` caps the returned documents
    (default :data:`_DEFAULT_DOC_LIMIT`). ``tenant`` overrides the configured
    tenant (resolved via :func:`brain.graph_rag.tenancy.resolve_tenant`).
    ``session_id`` is generated when omitted (a fresh ``uuid4`` hex), mirroring
    the MCP ``{session_id, results}`` envelope.

    An unresolvable query / empty scope returns an empty-but-valid
    ``GraphContext`` (never raises). A traversal/scope that exceeds the backend's
    safe bound surfaces as :class:`~brain.errors.GraphBackendError`
    (complete-or-loud-failure, not silent truncation).

    Raises:
        ValueError: ``mode='themes'`` without a resolvable ``person``, or an
            unrecognized ``mode`` (caller bug, surfaced by the router).
        PersonNotFound / PersonAmbiguous: ``person`` does not resolve to exactly
            one directory person (themes mode; surfaced for the CLI/MCP to map).
        GraphTenantError: the effective tenant id resolves empty (caller bug).
    """
    tenant_id = resolve_tenant(cfg, tenant)
    resolved_depth = cfg.graph_depth if depth is None else depth
    resolved_frontier = cfg.graph_frontier_cap if frontier_cap is None else frontier_cap
    resolved_min_weight = (
        cfg.graph_min_edge_weight if min_edge_weight is None else min_edge_weight
    )
    resolved_limit = _DEFAULT_DOC_LIMIT if limit is None else limit
    resolved_session = uuid.uuid4().hex if session_id is None else session_id

    # Route. ``known_persons`` (the token-boundary-scan candidates) are only
    # needed for the auto path; explicit modes ignore them, so skip the DB read.
    # The router honors explicit modes (incl. ``global`` after the G3-e flip) and
    # decides the auto branches purely; the DB-derived person list is injected so
    # the router itself stays DB-free (dependency inversion).
    known_persons = (
        _fetch_known_persons(conn, tenant_id) if mode == AUTO_MODE else []
    )
    decision = route(query, mode=mode, person=person, known_persons=known_persons)

    # Global (community-based) path (spec §6c; waves G3-d/G3-e). An EXPLICIT
    # ``mode='global'`` AND the auto thematic-no-person branch both resolve to
    # GLOBAL_MODE and share this single dispatch to :func:`_retrieve_global` —
    # the only path that uses ``embedder`` (spec §17c Q9; local/themes stay
    # embedder-free). The community limit defaults from
    # ``cfg.graph_community_limit`` (resolved inside ``_retrieve_global``), not
    # the doc ``limit``. The G3-e flip routes BOTH global cases here; the G2
    # ``global→local`` degradation is gone (spec §17c Q6).
    if decision.executed_mode == GLOBAL_MODE:
        return _retrieve_global(
            conn,
            cfg,
            query,
            tenant=tenant_id,
            limit=limit,
            embedder=embedder,
            session_id=resolved_session,
            exclude_confidential=exclude_confidential,
        )

    # Fuse (graph ⊕ hybrid doc-leg RRF) path (spec §17d Q1; wave G4-c). Honored
    # ONLY for an EXPLICIT ``mode='fuse'`` — the auto router never targets it
    # (auto stays local/themes/global). Runs the local doc leg + the hybrid leg
    # and RRF-merges their doc-id rankings; the pre-warmed ``embedder`` feeds the
    # hybrid leg's vector arm (a missing/failed embedder degrades it to FTS-only
    # — never-raise). Local/themes/global/auto are unchanged.
    if decision.executed_mode == FUSE_MODE:
        return _retrieve_fuse(
            conn,
            cfg,
            query,
            backend=backend,
            tenant=tenant_id,
            depth=resolved_depth,
            frontier_cap=resolved_frontier,
            min_edge_weight=resolved_min_weight,
            limit=resolved_limit,
            embedder=embedder,
            session_id=resolved_session,
            exclude_confidential=exclude_confidential,
        )

    if decision.executed_mode == THEMES_MODE:
        resolved_person = (
            decision.resolved_person.display_name
            if decision.resolved_person is not None
            else None
        )
        if resolved_person is None or not resolved_person.strip():
            raise ValueError(
                "mode='themes' requires a non-empty 'person' (the X to scope to)"
            )
        resolved_theme_limit = (
            cfg.graph_theme_limit if theme_limit is None else theme_limit
        )
        return _retrieve_themes(
            conn,
            person=resolved_person,
            query=query,
            backend=backend,
            tenant_id=tenant_id,
            frontier_cap=resolved_frontier,
            min_edge_weight=resolved_min_weight,
            generic_df_ratio=cfg.graph_generic_df_ratio,
            owner_keys=cfg.owner_participants,
            theme_limit=resolved_theme_limit,
            limit=resolved_limit,
            session_id=resolved_session,
            synthesize=synthesize,
            enricher=enricher,
            exclude_confidential=exclude_confidential,
        )

    # Local path (explicit ``local`` OR the auto non-thematic branch). After the
    # G3-e flip the thematic-no-person case routes to ``global`` above, so no
    # path reaching here ever degrades — the G2 degradation signals stay dormant
    # (``None``) on the returned context (spec §17c Q6).
    return _retrieve_local(
        conn,
        query,
        backend=backend,
        tenant_id=tenant_id,
        depth=resolved_depth,
        frontier_cap=resolved_frontier,
        min_edge_weight=resolved_min_weight,
        limit=resolved_limit,
        session_id=resolved_session,
        exclude_confidential=exclude_confidential,
    )


# --------------------------------------------------------------------------- #
# Local-path orchestration (the single seam G2-f/G2-g compose over)
# --------------------------------------------------------------------------- #
def _retrieve_local(
    conn: psycopg.Connection[Any],
    query: str,
    *,
    backend: GraphBackend,
    tenant_id: str,
    depth: int,
    frontier_cap: int,
    min_edge_weight: float,
    limit: int,
    session_id: str,
    exclude_confidential: bool = False,
) -> GraphContext:
    """Run the resolved local retrieval and assemble its ``GraphContext``.

    Single code path for the populated and the empty (no-seed) cases: when no
    seed resolves, the traversal loop and every downstream query are skipped on
    empty inputs, yielding an empty-but-valid context (never-raise).
    """
    seeds = _resolve_seeds(conn, tenant_id, query)
    seed_ids = {seed.id for seed in seeds}

    # Traverse from each seed; keep the best (highest-affinity) path per reached
    # entity across all seeds. The seed set itself is excluded (a seed reached
    # from another seed stays a seed, never a neighbour).
    reached: dict[str, float] = {}
    for seed in seeds:
        hits = backend.traverse(
            conn,
            tenant_id,
            seed.id,
            depth=depth,
            frontier_cap=frontier_cap,
            min_edge_weight=min_edge_weight,
        )
        for hit in hits:
            if hit.entity_uuid in seed_ids:
                continue
            prior = reached.get(hit.entity_uuid)
            if prior is None or hit.affinity > prior:
                reached[hit.entity_uuid] = hit.affinity

    reached_entities = _fetch_entities(conn, tenant_id, list(reached))
    ordered_reached = sorted(
        reached_entities,
        key=lambda e: (-reached[e.id], e.canonical_key, e.id),
    )
    entities = [*seeds, *ordered_reached]

    # Document scoring: a doc accumulates the weight of every seed (1.0) /
    # reached (path affinity) entity it mentions, so docs co-mentioning multiple
    # relevant entities rank highest. Ties break on document_id for determinism.
    weights: dict[str, float] = {seed.id: _SEED_WEIGHT for seed in seeds}
    for entity_uuid, affinity in reached.items():
        weights.setdefault(entity_uuid, affinity)
    ranked = _rank_documents(conn, tenant_id, weights, limit)
    docs = _build_doc_results(
        conn, query, ranked, exclude_confidential=exclude_confidential
    )

    explanation = GraphExplanation(
        mode=LOCAL_MODE,
        tenant_id=tenant_id,
        seed_entity_ids=[seed.id for seed in seeds],
        person_keys=[],
        depth=depth,
        frontier_cap=frontier_cap,
        min_edge_weight=min_edge_weight,
        nodes_visited=len(seeds) + len(reached),
        # The AGE backend's TraversalHit surfaces reached entities + path affinity
        # but not a per-edge visit count, so edges_considered stays 0 in G2-d.
        edges_considered=0,
        generic_df_cap=None,
        matched_filters={
            "seed_query": query,
            "seed_count": len(seeds),
            "reached_count": len(reached),
            "depth": depth,
            "frontier_cap": frontier_cap,
            "min_edge_weight": min_edge_weight,
            "limit": limit,
        },
    )
    return GraphContext(
        session_id=session_id,
        mode=LOCAL_MODE,
        query=query,
        tenant_id=tenant_id,
        person=None,
        themes=[],
        entities=entities,
        docs=docs,
        explanation=explanation,
    )


# --------------------------------------------------------------------------- #
# Seed resolution (relational catalog; tenant-scoped; deterministic)
# --------------------------------------------------------------------------- #
def _resolve_seeds(
    conn: psycopg.Connection[Any], tenant_id: str, query: str
) -> list[GraphEntity]:
    """Resolve a query to seed ``graph_entities`` rows (spec §6a seed step).

    Tenant-scoped. Exact ``canonical_key``/``name`` matches (case-insensitive)
    win; only when none exist does it fall back to a case-insensitive substring
    match. Either tier is ordered by ``doc_count`` DESC then ``canonical_key`` /
    ``entity_type`` ASC and capped at :data:`_MAX_SEED_ENTITIES`, so the seed set
    is deterministic. An empty / whitespace-only query resolves to no seeds.

    Vector pre-match (spec §6a "or vector pre-match") is intentionally NOT used
    in G2-d — exact+substring covers entity-name queries and keeps the path
    free of an embedder dependency (YAGNI; can be added later behind the same
    function).
    """
    needle = query.strip().lower()
    if not needle:
        return []

    columns = "id::text, entity_type, name, canonical_key, description, doc_count"
    exact_rows = conn.execute(
        f"SELECT {columns} FROM graph_entities "
        "WHERE tenant_id = %s AND (lower(canonical_key) = %s OR lower(name) = %s) "
        "ORDER BY doc_count DESC, canonical_key ASC, entity_type ASC",
        (tenant_id, needle, needle),
    ).fetchall()
    rows = exact_rows
    if not rows:
        pattern = _like_contains(needle)
        rows = conn.execute(
            f"SELECT {columns} FROM graph_entities "
            "WHERE tenant_id = %s "
            "AND (lower(name) LIKE %s OR lower(canonical_key) LIKE %s) "
            "ORDER BY doc_count DESC, canonical_key ASC, entity_type ASC",
            (tenant_id, pattern, pattern),
        ).fetchall()
    return [_row_to_entity(row, tenant_id) for row in rows[:_MAX_SEED_ENTITIES]]


def _like_contains(needle: str) -> str:
    """Build a case-insensitive ``LIKE`` contains-pattern, escaping wildcards.

    Postgres ``LIKE`` defaults to backslash as the escape character, so a
    literal ``%`` / ``_`` / ``\\`` in the (already lower-cased) needle is
    backslash-escaped before being wrapped in ``%...%``.
    """
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _fetch_known_persons(
    conn: psycopg.Connection[Any], tenant_id: str
) -> list[KnownPerson]:
    """Fetch tenant-scoped known person entities for the router's name scan.

    Returns every ``person`` ``graph_entities`` row's
    ``(canonical_key, display_name, doc_count)`` so the pure
    :func:`brain.graph_rag.router.route` can token-boundary-scan the query
    without any DB access (dependency inversion — the DB-derived candidate list
    is injected). Ordered by ``canonical_key`` for a reproducible input; the
    router's tie-break is order-independent, but a stable order keeps the call
    deterministic regardless of physical row order.
    """
    rows = conn.execute(
        "SELECT canonical_key, name, doc_count FROM graph_entities "
        "WHERE tenant_id = %s AND entity_type = 'person' "
        "ORDER BY canonical_key",
        (tenant_id,),
    ).fetchall()
    return [
        KnownPerson(
            canonical_key=str(row[0]),
            display_name=str(row[1]),
            doc_count=int(row[2]),
        )
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# Reached entities → ranked documents (tenant-scoped mention mirror)
# --------------------------------------------------------------------------- #
def _rank_documents(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    weights: dict[str, float],
    limit: int,
) -> list[tuple[str, float]]:
    """Map weighted entities to documents and rank them (spec §6a doc step).

    Reads the tenant-scoped ``graph_entity_mentions`` mirror for the seed +
    reached entity ids and sums each document's entity weights. Returns the top
    ``limit`` ``(document_id, score)`` pairs ordered by score DESC, document_id
    ASC (deterministic). An empty entity/weight set returns no documents.
    """
    if not weights or limit <= 0:
        return []
    rows = conn.execute(
        "SELECT entity_id::text, document_id::text FROM graph_entity_mentions "
        "WHERE tenant_id = %s AND entity_id = ANY(%s)",
        (tenant_id, list(weights)),
    ).fetchall()
    doc_scores: dict[str, float] = {}
    for entity_id, document_id in rows:
        doc_scores[str(document_id)] = doc_scores.get(str(document_id), 0.0) + weights[
            str(entity_id)
        ]
    ranked = sorted(doc_scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:limit]
