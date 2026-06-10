"""Weekly-review orchestrator: assemble a :class:`WeeklyReport` from the DB.

This module owns *which* sections are assembled and *how* themes are ranked —
nothing about rendering or vault I/O (those live in ``render`` / ``emit``). It
reads only existing tables (no new migration): ``interactions`` / ``documents``
for activity + ingest signal (via :mod:`brain.activity`), ``krisp_action_items``
docs for open loops (via :mod:`brain.todo`), and the relational graph tables
(``graph_communities`` / ``graph_community_members`` / ``graph_edge_contributions``
/ ``graph_entities`` / ``graph_entity_mentions``) for the theme clusters.

The theme leg has two paths:

* **Graph path** (``cfg.graph_enabled and not no_graph``): rank the tenant's
  communities by in-window co-occurrence weight, then attach top entity names,
  representative in-window doc titles, and a best-effort LLM synthesis. These
  reads hit the relational source-of-truth tables (always present after the
  graph migrations) — NOT Apache AGE — so the path is safe on stock pgvector and
  simply yields no themes when the graph has not been built.
* **Fallback path** (graph disabled, ``--no-graph``, or zero active communities):
  cluster the activity docs by their most-frequent tag. No LLM synthesis.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg

from ..activity import (
    ActivityDoc,
    IngestedDoc,
    iter_activity_docs,
    iter_ingested_docs,
    week_bounds,
)
from ..config import Config
from ..enrichment import OllamaEnricher
from ..todo import TodoRow, iter_action_item_docs
from ..wiki.build_people import _doc_participant_keys

# Top entity names / representative doc titles attached per graph theme block.
_THEME_ENTITY_CAP = 3
_THEME_DOC_CAP = 3
# Canonical key-people count surfaced in a weekly report.
_KEY_PEOPLE_CAP = 5


@dataclass(frozen=True)
class ThemeBlock:
    """One synthesized theme cluster for the week.

    ``key`` is the ``community_key`` (graph path) or the tag name (fallback).
    ``entity_names`` are the cluster's top entities (graph) or ``[tag]``
    (fallback). ``docs`` are ``(document_id, title)`` pairs for representative
    in-window documents. ``synthesis`` is the best-effort LLM one-liner (graph
    path only; ``None`` on the fallback path or when Ollama is unavailable).
    """

    key: str
    entity_names: list[str]
    docs: list[tuple[str, str]]
    synthesis: str | None


@dataclass(frozen=True)
class WeeklyReport:
    """Everything the renderer needs for one weekly review page.

    ``vault_paths`` maps ``document_id`` → ``documents.vault_path`` for every
    referenced doc that has one, so the renderer can emit wiki-links without
    re-querying (docs without a vault path render as plain titles).
    ``graph_used`` records whether the theme leg took the graph path.
    """

    week: str
    start_date: date
    end_date: date
    generated_date: date
    themes: list[ThemeBlock]
    activity: list[ActivityDoc]
    open_loops: list[TodoRow]
    ingested: list[IngestedDoc]
    key_people: list[str]
    graph_used: bool
    vault_paths: dict[str, str]


def build_weekly_report(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    week: str,
    generated_on: date,
    no_graph: bool = False,
    enricher: OllamaEnricher | None = None,
) -> WeeklyReport:
    """Assemble the :class:`WeeklyReport` for ``week`` (``"YYYY-Www"``).

    ``generated_on`` is the page's ``date:`` stamp (the caller passes today so
    this function stays free of wall-clock reads and is deterministic under
    test). ``no_graph`` forces the tag-cluster fallback. ``enricher`` enables
    best-effort theme synthesis on the graph path; when ``None`` the theme
    blocks carry entity/doc names without a synthesis sentence.

    Raises:
        ValueError: ``week`` is not a valid ``"YYYY-Www"`` string (from
            :func:`brain.activity.week_bounds`).
    """
    after, before = week_bounds(week)

    activity = iter_activity_docs(
        conn, after=after, before=before, limit=cfg.review_activity_limit
    )
    ingested = iter_ingested_docs(
        conn, after=after, before=before, limit=cfg.review_activity_limit
    )
    # Open loops are scoped to the TARGET week, not the last 7 days from NOW():
    # ``iter_action_item_docs(since_days=...)`` is NOW()-relative (todo.py), which
    # would surface the current week's loops for a past --week. Pull all open
    # action items and filter by the requested window so a past-week retrospective
    # shows that week's loops.
    open_loops = [
        row
        for row in iter_action_item_docs(conn, include_closed=False)
        if row.ingested_at is not None and after <= row.ingested_at <= before
    ][: cfg.review_open_loop_limit]

    use_graph = cfg.graph_enabled and not no_graph
    themes: list[ThemeBlock] = []
    if use_graph:
        themes = _graph_themes(
            conn,
            tenant_id=cfg.graph_tenant_id,
            after=after,
            before=before,
            theme_limit=cfg.review_theme_limit,
            enricher=enricher,
        )
    graph_used = use_graph and bool(themes)
    if not themes:
        themes = _tag_cluster_themes(activity, theme_limit=cfg.review_theme_limit)

    key_people = _key_people(conn, activity, cap=_KEY_PEOPLE_CAP)

    vault_paths = _collect_vault_paths(conn, activity, ingested, open_loops, themes)

    return WeeklyReport(
        week=week,
        start_date=after.date(),
        end_date=before.date(),
        generated_date=generated_on,
        themes=themes,
        activity=activity,
        open_loops=open_loops,
        ingested=ingested,
        key_people=key_people,
        graph_used=graph_used,
        vault_paths=vault_paths,
    )


def weekly_active_communities(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    after: datetime,
    before: datetime,
    theme_limit: int,
) -> list[tuple[str, int]]:
    """Return ``(community_key, weekly_weight)`` for in-window-active communities.

    Sums ``graph_edge_contributions.cooccur_count`` for edges whose source
    document was ingested in ``[after, before]``, grouped by the community each
    edge endpoint belongs to. Tenant-scoped on both the edge and the membership
    join. Returns ``[]`` when the graph has no in-window activity (no crash) —
    the caller then falls back to tag clusters.
    """
    rows = conn.execute(
        """
        SELECT gcm.community_key::text, SUM(gec.cooccur_count) AS weekly_weight
        FROM   graph_edge_contributions gec
        JOIN   graph_community_members gcm
                 ON gcm.tenant_id = gec.tenant_id
                AND (gcm.entity_id = gec.src_id OR gcm.entity_id = gec.dst_id)
        JOIN   documents d ON d.id = gec.document_id
        WHERE  gec.tenant_id = %s
          AND  d.ingested_at BETWEEN %s AND %s
        GROUP  BY gcm.community_key
        ORDER  BY weekly_weight DESC, gcm.community_key
        LIMIT  %s
        """,
        (tenant_id, after, before, theme_limit),
    ).fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


def _graph_themes(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    after: datetime,
    before: datetime,
    theme_limit: int,
    enricher: OllamaEnricher | None,
) -> list[ThemeBlock]:
    """Build theme blocks from the in-window-active communities (graph path)."""
    blocks: list[ThemeBlock] = []
    for community_key, _weight in weekly_active_communities(
        conn,
        tenant_id=tenant_id,
        after=after,
        before=before,
        theme_limit=theme_limit,
    ):
        entity_names = _community_entity_names(
            conn, tenant_id=tenant_id, community_key=community_key
        )
        docs = _community_window_docs(
            conn,
            tenant_id=tenant_id,
            community_key=community_key,
            after=after,
            before=before,
        )
        synthesis = _community_summary(
            conn, tenant_id=tenant_id, community_key=community_key
        )
        if synthesis is None and enricher is not None and entity_names:
            # Best-effort — summarize_group never raises (logs + returns None
            # when Ollama is unavailable).
            synthesis = enricher.summarize_group(
                person=None,
                entity_names=entity_names,
                doc_titles=[title for _id, title in docs],
            )
        blocks.append(
            ThemeBlock(
                key=community_key,
                entity_names=entity_names,
                docs=docs,
                synthesis=synthesis,
            )
        )
    return blocks


def _community_entity_names(
    conn: psycopg.Connection[Any], *, tenant_id: str, community_key: str
) -> list[str]:
    """Top entity names for a community (by member rank then weight)."""
    rows = conn.execute(
        """
        SELECT ge.name
        FROM   graph_community_members gcm
        JOIN   graph_entities ge
                 ON ge.tenant_id = gcm.tenant_id AND ge.id = gcm.entity_id
        WHERE  gcm.tenant_id = %s AND gcm.community_key = %s
        ORDER  BY gcm.member_rank, gcm.member_weight DESC, ge.name
        LIMIT  %s
        """,
        (tenant_id, community_key, _THEME_ENTITY_CAP),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _community_window_docs(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    community_key: str,
    after: datetime,
    before: datetime,
) -> list[tuple[str, str]]:
    """Representative ``(id, title)`` docs in-window for a community's entities."""
    rows = conn.execute(
        """
        SELECT d.id::text, d.title
        FROM   documents d
        WHERE  d.ingested_at BETWEEN %s AND %s
          AND  d.id IN (
                SELECT gem.document_id
                FROM   graph_entity_mentions gem
                JOIN   graph_community_members gcm
                         ON gcm.tenant_id = gem.tenant_id
                        AND gcm.entity_id = gem.entity_id
                WHERE  gcm.tenant_id = %s AND gcm.community_key = %s
          )
        ORDER  BY d.ingested_at DESC, d.id
        LIMIT  %s
        """,
        (after, before, tenant_id, community_key, _THEME_DOC_CAP),
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def _community_summary(
    conn: psycopg.Connection[Any], *, tenant_id: str, community_key: str
) -> str | None:
    """Return the stored community summary, or ``None`` if not yet materialized."""
    row = conn.execute(
        "SELECT summary FROM graph_communities "
        "WHERE tenant_id = %s AND community_key = %s",
        (tenant_id, community_key),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    summary = str(row[0]).strip()
    return summary or None


def _tag_cluster_themes(
    activity: list[ActivityDoc], *, theme_limit: int
) -> list[ThemeBlock]:
    """Fallback theme leg: cluster activity docs by their most-frequent tag.

    Each doc is bucketed under its first tag (docs with no tags are skipped).
    Buckets are ranked by document count (then tag name) and capped at
    ``theme_limit``. No LLM synthesis on this path.
    """
    buckets: dict[str, list[tuple[str, str]]] = {}
    for doc in activity:
        if not doc.tags:
            continue
        tag = doc.tags[0]
        buckets.setdefault(tag, []).append((doc.document_id, doc.title))
    ranked = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return [
        ThemeBlock(
            key=tag,
            entity_names=[tag],
            docs=docs[:_THEME_DOC_CAP],
            synthesis=None,
        )
        for tag, docs in ranked[:theme_limit]
    ]


def _key_people(
    conn: psycopg.Connection[Any], activity: list[ActivityDoc], *, cap: int
) -> list[str]:
    """Tally participant keys across the activity docs; return the top ``cap``.

    Reuses :func:`brain.wiki.build_people._doc_participant_keys` (imported, not
    copy-pasted) to extract each doc's raw participant keys from its
    ``metadata`` + joined ``sources.kind``. Keys are tallied by frequency
    (ties broken alphabetically) and de-duplicated.
    """
    if not activity:
        return []
    doc_ids = [doc.document_id for doc in activity]
    rows = conn.execute(
        """
        SELECT d.id::text, s.kind, d.metadata
        FROM   documents d
        LEFT JOIN sources s ON s.id = d.source_id
        WHERE  d.id = ANY(%s)
        """,
        (doc_ids,),
    ).fetchall()
    counts: dict[str, int] = {}
    for _doc_id, source_kind, metadata in rows:
        meta: dict[str, Any] = dict(metadata) if metadata else {}
        keys = _doc_participant_keys(source_kind=source_kind or "", metadata=meta)
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [key for key, _count in ranked[:cap]]


def _collect_vault_paths(
    conn: psycopg.Connection[Any],
    activity: list[ActivityDoc],
    ingested: list[IngestedDoc],
    open_loops: list[TodoRow],
    themes: list[ThemeBlock],
) -> dict[str, str]:
    """Map ``document_id`` → ``vault_path`` for every referenced doc that has one.

    A single batched lookup over the union of all referenced ids so the renderer
    can emit wiki-links (``[[<vault-path>|<title>]]``) for the docs that are
    mirrored to the vault, and plain titles for those that are not.
    """
    ids: set[str] = set()
    ids.update(doc.document_id for doc in activity)
    ids.update(doc.document_id for doc in ingested)
    ids.update(row.document_id for row in open_loops)
    for block in themes:
        ids.update(doc_id for doc_id, _title in block.docs)
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT id::text, vault_path FROM documents "
        "WHERE id = ANY(%s) AND vault_path IS NOT NULL",
        (list(ids),),
    ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}
