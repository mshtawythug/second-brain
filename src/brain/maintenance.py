"""brain-rebuild orchestrator: a full-corpus rebuild chaining every derived-layer stage."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import typer

from .config import Config
from .db import connect
from .errors import BrainError


@dataclass(frozen=True)
class Step:
    """One subprocess invocation. ``fatal=False`` warns-and-continues on failure."""

    argv: tuple[str, ...]
    fatal: bool = True


@dataclass(frozen=True)
class Stage:
    """A named, selectable unit of the rebuild composed of one or more steps."""

    stage_id: str
    description: str
    steps: tuple[Step, ...]


ALL_STAGE_IDS: tuple[str, ...] = (
    "embeddings", "summaries", "search", "graph", "graph-weights", "communities",
    "connect", "mirror",
)


def brain_argv(*args: str) -> tuple[str, ...]:
    """Build the argv for a ``brain`` sub-command run as a child process.

    Invokes the CLI as ``<sys.executable> -m brain ...`` rather than the bare
    ``brain`` console script. The console script is only reachable when its
    install directory is on ``PATH``, which is *not* guaranteed: CI installs
    into a repo-local ``.venv`` and calls ``.venv/bin/pytest`` without ever
    activating it, so a bare ``brain`` raises ``FileNotFoundError``. Worse than
    failing, on a machine that *does* have some other ``brain`` on ``PATH``
    (e.g. a pipx/uv-tool install) the bare name silently resolves to a
    different, possibly stale installation instead of the one running this
    orchestrator.

    ``-m brain`` is the convention already used by :mod:`brain.setup` and is the
    documented purpose of :mod:`brain.__main__`; it always targets the
    interpreter running this process, needs no ``PATH`` entry, and keeps the
    child on the same code as the parent.
    """
    return (sys.executable, "-m", "brain", *args)


def build_stages(*, vault_path: Path) -> list[Stage]:
    """Construct the canonical stage list in dependency order.

    Spec §9.4 A10: the old ``wiki`` stage was split at R2 (phase 8). Its three
    vault steps are ``mirror``, which runs by default: ``prune-orphans`` is a
    deleter that lives or dies with the People Hub emitter, so its argv and
    ``fatal=False`` are unchanged. The two Quartz steps went to an opt-in
    ``quartz`` stage at R2 and were deleted with the wiki at R3 (phase 9).
    """
    mirror_steps = (
        Step(brain_argv("vault", "export", "--to", str(vault_path))),
        Step(brain_argv("vault", "sync-summaries", "--vault", str(vault_path)), fatal=False),
        Step(
            brain_argv(
                "vault", "prune-orphans", "--apply", "--include-stale",
                "--vault", str(vault_path),
            ),
            fatal=False,
        ),
    )
    return [
        Stage(
            "embeddings",
            "backfill NULL embeddings + finalize HNSW",
            (Step(brain_argv("reembed")),),
        ),
        Stage(
            "summaries",
            "LLM summary backfill (NULL-summary docs)",
            (Step(brain_argv("enrich", "--backfill")),),
        ),
        Stage(
            "search",
            "denorm title/tags + recompute search_extras",
            (Step(brain_argv("backfill", "search")),),
        ),
        Stage(
            "graph",
            "entities + CO_OCCURS edges (per-doc watermark)",
            (Step(brain_argv("graphrag", "build", "--backfill")),),
        ),
        Stage(
            "graph-weights",
            "recompute all edge weights from contributions",
            (Step(brain_argv("graphrag", "refresh")),),
        ),
        Stage(
            "communities",
            "Louvain communities + summaries",
            (Step(brain_argv("graphrag", "communities", "refresh")),),
        ),
        Stage(
            "connect",
            "recompute proactive auto-link suggestions",
            # Non-fatal: a connect-refresh failure (e.g. graph unbuilt) must not
            # abort the rebuild — suggestions are an enhancement.
            (Step(brain_argv("connect", "refresh"), fatal=False),),
        ),
        Stage("mirror", "vault mirror: export + sync-summaries + prune-orphans", mirror_steps),
    ]


class SelectionError(BrainError):
    """Invalid --only/--skip combination or unknown stage id."""


def select_stages(
    stages: Sequence[Stage],
    *,
    only: Sequence[str] | None,
    skip: Sequence[str] | None,
) -> list[Stage]:
    """Select stages by --only/--skip, preserving registry order."""
    if only and skip:
        raise SelectionError("--only and --skip are mutually exclusive")
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


# Matches the `ingest` / `ingest-<variant>` subcommand token that follows the
# `brain` executable.  Using argv tokenization (not a substring scan) so that
# `brain search 'brain ingest foo'` is not a false-positive.
_INGEST_SUBCOMMAND_RE = re.compile(r"^ingest(-[a-z]+)?$")


def snapshot_processes() -> list[str]:
    """Return one command-line string per running process (best-effort).

    Public because it is shared: ``ingest_in_flight`` here and the vault-watcher
    check in :mod:`brain.doctor_watcher` read the same ``ps`` snapshot.
    """
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


def _is_brain_ingest(cmd: str) -> bool:
    """True iff ``cmd`` invokes ``brain ingest*`` (not a watcher or other sub-command).

    Finds the first token whose basename (part after the last ``/``) is exactly
    ``brain`` — the executable — and checks whether the immediately following
    token is ``ingest`` or ``ingest-<variant>``.  Returning at the first match
    prevents later bare ``brain ingest`` tokens (positional args to a different
    sub-command) from triggering a false-positive.
    """
    tokens = cmd.split()
    for i, tok in enumerate(tokens):
        if tok.rsplit("/", 1)[-1] == "brain":
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            return bool(_INGEST_SUBCOMMAND_RE.match(nxt))
    return False


def ingest_in_flight(processes: Sequence[str] | None = None) -> bool:
    """True iff a ``brain ingest*`` process is running. Watchers are not matched."""
    procs = processes if processes is not None else snapshot_processes()
    return any(_is_brain_ingest(p) for p in procs)


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


class StageFailed(BrainError):
    """A fatal step in a stage exited non-zero (fail-fast trigger)."""

    def __init__(self, stage_id: str, exit_code: int) -> None:
        super().__init__(f"stage {stage_id!r} failed (exit {exit_code})")
        self.stage_id = stage_id
        self.exit_code = exit_code


def _default_runner(argv: Sequence[str]) -> int:
    """Run a subprocess, inheriting this process's environment, and return its exit code.

    (A per-step ``env`` overlay existed only for the Quartz ``build_swap`` step's
    ``BRAIN_WIKI_RELOAD=1``; it went with that step at R3, wiki-to-ui phase 9.)
    """
    proc = subprocess.run(list(argv), check=False)  # noqa: S603
    return proc.returncode


def run_stages(
    stages: Sequence[Stage],
    *,
    runner: Callable[..., int] = _default_runner,
) -> None:
    """Run selected stages in order; fail-fast on the first fatal non-zero step."""
    for stage in stages:
        typer.echo(f"▶ {stage.stage_id}: {stage.description}")
        for step in stage.steps:
            code = runner(step.argv)
            if code != 0:
                if step.fatal:
                    raise StageFailed(stage.stage_id, code)
                typer.secho(
                    f"  warn: {' '.join(step.argv)} exited {code} (non-fatal, continuing)",
                    fg="yellow",
                )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse brain-rebuild command-line arguments."""
    p = argparse.ArgumentParser(
        prog="brain-rebuild",
        description=(
            "Full-corpus rebuild: embeddings → summaries → search → graph → weights "
            "→ communities → connect → mirror."
        ),
    )
    p.add_argument(
        "--wiki-only",
        action="store_true",
        help="Removed; exits 2. Use --only mirror for the vault mirror.",
    )
    p.add_argument("--only", help="Comma-separated stage ids to run.")
    p.add_argument("--skip", help="Comma-separated stage ids to skip.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the stage plan and exit; run nothing, take no lock.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Override the in-flight-ingest guard.",
    )
    return p.parse_args(list(argv))


