"""Output formatting (human + JSON)."""
import json
from typing import TYPE_CHECKING, Any

from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text

from .search import SearchExplanation, SearchResult

if TYPE_CHECKING:
    from .eval.baseline import BaselineDiff
    from .eval.runner import EvalReport
    from .graph_rag.schema import (
        CommunityGroup,
        CommunityRecord,
        EntitySummary,
        GraphContext,
        GraphEntity,
        GraphExplanation,
        GraphStats,
        ThemeGroup,
    )

# nDCG@5 delta threshold below which a query row is highlighted red in the
# diff table.  Display-only — the CLI never exits non-zero based on this.
_EVAL_REGRESSION_THRESHOLD: float = -0.05

console = Console()


def emit_json(payload: Any) -> None:
    """Print a JSON-serializable payload as pretty JSON via Rich."""
    console.print_json(json.dumps(payload, default=str))


def search_table(results: list[SearchResult], *, title: str = "Search results") -> Table:
    """Render hybrid-search results as a Rich table.

    ``title`` defaults to ``"Search results"`` for ``brain search``; graph
    retrieval reuses the same row shape for its document hits with a
    ``"Documents"`` title (spec §4 D8 — graph ``docs`` reuse ``SearchResult``).
    """
    table = Table(title=title)
    table.add_column("ID", style="dim")
    table.add_column("Title")
    table.add_column("Source", style="cyan")
    table.add_column("Score", justify="right")
    table.add_column("Snippet")
    for r in results:
        table.add_row(
            r.document_id[:8],
            r.title,
            r.source_kind or "manual",
            f"{r.score:.3f}",
            r.snippet[:120].replace("\n", " "),
        )
    return table


def _fmt_opt_int(val: int | None) -> str:
    """Format an optional integer rank as a string, using '-' for None."""
    return str(val) if val is not None else "-"


def _fmt_opt_float(val: float | None, precision: int = 4) -> str:
    """Format an optional float, using '-' for None."""
    return f"{val:.{precision}f}" if val is not None else "-"


def _fmt_filters(matched_filters: dict[str, Any]) -> str:
    """Render matched_filters as a compact string, omitting None/False values."""
    parts = []
    for key, value in matched_filters.items():
        if value is None or value is False:
            continue
        # Show boolean flags without the "=True" suffix for readability.
        if isinstance(value, bool):
            parts.append(key)
        else:
            parts.append(f"{key}={value}")
    return " · ".join(parts) if parts else "(none)"


def explain_table(results: list[SearchResult], *, verbose: bool = False) -> Table:
    """Render hybrid-search results with full ranking diagnostics as a Rich table.

    Columns (default): ID / Title / Source / FTS# / Vec# / Vec-cos / RRF /
    Recency / Final / Best-chunk#.
    With ``verbose=True`` a Filters column is appended.

    Results without an :class:`SearchExplanation` (``explain is None``) render
    with ``-`` in all diagnostic columns — this should not happen in normal use
    since ``brain explain`` always sets ``explain=True``.
    """
    table = Table(title="Explain results")
    table.add_column("ID", style="dim")
    table.add_column("Title")
    table.add_column("Source", style="cyan")
    table.add_column("FTS#", justify="right")
    table.add_column("Vec#", justify="right")
    table.add_column("Vec-cos", justify="right")
    table.add_column("RRF", justify="right")
    table.add_column("Recency", justify="right")
    table.add_column("Final", justify="right")
    table.add_column("Best-chunk#", justify="right")
    if verbose:
        table.add_column("Filters")

    for r in results:
        ex: SearchExplanation | None = r.explain
        if ex is not None:
            row = [
                r.document_id[:8],
                r.title,
                r.source_kind or "manual",
                _fmt_opt_int(ex.fts_rank),
                _fmt_opt_int(ex.vector_rank),
                _fmt_opt_float(ex.vector_cosine),
                f"{ex.rrf_score:.5f}",
                f"{ex.recency_boost:.4f}×",
                f"{ex.final_score:.5f}",
                f"#{ex.best_chunk_index}",
            ]
            if verbose:
                row.append(_fmt_filters(ex.matched_filters))
        else:
            # Fallback for results that somehow lack an explanation.
            row = [
                r.document_id[:8],
                r.title,
                r.source_kind or "manual",
                "-", "-", "-", f"{r.score:.5f}", "-", f"{r.score:.5f}", "-",
            ]
            if verbose:
                row.append("-")
        table.add_row(*row)

    return table


