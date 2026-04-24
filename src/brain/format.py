"""Output formatting (human + JSON)."""
import json
from typing import Any

from rich.console import Console
from rich.table import Table

from .search import SearchResult

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
