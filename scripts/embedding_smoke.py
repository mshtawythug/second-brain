"""Smoke test for hybrid search — runs canned queries and prints top-K results.

Useful after ``brain reembed`` or after switching ``BRAIN_EMBEDDER`` backends
to confirm retrieval looks sensible. Reads queries from
``scripts/smoke_queries.txt`` (one per line; blank lines and ``#``-prefixed
comments are ignored), runs each through :func:`brain.search.hybrid_search`,
and prints the top-K results for visual inspection.

Standalone script (not a ``brain`` subcommand) — kept off the user-facing
CLI surface deliberately. Import shape matches any other ``brain.*``
consumer; no special hooks.
"""
import argparse
import json
import sys
from pathlib import Path

import psycopg

from brain.config import Config, ConfigError
from brain.db import connect
from brain.embeddings import make_embedder
from brain.errors import BrainError
from brain.ingest import Embedder
from brain.search import SearchResult, hybrid_search

DEFAULT_QUERIES_FILE = Path(__file__).resolve().parent / "smoke_queries.txt"
SNIPPET_PREVIEW_CHARS = 160


def _load_queries(path: Path) -> list[str]:
    """Return non-blank, non-comment lines from ``path`` in order.

    Lines whose stripped form is empty or starts with ``#`` are dropped;
    everything else is returned with surrounding whitespace stripped.
    """
    queries: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        queries.append(stripped)
    return queries


def _result_to_dict(r: SearchResult) -> dict[str, object]:
    """Serialize a :class:`SearchResult` to a JSON-friendly dict."""
    return {
        "id": r.document_id,
        "title": r.title,
        "source_kind": r.source_kind,
        "score": r.score,
        "content_type": r.content_type,
        "tags": r.tags,
        "snippet": r.snippet,
    }


def _print_human(
    query_idx: int, total: int, query: str, results: list[SearchResult]
) -> None:
    """Print one query's results in the human-readable format."""
    print(f'[query {query_idx}/{total}] "{query}"')
    if not results:
        print("  (no results)")
        print()
        return
    for i, r in enumerate(results, 1):
        source = r.source_kind or "manual"
        print(f"  {i}. ({r.score:.4f}) {r.title} — {source}")
        snippet = r.snippet[:SNIPPET_PREVIEW_CHARS].replace("\n", " ")
        print(f'     "{snippet}"')
    print()


def _print_jsonl(query: str, results: list[SearchResult]) -> None:
    """Emit one JSON object per query — JSON Lines / NDJSON shape."""
    payload = {
        "query": query,
        "results": [_result_to_dict(r) for r in results],
    }
    print(json.dumps(payload, default=str))


def _build_embedder(cfg: Config) -> Embedder:
    """Build the configured embedder. Indirected so tests can substitute a fake."""
    return make_embedder(cfg)


def _run_smoke(queries_file: Path, limit: int, json_output: bool) -> int:
    """Run the smoke test end-to-end. Returns a shell exit code."""
    if not queries_file.is_file():
        print(f"queries file not found: {queries_file}", file=sys.stderr)
        return 1
    queries = _load_queries(queries_file)
    if not queries:
        print(f"no queries in {queries_file}")
        return 0

    try:
        cfg = Config.load()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1

    try:
        embedder = _build_embedder(cfg)
    except (ConfigError, BrainError) as e:
        print(f"embedder error: {e}", file=sys.stderr)
        return 1

    try:
        with connect(cfg.database_url) as conn:
            for idx, query in enumerate(queries, 1):
                results = hybrid_search(
                    conn, embedder=embedder, query=query, limit=limit
                )
                if json_output:
                    _print_jsonl(query, results)
                else:
                    _print_human(idx, len(queries), query, results)
    except psycopg.Error as e:
        print(f"database error: {e}", file=sys.stderr)
        return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse args and run the smoke test. Returns a shell exit code."""
    parser = argparse.ArgumentParser(description="Hybrid search smoke test.")
    parser.add_argument(
        "--queries-file",
        type=Path,
        default=DEFAULT_QUERIES_FILE,
        help="Path to a queries file (one per line; #-comments + blank lines ignored).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Top-K results to return per query (default 5).",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit JSONL (one JSON object per query) instead of human output.",
    )
    args = parser.parse_args(argv)
    return _run_smoke(args.queries_file, args.limit, args.json_output)


if __name__ == "__main__":
    sys.exit(main())
