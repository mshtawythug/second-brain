"""Scope-first "themes with X" graph retrieval (spec §6b — the HEADLINE feature).

Extracted from :mod:`brain.graph_rag.retrieve` (the G2 wave-boundary file-size
split, mirroring the G1 :mod:`brain.graph_rag.aggregates` / G2-c
:mod:`brain.graph_rag.relational` extractions). The public dispatch entry
:func:`brain.graph_rag.retrieve.graph_rag_search` calls :func:`_retrieve_themes`
here when the router resolves ``mode='themes'``; the shared entity-row +
doc-result helpers live in :mod:`brain.graph_rag._retrieval_common`. This is a
pure move — no behavior change.

The algorithm (spec §6b): scope to a person ``X`` via
:meth:`~brain.graph_rag.backends.base.GraphBackend.scope_person`, compute the
**in-scope normalized lift** over ``graph_edge_contributions`` restricted to X's
documents, suppress entities that are **generic across the whole tenant** (the
corpus-wide df vs the corpus cap — matching derive-time suppression in
:func:`brain.graph_rag.aggregates._recompute_aggregates`), **exclude the seed X
and the owner**, group the scoped subgraph into ranked
:class:`~brain.graph_rag.schema.ThemeGroup`s
(:func:`brain.graph_rag.grouping.group_themes`), populate each group with its
representative X-documents + snippets, and (opt-in ``synthesize``) attach a
best-effort Ollama group summary. Cypher provides the scoping; SQL provides the
weighting/evidence; Python shapes the ``GraphContext`` (spec §6b closing note).
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol

import psycopg

from ._retrieval_common import _build_doc_results, _fetch_entities
from .grouping import group_themes
from .router import THEMES_MODE
from .schema import Edge, GraphContext, GraphEntity, GraphExplanation, ThemeGroup
from .weighting import generic_df_cap, is_generic_entity, normalized_lift

if TYPE_CHECKING:
    from ..search import SearchResult
    from .backends.base import GraphBackend

_logger = logging.getLogger(__name__)


class _GroupSummarizer(Protocol):
    """Structural type for the optional ``--synthesize`` enricher (DI seam).

    The themes path depends only on this one method, so production injects a
    :class:`brain.enrichment.OllamaEnricher` and tests inject a fake — neither is
    imported here. ``summarize_group`` is best-effort and **never raises**
    (returns ``None`` + a WARN on Ollama failure), so the themes retrieval never
    becomes a hard live-Ollama dependency (spec §17b decision 7).
    """

    def summarize_group(
        self,
        *,
        person: str | None,
        entity_names: list[str],
        doc_titles: list[str],
    ) -> str | None: ...


# --------------------------------------------------------------------------- #
# Themes-with-X orchestration (spec §6b — the HEADLINE feature)
# --------------------------------------------------------------------------- #
def _retrieve_themes(
    conn: psycopg.Connection[Any],
    *,
    person: str,
    query: str,
    backend: GraphBackend,
    tenant_id: str,
    frontier_cap: int,
    min_edge_weight: float,
    generic_df_ratio: float,
    owner_keys: frozenset[str],
    theme_limit: int,
    limit: int,
    session_id: str,
    synthesize: bool,
    enricher: _GroupSummarizer | None,
    exclude_confidential: bool = False,
) -> GraphContext:
    """Run scope-first "themes with X" retrieval + assemble its ``GraphContext``.

    Scope X (:meth:`GraphBackend.scope_person`), compute the in-scope normalized
    lift over ``graph_edge_contributions`` restricted to X's documents, suppress
    generic entities, exclude X + owner, group the scoped subgraph, populate each
    group's representative X-docs + snippets, and optionally synthesize a summary.

    Empty at any stage (no person vertex in the graph, an empty scope, no
    eligible entities) flows through to an empty-but-valid context — never-raise,
    mirroring the local no-seed path. ``PersonNotFound`` / ``PersonAmbiguous``
    from the resolver are *not* swallowed (the CLI/MCP map them in G2-h/i).
    """
    # Late import: brain.queries → brain.people pulls a heavier subtree;
    # resolving lazily keeps brain.graph_rag import-cheap (mirrors the
    # brain.search late import in _build_doc_results).
    from ..queries import resolve_person_to_keys

    # 1. Resolve the person string → canonical participant keys (deterministic;
    #    raises PersonNotFound / PersonAmbiguous for the CLI/MCP to map).
    match = resolve_person_to_keys(conn, person)
    person_display = match.display_name

    # 2. Map the resolved person → seed person ENTITY ids, and the owner → its
    #    entity ids; both sets are excluded from the theme-eligible set (§17.5).
    seed_entity_ids = _person_entity_ids(conn, tenant_id, match.keys)
    owner_entity_ids = _person_entity_ids(
        conn, tenant_id, sorted(key.lower() for key in owner_keys)
    )
    seed_set = set(seed_entity_ids)
    owner_set = set(owner_entity_ids)

    # 3. Scope: union each seed's co-mentioned entities + connecting documents.
    scope_entity_uuids: set[str] = set()
    scope_doc_uuids: set[str] = set()
    for seed_id in seed_entity_ids:
        scope = backend.scope_person(
            conn, tenant_id, seed_id, frontier_cap=frontier_cap
        )
        scope_entity_uuids.update(scope.entity_uuids)
        scope_doc_uuids.update(scope.document_uuids)
    scope_doc_ids = sorted(scope_doc_uuids)

    # 4. Candidate theme entities: co-mentioned, minus X (every seed) + owner.
    candidate_ids = sorted(scope_entity_uuids - seed_set - owner_set)

    # 5. Generic suppression uses the CORPUS-WIDE document frequency vs the corpus
    #    cap — matching the derive-time suppression in
    #    ``aggregates._recompute_aggregates`` (spec §6b step 2 / §7). The in-scope
    #    df (max = |X's docs|) almost never reaches the corpus-sized cap, so a
    #    person/concept that is generic across the whole tenant but co-occurs with
    #    X in only a few of X's docs would wrongly survive into X's themes. The
    #    in-scope df is retained ONLY as the lift normalizer in ``_in_scope_edges``
    #    (the ``eid in in_scope_df`` guard also ensures every eligible endpoint has
    #    a normalizer value there).
    in_scope_df = _in_scope_df(conn, tenant_id, scope_doc_ids, candidate_ids)
    corpus_df = _corpus_df(conn, tenant_id, candidate_ids)
    cap = generic_df_cap(_corpus_doc_count(conn, tenant_id), generic_df_ratio)
    eligible_ids = [
        eid
        for eid in candidate_ids
        if eid in in_scope_df and not is_generic_entity(corpus_df.get(eid, 0), cap)
    ]

    # 6. In-scope normalized-lift edges among eligible entities → grouping.
    #    Thread ``in_scope_df`` (person-scoped distinct-doc count) onto each entity
    #    before grouping so callers can show "N docs with X" per entity (A2).
    #    Every eligible_id is guaranteed present in in_scope_df (the eligibility
    #    guard at step 5 requires ``eid in in_scope_df``), so ``.get(e.id, 0)`` is
    #    purely defensive and is never reached in production.
    edges = _in_scope_edges(conn, tenant_id, scope_doc_ids, eligible_ids, in_scope_df)
    entities = [
        replace(e, scoped_doc_count=in_scope_df.get(e.id, 0))
        for e in _fetch_entities(conn, tenant_id, eligible_ids)
    ]
    groups = group_themes(
        entities, edges, min_edge_weight=min_edge_weight, theme_limit=theme_limit
    )

    # 7. Representative X-docs + snippets per group (and the context-level docs).
    groups, doc_results = _populate_theme_docs(
        conn,
        tenant_id,
        query,
        groups,
        scope_doc_ids,
        limit,
        exclude_confidential=exclude_confidential,
    )

    # 8. Optional best-effort group synthesis (never required for retrieval).
    if synthesize:
        groups = _synthesize_groups(conn, groups, person_display, enricher)

    explanation = GraphExplanation(
        mode=THEMES_MODE,
        tenant_id=tenant_id,
        seed_entity_ids=seed_entity_ids,
        person_keys=list(match.keys),
        depth=0,  # themes scopes person→docs→co-entities; no depth traversal
        frontier_cap=frontier_cap,
        min_edge_weight=min_edge_weight,
        nodes_visited=len(eligible_ids),
        edges_considered=len(edges),
        generic_df_cap=cap,
        matched_filters={
            "person": person_display,
            "person_keys": list(match.keys),
            "seed_entity_count": len(seed_entity_ids),
            "scope_doc_count": len(scope_doc_ids),
            "scope_entity_count": len(scope_entity_uuids),
            "eligible_entity_count": len(eligible_ids),
            "generic_df_cap": cap,
            "theme_count": len(groups),
            "theme_limit": theme_limit,
            "min_edge_weight": min_edge_weight,
            "frontier_cap": frontier_cap,
            "synthesize": synthesize,
        },
    )
    return GraphContext(
        session_id=session_id,
        mode=THEMES_MODE,
        query=query,
        tenant_id=tenant_id,
        person=person_display,
        themes=groups,
        entities=_theme_member_entities(groups),
        docs=doc_results,
        explanation=explanation,
    )


def _person_entity_ids(
    conn: psycopg.Connection[Any], tenant_id: str, keys: list[str]
) -> list[str]:
    """Resolve participant ``keys`` to ``person`` entity ids (tenant-scoped).

    A person ``graph_entities`` row's ``canonical_key`` is the lowercased
    People-Hub display name; ``keys`` (from ``resolve_person_to_keys`` / the
    owner key set) carry that same lowercased form alongside emails, so the
    name key matches and the email keys harmlessly miss. Ordered by id for a
    deterministic seed list. An empty ``keys`` resolves to no entities.
    """
    if not keys:
        return []
    rows = conn.execute(
        "SELECT id::text FROM graph_entities "
        "WHERE tenant_id = %s AND entity_type = 'person' "
        "AND lower(canonical_key) = ANY(%s) ORDER BY id",
        (tenant_id, keys),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _corpus_doc_count(conn: psycopg.Connection[Any], tenant_id: str) -> int:
    """Tenant corpus N = distinct documents with any mention (spec §6b step 2).

    Identical definition to the reconcile recompute path
    (:func:`brain.graph_rag.aggregates._recompute_aggregates`) so the themes
    generic cap matches the derive-time suppression cap — never recomputed
    differently.
    """
    row = conn.execute(
        "SELECT COUNT(DISTINCT document_id) FROM graph_entity_mentions "
        "WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _corpus_df(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    entity_ids: list[str],
) -> dict[str, int]:
    """Per-entity CORPUS-WIDE document frequency (``graph_entities.doc_count``).

    The maintained tenant-wide df refreshed by
    :func:`brain.graph_rag.aggregates._recompute_aggregates`, so the themes
    generic-suppression filter (:func:`_retrieve_themes` step 5) uses the SAME df
    scope as derive-time suppression — corpus-wide df vs the corpus cap — instead
    of the in-scope df (whose max is ``|X's docs|`` and so would leave the
    corpus-sized cap effectively unreachable, never suppressing a tenant-generic
    entity that merely co-occurs with X). An empty entity set yields an empty map;
    an entity with no catalog row is simply absent (the caller treats a miss as
    df 0, i.e. not generic — defensive, never reached for catalog ids).
    """
    if not entity_ids:
        return {}
    rows = conn.execute(
        "SELECT id::text, doc_count FROM graph_entities "
        "WHERE tenant_id = %s AND id = ANY(%s)",
        (tenant_id, entity_ids),
    ).fetchall()
    return {str(entity_id): int(count) for entity_id, count in rows}


def _in_scope_df(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    doc_ids: list[str],
    entity_ids: list[str],
) -> dict[str, int]:
    """Per-entity document frequency WITHIN X's scoped document set.

    ``df_in_scope[e]`` = distinct ``doc_ids`` (X's docs) that mention ``e``. The
    normalizer + generic-suppression input for the in-scope lift (spec §6b
    step 2). An empty doc/entity set yields an empty map.
    """
    if not doc_ids or not entity_ids:
        return {}
    rows = conn.execute(
        "SELECT entity_id::text, COUNT(DISTINCT document_id) "
        "FROM graph_entity_mentions "
        "WHERE tenant_id = %s AND document_id = ANY(%s) AND entity_id = ANY(%s) "
        "GROUP BY entity_id",
        (tenant_id, doc_ids, entity_ids),
    ).fetchall()
    return {str(entity_id): int(count) for entity_id, count in rows}


def _in_scope_edges(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    doc_ids: list[str],
    eligible_ids: list[str],
    in_scope_df: dict[str, int],
) -> list[Edge]:
    """In-scope normalized-lift edges among eligible entities (spec §6b step 2).

    For each canonical pair of eligible entities, ``co_doc`` = distinct X-docs in
    which both co-occur (``graph_edge_contributions`` restricted to ``doc_ids``);
    the weight is ``normalized_lift(co_doc, df_src_in_scope, df_dst_in_scope)``.
    Both endpoints are constrained to ``eligible_ids`` so a suppressed/excluded
    entity never forms an edge. Fewer than two eligible entities → no edges.

    **Relational-mirror-drift tolerance.** ``co_doc`` (from
    ``graph_edge_contributions``) and each endpoint's in-scope df (from
    ``graph_entity_mentions``) come from two separate relational tables that can
    fall out of sync — e.g. an edge-contribution row whose corresponding mention
    row was never written. When that happens ``co_doc`` can exceed the rarer
    endpoint's in-scope df, which ``normalized_lift`` rejects with a
    ``WeightingError`` that would otherwise abort the ENTIRE ``themes --person``
    query. To keep one drifted edge from killing the whole query, a drifted edge
    is clamped to a valid weight (``co_doc → min(co_doc, min(df))``) with a WARN;
    an edge missing an in-scope normalizer entirely (df 0 — no valid lift) is
    skip-logged instead.
    """
    if not doc_ids or len(eligible_ids) < 2:
        return []
    rows = conn.execute(
        "SELECT src_id::text, dst_id::text, COUNT(DISTINCT document_id) "
        "FROM graph_edge_contributions "
        "WHERE tenant_id = %s AND document_id = ANY(%s) "
        "AND src_id = ANY(%s) AND dst_id = ANY(%s) "
        "GROUP BY src_id, dst_id",
        (tenant_id, doc_ids, eligible_ids, eligible_ids),
    ).fetchall()
    edges: list[Edge] = []
    for src_id, dst_id, co_doc in rows:
        src = str(src_id)
        dst = str(dst_id)
        df_src = in_scope_df.get(src, 0)
        df_dst = in_scope_df.get(dst, 0)
        min_df = min(df_src, df_dst)
        co = int(co_doc)
        if min_df < 1 or co < 1:
            # Mirror drift: an endpoint has no in-scope mention row (df 0), so the
            # edge has no valid normalizer. Skip it — a single drifted edge must
            # never abort the whole themes query.
            _logger.warning(
                "themes: skipping drifted in-scope edge (%s, %s): missing "
                "normalizer (df_src=%d df_dst=%d co_doc=%d) — relational mirror "
                "drift between graph_edge_contributions and graph_entity_mentions",
                src, dst, df_src, df_dst, co,
            )
            continue
        if co > min_df:
            # Mirror drift: the edge's in-scope co-document count exceeds the
            # rarer endpoint's in-scope df. Clamp to the valid maximum so
            # normalized_lift never raises (co_doc → min(co_doc, min(df))).
            _logger.warning(
                "themes: clamping drifted in-scope edge (%s, %s): co_doc %d "
                "exceeds min in-scope df %d — relational mirror drift between "
                "graph_edge_contributions and graph_entity_mentions",
                src, dst, co, min_df,
            )
            co = min_df
        weight = normalized_lift(co, df_src, df_dst)
        edges.append(Edge(src_id=src, dst_id=dst, weight=weight, tenant_id=tenant_id))
    return edges


def _docs_by_entity(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    doc_ids: list[str],
    entity_ids: list[str],
) -> dict[str, set[str]]:
    """Map each entity id → the set of X-docs (in ``doc_ids``) mentioning it."""
    if not doc_ids or not entity_ids:
        return {}
    rows = conn.execute(
        "SELECT entity_id::text, document_id::text FROM graph_entity_mentions "
        "WHERE tenant_id = %s AND document_id = ANY(%s) AND entity_id = ANY(%s)",
        (tenant_id, doc_ids, entity_ids),
    ).fetchall()
    out: dict[str, set[str]] = {}
    for entity_id, document_id in rows:
        out.setdefault(str(entity_id), set()).add(str(document_id))
    return out


def _populate_theme_docs(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    query: str,
    groups: list[ThemeGroup],
    scope_doc_ids: list[str],
    limit: int,
    exclude_confidential: bool = False,
) -> tuple[list[ThemeGroup], list[SearchResult]]:
    """Attach representative X-docs to each group + build the context docs.

    A group's representative docs are X's scoped documents that mention the
    group's entities, ranked by the count of the group's entities present
    (DESC), then document id (ASC) — deterministic — and capped at ``limit``.
    The context-level ``docs`` rank every contributing X-doc by the count of
    eligible entities it mentions and reuse :func:`_build_doc_results` for the
    snippet path (so themes docs match the local/hybrid snippet shape).
    """
    if not groups:
        return [], []
    member_ids = sorted({entity.id for group in groups for entity in group.entities})
    docs_by_entity = _docs_by_entity(conn, tenant_id, scope_doc_ids, member_ids)

    populated: list[ThemeGroup] = []
    global_doc_score: dict[str, float] = {}
    for group in groups:
        doc_score: dict[str, int] = {}
        for entity in group.entities:
            for doc_id in docs_by_entity.get(entity.id, set()):
                doc_score[doc_id] = doc_score.get(doc_id, 0) + 1
        ranked = sorted(doc_score.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        populated.append(replace(group, doc_ids=[doc_id for doc_id, _ in ranked]))
        for doc_id, count in doc_score.items():
            global_doc_score[doc_id] = global_doc_score.get(doc_id, 0.0) + count

    global_ranked = sorted(
        global_doc_score.items(), key=lambda kv: (-kv[1], kv[0])
    )[:limit]
    return populated, _build_doc_results(
        conn, query, global_ranked, exclude_confidential=exclude_confidential
    )


def _synthesize_groups(
    conn: psycopg.Connection[Any],
    groups: list[ThemeGroup],
    person_display: str,
    enricher: _GroupSummarizer | None,
) -> list[ThemeGroup]:
    """Attach a best-effort Ollama summary to each group (spec §17b decision 7).

    Opt-in (``--synthesize``); ``summarize_group`` is best-effort and never
    raises (returns ``None`` + a WARN on Ollama failure), so retrieval succeeds
    regardless. A ``synthesize=True`` call with no injected ``enricher`` is a
    caller wiring gap — logged WARN, summaries left ``None`` (still never-raise).
    """
    if not groups:
        return groups
    if enricher is None:
        _logger.warning(
            "themes synthesize=True but no enricher injected; summaries stay None"
        )
        return groups
    all_doc_ids = sorted({doc_id for group in groups for doc_id in group.doc_ids})
    title_by_doc = _fetch_doc_titles(conn, all_doc_ids)
    summarized: list[ThemeGroup] = []
    for group in groups:
        titles = [
            title_by_doc[doc_id] for doc_id in group.doc_ids if doc_id in title_by_doc
        ]
        try:
            summary = enricher.summarize_group(
                person=person_display,
                entity_names=[entity.name for entity in group.entities],
                doc_titles=titles,
            )
        except Exception as exc:  # noqa: BLE001 -- best-effort: never fail retrieval
            # summarize_group already returns None on Ollama down/timeout/invalid
            # (spec §17b decision 7); this guard is defence-in-depth at the
            # optional-side-work boundary (mirrors GraphSyncer) so even a
            # misbehaving injected enricher can never turn themes retrieval into
            # a hard live-Ollama dependency.
            _logger.warning(
                "themes synthesize: group %d summary failed (%s); summary=None",
                group.group_id,
                exc,
            )
            summary = None
        summarized.append(replace(group, summary=summary))
    return summarized


def _fetch_doc_titles(
    conn: psycopg.Connection[Any], doc_ids: list[str]
) -> dict[str, str]:
    """Map document id → title for the group-synthesis prompt (no body content)."""
    if not doc_ids:
        return {}
    rows = conn.execute(
        "SELECT id::text, title FROM documents WHERE id = ANY(%s)",
        (doc_ids,),
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def _theme_member_entities(groups: list[ThemeGroup]) -> list[GraphEntity]:
    """Flat, deduped, deterministically-sorted entity list across all groups."""
    by_id: dict[str, GraphEntity] = {}
    for group in groups:
        for entity in group.entities:
            by_id.setdefault(entity.id, entity)
    return sorted(
        by_id.values(), key=lambda e: (e.canonical_key, e.entity_type, e.id)
    )
