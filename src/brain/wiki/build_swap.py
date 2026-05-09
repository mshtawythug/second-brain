"""Atomic blue/green Quartz build + symlink swap.

Each call to :func:`build_and_swap` runs ``npx quartz build`` into a fresh
``<quartz_dir>/builds/<ts>-<rand>/`` directory and then atomically retargets
the ``<quartz_dir>/current`` symlink at the new build via a temp-symlink +
``rename(2)`` dance. Caddy serves ``current/`` directly, so readers only
ever see a fully-written tree — even mid-build, the previous build remains
the one on the wire.

Two build paths are supported:

- **output-flag** — Quartz versions that accept ``--output`` write the site
  straight into the build directory.
- **rename-public** — older Quartz versions only know about ``./public``,
  so we run the build (which writes ``<quartz_dir>/public/``) and then
  rename ``public/`` into the build directory ourselves.

The choice between the two is made by probing ``npx quartz build --help``
once per (npx_path, quartz_dir) pair and caching the result.

After a successful swap, old build directories are garbage-collected: the
N most recent (by mtime) survive plus whichever directory ``current`` now
points at, even if it would otherwise be evicted. This guarantees the
active build is never deleted out from under Caddy.

This module also exposes a one-shot CLI entry point — ``python -m
brain.wiki.build_swap --vault PATH [--quartz-dir PATH] [--keep N]`` —
used by ``bin/brain-up`` (cold-start build) and ``bin/brain-rebuild``
(forced rebuild) so those scripts never have to import Python directly.
Exits 0 on success, 1 on a wrapped :class:`BrainWikiError`, and 2 (via
argparse) on bad arguments.
"""
from __future__ import annotations

import argparse
import dataclasses
import functools
import logging
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover — type-only import to avoid runtime cycle
    from ..config import Config

from .errors import BrainWikiBuildError, BrainWikiError

logger = logging.getLogger(__name__)


# Hard ceiling on the help-probe subprocess. ``npx quartz build --help`` is
# fast (sub-second locally) but the surrounding npx warmup can stall if the
# global Quartz binary isn't cached — 30s is generous without being a hang.
_PROBE_TIMEOUT_S = 30.0

# Build method discriminant returned in :class:`BuildResult` so callers can
# log/test which code path ran.
BuildMethod = Literal["output-flag", "rename-public"]


@dataclass(frozen=True)
class BuildResult:
    """Summary of a single :func:`build_and_swap` invocation.

    ``build_dir`` is the absolute path to the new build's tree; ``build_id``
    is its basename and the value written into ``build_dir/.build-id`` for
    the reload-poller to read. ``elapsed_seconds`` covers the whole call
    (probe + build + swap + GC). ``pruned`` lists every build directory
    deleted by garbage collection — empty on early runs, non-empty once
    we exceed ``keep``. ``method`` records which build path ran so that
    bin/brain-status (and tests) can tell at a glance.
    """

    build_dir: Path
    build_id: str
    elapsed_seconds: float
    pruned: list[Path]
    method: BuildMethod