# ---------------------------------------------------------------------------
# Eval report tables
# ---------------------------------------------------------------------------


def eval_report_table(report: "EvalReport") -> Table:
    """Render an :class:`~brain.eval.runner.EvalReport` as a Rich table.

    Columns: Category / Query / nDCG@5 / MRR / Recall@20.
    A separator row is added at the bottom with the aggregate means.
    """
    table = Table(title="Eval results")
    table.add_column("Category", style="cyan")
    table.add_column("Query")
    table.add_column("nDCG@5", justify="right")
    table.add_column("MRR", justify="right")
    table.add_column("Recall@20", justify="right")

    for r in report.results:
        table.add_row(
            r.category,
            r.query[:60],
            f"{r.ndcg_at_5:.4f}",
            f"{r.mrr:.4f}",
            f"{r.recall_at_20:.4f}",
        )

    table.add_section()
    table.add_row(
        "[bold]mean[/bold]",
        "",
        f"[bold]{report.mean_ndcg_at_5:.4f}[/bold]",
        f"[bold]{report.mean_mrr:.4f}[/bold]",
        f"[bold]{report.mean_recall_at_20:.4f}[/bold]",
    )
    return table


def eval_diff_table(diff: "BaselineDiff") -> Table:
    """Render a :class:`~brain.eval.baseline.BaselineDiff` as a Rich table.

    Columns: Category / Query / ΔnDCG@5 / ΔMRR / ΔRecall@20.
    Rows where ``ndcg_at_5_delta < _EVAL_REGRESSION_THRESHOLD`` are highlighted
    red.  If the config signature changed between baseline and current, a
    caption is appended.
    """
    table = Table(title="Eval diff (current − baseline)")
    table.add_column("Category", style="cyan")
    table.add_column("Query")
    table.add_column("ΔnDCG@5", justify="right")
    table.add_column("ΔMRR", justify="right")
    table.add_column("ΔRecall@20", justify="right")

    for d in diff.per_query:
        regressed = d.ndcg_at_5_delta < _EVAL_REGRESSION_THRESHOLD
        style = "red" if regressed else ""
        table.add_row(
            d.category,
            d.query[:60],
            f"{d.ndcg_at_5_delta:+.4f}",
            f"{d.mrr_delta:+.4f}",
            f"{d.recall_at_20_delta:+.4f}",
            style=style,
        )

    table.add_section()
    agg_regressed = diff.mean_ndcg_at_5_delta < _EVAL_REGRESSION_THRESHOLD
    agg_style = "red" if agg_regressed else ""
    table.add_row(
        "[bold]mean[/bold]",
        "",
        f"[bold]{diff.mean_ndcg_at_5_delta:+.4f}[/bold]",
        f"[bold]{diff.mean_mrr_delta:+.4f}[/bold]",
        f"[bold]{diff.mean_recall_at_20_delta:+.4f}[/bold]",
        style=agg_style,
    )

    if diff.config_signature_changed:
        table.caption = "⚠ config_signature changed between baseline and current run"

    return table


# ---------------------------------------------------------------------------
# GraphRAG retrieval output (wave G2-h) — human renderable + JSON serializer
# for the ``GraphContext`` wire shape (spec §4 D8, §6, §9). NEVER exposes raw
# Cypher: the renderer reads only the structured value object.
# ---------------------------------------------------------------------------


def _entity_json(entity: "GraphEntity") -> dict[str, Any]:
    """Serialize one :class:`~brain.graph_rag.schema.GraphEntity` (read-side)."""
    return {
        "id": entity.id,
        "entity_type": entity.entity_type,
        "name": entity.name,
        "canonical_key": entity.canonical_key,
        "tenant_id": entity.tenant_id,
        "description": entity.description,
        "doc_count": entity.doc_count,
    }