def _csv(value: str | None) -> list[str] | None:
    """Split a comma-separated string into a stripped list, or return None."""
    if not value:
        return None
    return [s.strip() for s in value.split(",") if s.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for brain-rebuild. Returns an exit code (0 success, 1–4 error)."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.wiki_only:
        # R8-3: removed rather than aliased. The old stage did two jobs: the
        # vault mirror (now ``mirror``) and the Quartz build, which was deleted
        # with the wiki at R3. An alias would silently do only half of it.
        typer.secho(
            "error: --wiki-only was removed with the Quartz wiki. Use "
            "--only mirror for the vault mirror.",
            fg="red",
            err=True,
        )
        return 2

    cfg = Config.load()
    stages = build_stages(vault_path=cfg.vault_path)

    try:
        selected = select_stages(
            stages,
            only=_csv(args.only),
            skip=_csv(args.skip),
        )
    except SelectionError as exc:
        typer.secho(f"error: {exc}", fg="red", err=True)
        return 2
    if args.dry_run:
        typer.echo("brain-rebuild plan (dry-run):")
        for i, stage in enumerate(selected, 1):
            typer.echo(f"  {i}. {stage.stage_id} — {stage.description}")
        return 0

    if not args.force and ingest_in_flight():
        typer.secho(
            "error: a `brain ingest` process is in flight; "
            "wait for it to finish or pass --force.",
            fg="red",
            err=True,
        )
        return 3

    try:
        with rebuild_lock(cfg.database_url):
            run_stages(selected)
    except RebuildLockHeld as exc:
        typer.secho(f"error: {exc}", fg="red", err=True)
        return 4
    except StageFailed as exc:
        typer.secho(f"error: {exc}. Remaining stages not run.", fg="red", err=True)
        return 1

    typer.secho("✓ brain-rebuild complete", fg="green")
    return 0