def build_and_swap(
    vault: Path,
    *,
    quartz_dir: Path | None = None,
    keep: int = 3,
    npx_path: str = "npx",
    timeout_seconds: float = 600.0,
    env: dict[str, str] | None = None,
    refresh_related_inline: bool = True,
) -> BuildResult:
    """Build the vault into a fresh dir and atomically retarget ``current``.

    ``vault`` is the absolute path to the markdown root; Quartz is invoked
    with ``--directory <vault>``. ``quartz_dir`` defaults to
    ``<vault>/.quartz`` — that's where ``package.json`` and
    ``quartz.config.ts`` must live (the dir created by ``npx quartz create``).

    ``keep`` controls how many old build directories survive the post-swap
    garbage-collect step (separately from the active ``current`` target,
    which is *always* preserved — see :func:`_garbage_collect`).

    ``npx_path`` is the executable to invoke. Tests pass a stub script;
    production passes the resolved ``shutil.which("npx")`` (or just
    ``"npx"`` and trusts ``$PATH``).

    ``timeout_seconds`` is a hard wall-clock ceiling on the build
    subprocess. The default (10 min) accommodates large vaults with
    expensive Quartz transformers without ever leaving a runaway build
    holding the process forever.

    ``env`` is an optional extra-env dict merged into ``os.environ`` for
    the build subprocess — used by tests to opt the stub script into
    different output modes via env vars; production passes ``None``.

    Raises :class:`BrainWikiError` if the workspace is missing, or
    :class:`BrainWikiBuildError` wrapping the underlying
    ``CalledProcessError`` / ``TimeoutExpired`` if the build itself fails.
    Partial output (a half-written ``build_dir`` left behind by a failing
    build) is cleaned up before the exception propagates so retries land
    on a clean slate.
    """
    started = time.monotonic()
    workspace = quartz_dir if quartz_dir is not None else vault / ".quartz"
    _check_workspace(workspace)
    _refresh_pre_build_adornments(vault, refresh_related_inline=refresh_related_inline)

    method = _probe_build_method(npx_path, workspace, timeout_seconds=_PROBE_TIMEOUT_S)

    builds_root = workspace / "builds"
    builds_root.mkdir(parents=True, exist_ok=True)

    build_id = _generate_build_id()
    build_dir = builds_root / build_id

    try:
        _run_build(
            method,
            npx_path=npx_path,
            workspace=workspace,
            vault=vault,
            build_dir=build_dir,
            timeout_seconds=timeout_seconds,
            env=env,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        # Clean up any partial output before re-raising so the next attempt
        # doesn't trip over a half-written tree.
        if build_dir.exists():
            shutil.rmtree(build_dir, ignore_errors=True)
        if isinstance(exc, subprocess.CalledProcessError):
            raise BrainWikiBuildError(
                f"npx quartz build failed (exit {exc.returncode}) for vault {vault}"
            ) from exc
        if isinstance(exc, subprocess.TimeoutExpired):
            raise BrainWikiBuildError(
                f"npx quartz build exceeded {timeout_seconds}s for vault {vault}"
            ) from exc
        raise BrainWikiBuildError(
            f"npx quartz build raised {type(exc).__name__} for vault {vault}: {exc}"
        ) from exc

    # Mark the build with its id. The reload-poller reads this file via
    # /.build-id (Caddy serves the build root directly), comparing the
    # value across pages to detect swaps.
    (build_dir / ".build-id").write_text(f"{build_id}\n", encoding="utf-8")

    _atomic_swap(workspace, build_id)

    pruned = _garbage_collect(workspace, keep=keep)

    elapsed = time.monotonic() - started
    return BuildResult(
        build_dir=build_dir,
        build_id=build_id,
        elapsed_seconds=elapsed,
        pruned=pruned,
        method=method,
    )


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------


def _check_workspace(workspace: Path) -> None:
    """Validate that ``workspace`` looks like a Quartz workspace.

    The cheapest reliable signal is ``quartz.config.ts`` — every
    ``npx quartz create`` scaffold writes it, and our overlay step
    refuses to run without it. We don't validate ``package.json`` /
    ``node_modules`` here because the caller (brain-up cold start)
    has its own friendlier preflight; this is just a guard against
    pointing the builder at an empty directory.
    """
    if not workspace.is_dir():
        raise BrainWikiError(f"quartz workspace not found at {workspace}")
    if not (workspace / "quartz.config.ts").is_file():
        raise BrainWikiError(
            f"quartz workspace at {workspace} is missing quartz.config.ts"
        )


def _generate_build_id() -> str:
    """Return a fresh ``YYYYMMDD-HHMMSS-<6 hex>`` id.

    Timestamp + 24 bits of randomness keeps ids monotonic-by-time (so
    sort-by-name in ``ls`` lines up with sort-by-mtime) but still
    collision-free if two builds race within the same second.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


@functools.lru_cache(maxsize=8)
def _cached_probe(npx_path: str, workspace_str: str, timeout_seconds: float) -> BuildMethod:
    """Memoize :func:`_probe_build_method` per (npx, workspace).

    Probing runs an ``npx`` subprocess; doing it on every build adds
    seconds for no real benefit because the Quartz binary on a given
    workspace doesn't switch versions between calls. The cache key
    uses ``str(Path)`` because ``Path`` is unhashable in ``frozen``
    dataclasses on some Python builds — strings are universal.
    """
    workspace = Path(workspace_str)
    args = [npx_path, "quartz", "build", "--help"]
    try:
        completed = subprocess.run(  # noqa: S603 — list-form args, no shell
            args,
            cwd=str(workspace),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        # Probe failure shouldn't be fatal: assume the legacy ``public/``
        # path is available (it's the older shape). The build itself will
        # surface a friendlier error if npx is genuinely missing.
        logger.warning(
            "wiki build: --help probe failed (%s); assuming rename-public method",
            exc,
        )
        return "rename-public"
    haystack = (completed.stdout or "") + (completed.stderr or "")
    if "--output" in haystack:
        return "output-flag"
    return "rename-public"


def _probe_build_method(
    npx_path: str, workspace: Path, *, timeout_seconds: float
) -> BuildMethod:
    """Probe whether the installed Quartz supports ``--output``.

    Thin wrapper around :func:`_cached_probe` so callers don't have to
    stringify the workspace path themselves.
    """
    return _cached_probe(npx_path, str(workspace), timeout_seconds)


def _run_build(
    method: BuildMethod,
    *,
    npx_path: str,
    workspace: Path,
    vault: Path,
    build_dir: Path,
    timeout_seconds: float,
    env: dict[str, str] | None,
) -> None:
    """Invoke ``npx quartz build`` into ``build_dir`` via the chosen method.

    Implementation is split off :func:`build_and_swap` so the parent can
    own the cleanup-on-error semantics in one place — this function just
    runs the subprocess and (for the legacy method) renames ``public/``
    into the build dir, and lets every exception type bubble up
    untouched.
    """
    import os  # local import — only needed inside the build path

    merged_env: dict[str, str] | None = None
    if env is not None:
        merged_env = dict(os.environ)
        merged_env.update(env)

    if method == "output-flag":
        args = [
            npx_path,
            "quartz",
            "build",
            "--directory",
            str(vault),
            "--output",
            str(build_dir),
        ]
        subprocess.run(  # noqa: S603 — list-form args, no shell
            args,
            cwd=str(workspace),
            check=True,
            timeout=timeout_seconds,
            env=merged_env,
        )
        return

    # rename-public fallback: build into <workspace>/public/, then rename
    # the directory atomically into the build slot. Both halves share the
    # same filesystem (the Quartz workspace), so ``rename`` is O(1) and
    # atomic on POSIX.
    public_dir = workspace / "public"
    if public_dir.exists():
        shutil.rmtree(public_dir)
    args = [
        npx_path,
        "quartz",
        "build",
        "--directory",
        str(vault),
    ]
    subprocess.run(  # noqa: S603 — list-form args, no shell
        args,
        cwd=str(workspace),
        check=True,
        timeout=timeout_seconds,
        env=merged_env,
    )
    if not public_dir.exists():
        raise BrainWikiBuildError(
            f"quartz build did not produce {public_dir} (rename-public path)"
        )
    public_dir.rename(build_dir)


def _atomic_swap(workspace: Path, build_id: str) -> None:
    """Retarget ``<workspace>/current`` at ``builds/<build_id>`` atomically.

    Uses a temp-symlink + ``Path.replace`` (POSIX ``rename(2)``) so a
    reader that opens ``current/.build-id`` mid-swap either sees the old
    target or the new one — never a missing file. APFS, ext4, and ZFS
    all guarantee rename atomicity for symlinks.

    The relative target (``builds/<id>``) keeps the symlink portable
    if the workspace is moved; absolute targets would need rewriting.
    """
    target = Path("builds") / build_id
    current = workspace / "current"
    tmp = workspace / "current.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target)
    tmp.replace(current)


def _garbage_collect(workspace: Path, *, keep: int) -> list[Path]:
    """Delete build dirs beyond the ``keep`` most-recent + the active one.

    Sorting by mtime descending preserves the most recent ``keep`` even
    if their names sort differently (clock skew / manual touches).
    Whatever the symlink resolves to is *always* spared — without that
    rule a ``keep=1`` config could delete the live build out from under
    Caddy. Returns the list of pruned dirs so the CLI can log them.
    """
    builds_root = workspace / "builds"
    if not builds_root.is_dir():
        return []
    candidates = [p for p in builds_root.iterdir() if p.is_dir()]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    active = _resolve_current_target(workspace)
    keepers: set[Path] = set(candidates[:keep])
    if active is not None:
        keepers.add(active)

    pruned: list[Path] = []
    for p in candidates:
        if p in keepers:
            continue
        try:
            shutil.rmtree(p)
        except OSError as exc:
            # GC is best-effort — a stuck dir (open file handle on
            # Windows, exotic permissions) shouldn't fail the swap. Log
            # and move on; the next run gets another shot at it.
            logger.warning("wiki build: failed to prune %s: %s", p, exc)
            continue
        pruned.append(p)
    return pruned


def _resolve_current_target(workspace: Path) -> Path | None:
    """Return the absolute build dir ``current`` points at, or None.

    None covers two cases: the symlink doesn't exist (first build), or
    it points at a path that's already been deleted (rare race between
    GC and a manual ``rm``). The caller treats both as "no active
    build to spare from GC".
    """
    current = workspace / "current"
    if not current.is_symlink() and not current.exists():
        return None
    try:
        resolved = current.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_dir():
        return None
    return resolved


# ---------------------------------------------------------------------------
# CLI entry point — ``python -m brain.wiki.build_swap``.
# ---------------------------------------------------------------------------


def _replace_vault_path(cfg: Config, vault_path: Path) -> Config:
    """Return a copy of ``cfg`` with ``vault_path`` overridden.

    ``Config`` is frozen, so we use :func:`dataclasses.replace`. Lifted
    into a helper so the call site in :func:`main` reads cleanly and so
    a future Config field addition (which would otherwise break the
    inline ``replace(...)`` call by omission) flags here in one spot.
    """
    return dataclasses.replace(cfg, vault_path=vault_path)


def _refresh_pre_build_adornments(
    vault: Path, *, refresh_related_inline: bool = True
) -> None:
    """Best-effort generated wiki adornment refresh before any Quartz build.

    ``refresh_related_inline=False`` skips ``refresh_related`` (the heavy
    hybrid-search recompute, ~70s on a 1100-doc vault). The build watcher
    passes ``False`` and runs ``refresh_related`` on a background daemon
    thread post-build to keep edit-to-UI latency down. ``bin/brain-rebuild``
    keeps the default ``True`` so manual rebuilds always emit fresh
    related-docs JSON synchronously.
    """
    # P4.7/P5.1 — refresh generated wiki adornments before the build
    # so the new build picks up the freshest recent rail and related-doc
    # JSON. Failures here are logged-and-swallowed by the refresh helpers
    # themselves (DB unreachable, missing fence, …) — these adornments are
    # a courtesy, the build is the customer.
    # Config loading is opt-in: a Config-load failure (no DATABASE_URL on
    # PATH) shouldn't block the build either, so we wrap the import too.
    try:
        from ..config import Config, ConfigError
        from .build_homepage import refresh_homepage
        from .build_related import refresh_related

        try:
            cfg = Config.load()
        except ConfigError as exc:
            logger.warning(
                "wiki pre-build refresh: Config.load failed (%s) — skipping refresh",
                exc,
            )
        else:
            target_vault = vault.expanduser().resolve()
            # Honor explicit build vault overrides: the generated adornments
            # must follow the tree being built, not the env default.
            cfg_for_build = (
                cfg
                if cfg.vault_path == target_vault
                else _replace_vault_path(cfg, target_vault)
            )
            refresh_homepage(cfg_for_build)
            if refresh_related_inline:
                refresh_related(cfg_for_build)
    except Exception as exc:  # noqa: BLE001 — refresh is best-effort
        logger.warning("wiki pre-build refresh: unexpected failure: %s", exc)


def main(argv: list[str] | None = None) -> int:
    """Run a single ``build_and_swap`` from the command line.

    Used by ``bin/brain-up`` (cold-start build, when ``current/`` doesn't
    exist yet) and ``bin/brain-rebuild`` (forced rebuild) so those shell
    scripts can stay shell-only — no Python import needed at the call
    site.

    Mirrors :func:`build_watcher.main` argument shape (``--vault``,
    ``--quartz-dir``, ``--keep``) so the two CLIs feel like siblings.
    Returns 0 on success and 1 on any :class:`BrainWikiError` (build
    failure or workspace problem). Bad arguments exit 2 via argparse.
    """
    parser = argparse.ArgumentParser(
        prog="brain.wiki.build_swap",
        description="Run a single atomic Quartz build + symlink swap.",
    )
    parser.add_argument("--vault", required=True, type=Path, help="Vault root path.")
    parser.add_argument(
        "--quartz-dir",
        type=Path,
        default=None,
        help="Quartz workspace dir (default: <vault>/.quartz).",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=3,
        help="How many old build dirs to retain after the swap (default: 3).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    vault: Path = args.vault.expanduser().resolve()
    quartz_dir: Path | None = (
        args.quartz_dir.expanduser().resolve() if args.quartz_dir is not None else None
    )

    try:
        result = build_and_swap(vault, quartz_dir=quartz_dir, keep=args.keep)
    except BrainWikiError as exc:
        # Wrapped wiki errors — workspace missing, build subprocess
        # exited non-zero, or timed out. Print a one-line message and
        # exit 1 so callers (brain-up, brain-rebuild) can `|| exit 1`.
        sys.stderr.write(f"build failed: {exc}\n")
        return 1

    sys.stdout.write(
        f"{result.build_id} ({result.elapsed_seconds:.2f}s, method={result.method})\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in CLI
    raise SystemExit(main(sys.argv[1:]))