def _graph_doc_json(doc: SearchResult) -> dict[str, Any]:
    """Serialize one graph document hit (a reused :class:`SearchResult`)."""
    return {
        "id": doc.document_id,
        "title": doc.title,
        "source_kind": doc.source_kind,
        "snippet": doc.snippet,
        "score": doc.score,
        "content_type": doc.content_type,
        "tags": doc.tags,
    }


def _theme_json(theme: "ThemeGroup") -> dict[str, Any]:
    """Serialize one :class:`~brain.graph_rag.schema.ThemeGroup`."""
    return {
        "group_id": theme.group_id,
        "score": theme.score,
        "summary": theme.summary,
        "entities": [_entity_json(e) for e in theme.entities],
        "doc_ids": list(theme.doc_ids),
    }


def _community_json(community: "CommunityGroup") -> dict[str, Any]:
    """Serialize one :class:`~brain.graph_rag.schema.CommunityGroup` (global mode).

    The wire shape for the top-level ``communities`` key (spec §17c Q5):
    ``community_key`` / ``level`` / ``member_count`` / ``score`` / ``summary`` +
    the representative ``entities`` (reusing :func:`_entity_json`) and
    ``doc_ids``. Mirrors :func:`_theme_json` for the themes path.
    """
    return {
        "community_key": community.community_key,
        "level": community.level,
        "member_count": community.member_count,
        "score": community.score,
        "summary": community.summary,
        "entities": [_entity_json(e) for e in community.entities],
        "doc_ids": list(community.doc_ids),
    }


def _explanation_json(explanation: "GraphExplanation | None") -> dict[str, Any] | None:
    """Serialize the :class:`~brain.graph_rag.schema.GraphExplanation` diagnostic."""
    if explanation is None:
        return None
    return {
        "mode": explanation.mode,
        "tenant_id": explanation.tenant_id,
        "seed_entity_ids": list(explanation.seed_entity_ids),
        "person_keys": list(explanation.person_keys),
        "depth": explanation.depth,
        "frontier_cap": explanation.frontier_cap,
        "min_edge_weight": explanation.min_edge_weight,
        "nodes_visited": explanation.nodes_visited,
        "edges_considered": explanation.edges_considered,
        "generic_df_cap": explanation.generic_df_cap,
        "matched_filters": explanation.matched_filters,
    }


def graph_context_json(ctx: "GraphContext") -> dict[str, Any]:
    """Serialize a :class:`~brain.graph_rag.schema.GraphContext` for ``--json``.

    The full structured wire shape (spec §9): the resolved/requested mode +
    degradation signals, the scoped person, the ranked ``themes`` (themes mode),
    ``communities`` (global mode; spec §17c Q5), and ``entities`` (local mode),
    the document hits, and the ranking ``explanation``. For ``fuse`` mode (wave
    G4-c; spec §17d Q1) the fused doc list is the standard ``docs`` array (no new
    field — wire-stable) and the per-doc leg provenance rides inside
    ``explanation.matched_filters['fuse_doc_provenance']`` (serialized verbatim
    here). Raw Cypher is never present — only the value object's fields.
    """
    return {
        "session_id": ctx.session_id,
        "mode": ctx.mode,
        "query": ctx.query,
        "tenant_id": ctx.tenant_id,
        "person": ctx.person,
        "requested_mode": ctx.requested_mode,
        "degraded_from": ctx.degraded_from,
        "degradation_reason": ctx.degradation_reason,
        "themes": [_theme_json(t) for t in ctx.themes],
        "communities": [_community_json(c) for c in ctx.communities],
        "entities": [_entity_json(e) for e in ctx.entities],
        "docs": [_graph_doc_json(d) for d in ctx.docs],
        "explanation": _explanation_json(ctx.explanation),
    }


def _graph_header(ctx: "GraphContext") -> Text:
    """Build the one-line ``GraphContext`` header (mode / person / tenant)."""
    parts = [f"mode={ctx.mode}"]
    if ctx.person:
        parts.append(f"person={ctx.person}")
    parts.append(f"tenant={ctx.tenant_id}")
    return Text("Graph RAG · " + " · ".join(parts), style="bold")


