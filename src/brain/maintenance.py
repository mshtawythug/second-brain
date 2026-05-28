"""brain-rebuild orchestrator: a full-corpus rebuild chaining every derived-layer stage."""
from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .db import connect
from .errors import BrainError


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


class SelectionError(BrainError):
    """Invalid --only/--skip/--wiki-only combination or unknown stage id."""


def select_stages(
    stages: Sequence[Stage],
    *,
    only: Sequence[str] | None,
    skip: Sequence[str] | None,
    wiki_only: bool,
) -> list[Stage]:
    """Select stages by --only/--skip/--wiki-only, preserving registry order."""
    if wiki_only and (only or skip):
        raise SelectionError("--wiki-only cannot be combined with --only/--skip")
    if only and skip:
        raise SelectionError("--only and --skip are mutually exclusive")
    if wiki_only:
        only = ["wiki"]
    valid = {s.stage_id for s in stages}

    def _validate(ids: Sequence[str]) -> None:
        unknown = [i for i in ids if i not in valid]
        if unknown:
            raise SelectionError(
                f"unknown stage id(s): {', '.join(unknown)}; "
                f"valid: {', '.join(s.stage_id for s in stages)}"
            )

    if only:
        _validate(only)
        keep = set(only)
        return [s for s in stages if s.stage_id in keep]
    if skip:
        _validate(skip)
        drop = set(skip)
        return [s for s in stages if s.stage_id not in drop]
    return list(stages)


# Matches `brain ingest` / `brain ingest-dir/-stdin/-gmail` as a token after the
# `brain` executable. Excludes `brain.wiki.build_watcher` and
# `brain vault sync --watch` (neither contains the `brain ingest` token).
_INGEST_RE = re.compile(r"(^|/|\s)brain\s+ingest(-\w+)?(\s|$)")


def _snapshot_processes() -> list[str]:
    """Return one command-line string per running process (best-effort)."""
    try:
        out = subprocess.run(  # noqa: S603,S607
            ["ps", "-axo", "command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def ingest_in_flight(processes: Sequence[str] | None = None) -> bool:
    """True iff a ``brain ingest*`` process is running. Watchers are not matched."""
    procs = processes if processes is not None else _snapshot_processes()
    return any(_INGEST_RE.search(p) for p in procs)


# Fixed namespaced advisory-lock key for brain-rebuild (arbitrary 32-bit constant, "brnr").
_REBUILD_LOCK_KEY = 0x6272_6E72


class RebuildLockHeld(BrainError):
    """Another brain-rebuild run already holds the advisory lock."""


@contextmanager
def rebuild_lock(database_url: str) -> Iterator[None]:
    """Hold a session-level advisory lock for the rebuild.

    Raises :class:`RebuildLockHeld` if another run holds it. Released on exit.
    """
    with connect(database_url) as conn:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(%s)", (_REBUILD_LOCK_KEY,)
        ).fetchone()
        if not (row and row[0]):
            raise RebuildLockHeld("another brain-rebuild run is in progress")
        try:
            yield
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_REBUILD_LOCK_KEY,))
