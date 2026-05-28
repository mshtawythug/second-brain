"""brain-rebuild orchestrator: a full-corpus rebuild chaining every derived-layer stage."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Step:
    """One subprocess invocation. ``fatal=False`` warns-and-continues on failure."""

    argv: tuple[str, ...]
    fatal: bool = True
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Stage:
    """A named, selectable unit of the rebuild composed of one or more steps."""

    stage_id: str
    description: str
    steps: tuple[Step, ...]


ALL_STAGE_IDS: tuple[str, ...] = (
    "embeddings", "summaries", "search", "graph", "graph-weights", "communities", "wiki",
)


def build_stages(*, vault_path: Path, keep: int, clean_cache: bool) -> list[Stage]:
    """Construct the canonical stage list.

    ``clean_cache`` is consumed by the runner (it wipes the parser cache before
    the wiki build); it is threaded through here so callers have one place to
    pass all rebuild knobs.
    """
    py = sys.executable
    wiki_steps = (
        Step(("brain", "vault", "export", "--to", str(vault_path))),
        Step(("brain", "vault", "sync-summaries", "--vault", str(vault_path)), fatal=False),
        Step(
            (
                "brain", "vault", "prune-orphans", "--apply", "--include-stale",
                "--vault", str(vault_path),
            ),
            fatal=False,
        ),
        Step(
            ("brain", "vault", "render", "--overlay", "--no-build", "--vault", str(vault_path)),
            fatal=False,
        ),
        Step(
            (py, "-m", "brain.wiki.build_swap", "--vault", str(vault_path), "--keep", str(keep)),
            env=(("BRAIN_WIKI_RELOAD", "1"),),
        ),
    )
    _ = clean_cache  # consumed by run_stages; threaded through here for caller symmetry
    return [
        Stage(
            "embeddings",
            "backfill NULL embeddings + finalize HNSW",
            (Step(("brain", "reembed")),),
        ),
        Stage(
            "summaries",
            "LLM summary backfill (NULL-summary docs)",
            (Step(("brain", "enrich", "--backfill")),),
        ),
        Stage(
            "search",
            "denorm title/tags + recompute search_extras",
            (Step(("brain", "backfill", "search")),),
        ),
        Stage(
            "graph",
            "entities + CO_OCCURS edges (per-doc watermark)",
            (Step(("brain", "graphrag", "build", "--backfill")),),
        ),
        Stage(
            "graph-weights",
            "recompute all edge weights from contributions",
            (Step(("brain", "graphrag", "refresh")),),
        ),
        Stage(
            "communities",
            "Louvain communities + summaries",
            (Step(("brain", "graphrag", "communities", "refresh")),),
        ),
        Stage("wiki", "vault export/sync/prune/overlay + build_swap", wiki_steps),
    ]