def _graph_themes_table(themes: "list[ThemeGroup]") -> Table:
    """Render the ranked theme groups (themes mode)."""
    table = Table(title="Themes")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Entities")
    table.add_column("Score", justify="right")
    table.add_column("Docs", justify="right")
    table.add_column("Summary")
    for theme in themes:
        table.add_row(
            str(theme.group_id),
            ", ".join(e.name for e in theme.entities),
            f"{theme.score:.3f}",
            str(len(theme.doc_ids)),
            (theme.summary or "").replace("\n", " "),
        )
    return table


def _graph_communities_table(communities: "list[CommunityGroup]") -> Table:
    """Render the ranked community groups (global mode; spec §17c Q5).

    Columns: # (1-based rank) / Key (short ``community_key``) / Entities (the
    representative member names) / Members (full ``member_count``) / Score (fused
    RRF) / Summary. Mirrors :func:`_graph_themes_table`.
    """
    table = Table(title="Communities")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Key", style="dim")
    table.add_column("Entities")
    table.add_column("Members", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Summary")
    for rank, community in enumerate(communities, start=1):
        table.add_row(
            str(rank),
            community.community_key[:8],
            ", ".join(e.name for e in community.entities),
            str(community.member_count),
            f"{community.score:.4f}",
            (community.summary or "").replace("\n", " "),
        )
    return table


def _graph_entities_table(entities: "list[GraphEntity]") -> Table:
    """Render the seed + reached entity neighbourhood (local mode)."""
    table = Table(title="Entities")
    table.add_column("Type", style="cyan")
    table.add_column("Name")
    table.add_column("Key", style="dim")
    table.add_column("Docs", justify="right")
    for entity in entities:
        table.add_row(
            entity.entity_type,
            entity.name,
            entity.canonical_key,
            str(entity.doc_count),
        )
    return table


def graph_context_renderable(ctx: "GraphContext") -> RenderableType:
    """Render a :class:`~brain.graph_rag.schema.GraphContext` for the terminal.

    A header line (mode / person / tenant), a degradation note when the auto
    router degraded ``global → local`` (spec §17b decision 4 — kept dormant in
    G3), the ranked ``communities`` (global mode; spec §17c Q5), ``themes``
    (themes mode), or the ``entities`` neighbourhood (local mode), and the
    document hits (reusing the search-result table shape). For ``fuse`` mode
    (wave G4-c; spec §17d Q1) the graph leg's ``entities`` render above the fused
    ``docs`` table (the per-doc leg provenance is in the ``--json`` explanation).
    An all-empty context renders the header + a ``(no graph results)`` line. Raw
    Cypher is never shown — only the structured value object.
    """
    blocks: list[RenderableType] = [_graph_header(ctx)]
    if ctx.degraded_from is not None:
        blocks.append(
            Text(
                f"note: requested {ctx.requested_mode!r} degraded "
                f"{ctx.degraded_from}→{ctx.mode} ({ctx.degradation_reason})",
                style="yellow",
            )
        )
    if ctx.communities:
        blocks.append(_graph_communities_table(ctx.communities))
    elif ctx.themes:
        blocks.append(_graph_themes_table(ctx.themes))
    elif ctx.entities:
        blocks.append(_graph_entities_table(ctx.entities))
    if ctx.docs:
        blocks.append(search_table(ctx.docs, title="Documents"))
    if not ctx.communities and not ctx.themes and not ctx.entities and not ctx.docs:
        blocks.append(Text("(no graph results)", style="dim"))
    return Group(*blocks)


# ---------------------------------------------------------------------------
# Community admin listing (`brain graphrag communities list`, wave G3-f).
# A persisted-row view (NOT a ranked retrieval group) — mirrors the stored
# ``graph_communities`` rows for an operator. Distinct from the global-mode
# ``CommunityGroup`` rendering above (which is per-query, RRF-ranked).
# ---------------------------------------------------------------------------


def _summary_preview(summary: str | None, limit: int = 80) -> str:
    """One-line summary preview for the admin table (NULL → ``"(none)"``)."""
    if not summary:
        return "(none)"
    flattened = summary.replace("\n", " ").strip()
    return flattened if len(flattened) <= limit else flattened[: limit - 1] + "…"


def community_record_json(record: "CommunityRecord") -> dict[str, Any]:
    """Serialize one :class:`~brain.graph_rag.schema.CommunityRecord` (admin view).

    The wire shape for ``brain graphrag communities list --json``: the stored
    community's identity + aggregate stats + summary metadata. Like the other
    read-side serializers it omits the raw ``summary_embedding`` vector (a
    storage handle, not a wire value).
    """
    return {
        "community_key": record.community_key,
        "level": record.level,
        "build_version": record.build_version,
        "member_count": record.member_count,
        "edge_count": record.edge_count,
        "total_weight": record.total_weight,
        "summary": record.summary,
        "summary_model": record.summary_model,
        "summary_at": record.summary_at.isoformat() if record.summary_at else None,
    }


def community_records_table(records: "list[CommunityRecord]") -> Table:
    """Render stored communities as a Rich table (admin listing; wave G3-f).

    Columns: Key (short ``community_key``) / Members / Edges / Weight / Summary
    (preview). An empty list still renders the (header-only) table.
    """
    table = Table(title="Communities")
    table.add_column("Key", style="dim")
    table.add_column("Members", justify="right")
    table.add_column("Edges", justify="right")
    table.add_column("Weight", justify="right")
    table.add_column("Summary")
    for record in records:
        table.add_row(
            record.community_key[:8],
            str(record.member_count),
            str(record.edge_count),
            f"{record.total_weight:.3f}",
            _summary_preview(record.summary),
        )
    return table


# ---------------------------------------------------------------------------
# Entity listing renderers (admin view; plan 2026-05-23)
# Mirrors the community_records_table / community_record_json pair above.
# ---------------------------------------------------------------------------

# Max characters shown for the description preview in the entity table.
_ENTITY_DESC_PREVIEW = 60


def entity_summaries_json(row: "EntitySummary") -> dict[str, Any]:
    """Serialize one :class:`~brain.graph_rag.schema.EntitySummary` (admin view).

    The per-entity wire shape for ``brain graphrag entities --json`` and the
    ``brain_graphrag_entities`` MCP tool. Mirrors :func:`community_record_json`.
    """
    return {
        "entity_type": row.entity_type,
        "name": row.name,
        "canonical_key": row.canonical_key,
        "doc_count": row.doc_count,
        "description": row.description,
    }


def entity_summaries_table(rows: "list[EntitySummary]") -> Table:
    """Render entity summaries as a Rich table (admin listing).

    Columns: Type / Name / Docs / Description (preview). An empty list still
    renders the (header-only) table.
    """
    table = Table(title="Entities")
    table.add_column("Type", style="cyan")
    table.add_column("Name")
    table.add_column("Docs", justify="right")
    table.add_column("Description")
    for row in rows:
        table.add_row(
            row.entity_type,
            row.name,
            str(row.doc_count),
            _summary_preview(row.description, limit=_ENTITY_DESC_PREVIEW),
        )
    return table


def graph_stats_json(stats: "GraphStats") -> dict[str, Any]:
    """Serialize a :class:`~brain.graph_rag.schema.GraphStats` (admin view).

    The wire shape for ``brain graphrag stats --json`` and the
    ``brain_graphrag_stats`` MCP tool.
    """
    return {
        "counts_by_type": dict(stats.counts_by_type),
        "total_entities": stats.total_entities,
        "total_relationships": stats.total_relationships,
        "total_communities": stats.total_communities,
        "top_entities": [entity_summaries_json(e) for e in stats.top_entities],
    }


def graph_stats_table(stats: "GraphStats") -> Table:
    """Render a graph overview as a Rich table (admin view; ``brain graphrag stats``).

    Columns: Metric / Value. Rows: total entities, total relationships,
    total communities, then one row per entity type (sorted alphabetically).
    The top-entities slice is rendered separately by the CLI via
    :func:`entity_summaries_table`.
    """
    table = Table(title="Graph Statistics")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Total entities", str(stats.total_entities))
    table.add_row("Total relationships", str(stats.total_relationships))
    table.add_row("Total communities", str(stats.total_communities))
    for entity_type in sorted(stats.counts_by_type):
        table.add_row(f"  {entity_type}", str(stats.counts_by_type[entity_type]))
    return table
