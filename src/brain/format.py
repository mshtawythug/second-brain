"""Output formatting (human + JSON)."""
import json
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

from .search import SearchExplanation, SearchResult

if TYPE_CHECKING:
    from .eval.baseline import BaselineDiff
    from .eval.runner import EvalReport

# nDCG@5 delta threshold below which a query row is highlighted red in the
# diff table.  Display-only — the CLI never exits non-zero based on this.
_EVAL_REGRESSION_THRESHOLD: float = -0.05

console = Console()


def emit_json(payload: Any) -> None:
    """Print a JSON-serializable payload as pretty JSON via Rich."""
    console.print_json(json.dumps(payload, default=str))


def search_table(results: list[SearchResult]) -> Table:
    """Render hybrid-search results as a Rich table."""
    table = Table(title="Search results")
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
