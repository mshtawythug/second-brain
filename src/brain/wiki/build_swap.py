"""Atomic blue/green Quartz build + symlink swap.

Each call to :func:`build_and_swap` runs
``node <quartz_dir>/quartz/bootstrap-cli.mjs build`` into a fresh
``<quartz_dir>/builds/<ts>-<rand>/`` directory and then atomically retargets
the ``<quartz_dir>/current`` symlink at the new build via a temp-symlink +
``rename(2)`` dance.  Caddy serves ``current/`` directly, so readers only
ever see a fully-written tree — even mid-build, the previous build remains
the one on the wire.

The local Quartz workspace (``<quartz_dir>/quartz/bootstrap-cli.mjs``) is
invoked directly via ``node`` rather than ``npx quartz`` to eliminate the
~100 s of npm-exec/package-resolution overhead that ``npx`` can introduce
before Quartz's own build timer starts.  If ``bootstrap-cli.mjs`` is absent
or ``node`` is not on PATH, :func:`build_and_swap` hard-fails with a clear
repair-path message — there is **no automatic npx fallback**.

Quartz 4.5.x (pinned in ``package.json``) reliably supports ``--output``,
so ``_run_build`` uses that flag directly without any version probing.

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
import logging
import secrets
import shutil
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover — type-only import to avoid runtime cycle
    from ..config import Config

from .errors import BrainWikiBuildError, BrainWikiError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuildResult:
    """Summary of a single :func:`build_and_swap` invocation.

    ``build_dir`` is the absolute path to the new build's tree; ``build_id``
    is its basename and the value written into ``build_dir/.build-id`` for
    the reload-poller to read. ``elapsed_seconds`` covers the whole call
    (build + swap + GC). ``pruned`` lists every build directory deleted by
    garbage collection — empty on early runs, non-empty once we exceed
    ``keep``. ``method`` is always ``"output-flag"`` (Quartz 4.5.x is
    pinned and reliably supports ``--output``); it is preserved on the
    result for observability and test assertions.
    """

    build_dir: Path
    build_id: str
    elapsed_seconds: float
    pruned: list[Path]
    method: Literal["output-flag"]


def build_and_swap(
    vault: Path,
    *,
    quartz_dir: Path | None = None,
    keep: int = 3,
    node_path: str | None = None,
    timeout_seconds: float = 600.0,
    env: dict[str, str] | None = None,
    refresh_related_inline: bool = True,
    npx_path: str | None = None,  # Deprecated and ignored. Kept for API compatibility.
) -> BuildResult:
    """Build the vault into a fresh dir and atomically retarget ``current``.

    ``vault`` is the absolute path to the markdown root; Quartz is invoked
    with ``--directory <vault>``. ``quartz_dir`` defaults to
    ``<vault>/.quartz`` — that's where ``package.json`` and
    ``quartz.config.ts`` must live (the dir created by ``npx quartz create``).

    ``keep`` controls how many old build directories survive the post-swap
    garbage-collect step (separately from the active ``current`` target,
    which is *always* preserved — see :func:`_garbage_collect`).

    ``node_path`` is the Node.js binary to invoke for the build subprocess.
    When ``None`` (the default), it is resolved via
    :func:`shutil.which`\\ ``("node")`` at call time.  If ``node`` is not on
    PATH a :class:`BrainWikiBuildError` is raised with a clear install hint.
    Tests may pass an explicit path to a stub node script.

    ``npx_path`` is **deprecated and ignored** — accepted for backwards API
    compatibility only.  Passing any non-``None`` value emits a
    :class:`DeprecationWarning`.  The build subprocess now invokes ``node``
    directly via the pinned local Quartz workspace
    (``<quartz_dir>/quartz/bootstrap-cli.mjs``) to eliminate ~100 s of
    npm-exec overhead from the watcher hot path.

    ``timeout_seconds`` is a hard wall-clock ceiling on the build
    subprocess. The default (10 min) accommodates large vaults with
    expensive Quartz transformers without ever leaving a runaway build
    holding the process forever.

    ``env`` is an optional extra-env dict merged into ``os.environ`` for
    the build subprocess — used by tests to opt the stub script into
    different output modes via env vars; production passes ``None``.

    Raises :class:`BrainWikiError` if the workspace is missing or
    ``bootstrap-cli.mjs`` is absent (with a repair-path hint); raises
    :class:`BrainWikiBuildError` if ``node`` is not on PATH or if the build
    subprocess fails.  Partial output (a half-written ``build_dir`` left
    behind by a failing build) is cleaned up before the exception propagates
    so retries land on a clean slate.
    """
    if npx_path is not None:
        warnings.warn(
            "npx_path is deprecated and ignored; build now invokes node directly",
            DeprecationWarning,
            stacklevel=2,
        )

    started = time.monotonic()
    workspace = quartz_dir if quartz_dir is not None else vault / ".quartz"
    _check_workspace(workspace)  # Also asserts bootstrap-cli.mjs is present.

    # Resolve the node binary before any other work — hard-fail loud rather
    # than falling back to npx (which adds ~100 s of variance per Codex
    # measurement).
    resolved_node: str
    if node_path is not None:
        resolved_node = node_path
    else:
        found = shutil.which("node")
        if found is None:
            raise BrainWikiBuildError(
                "node binary not found on PATH; install Node.js"
                " (Homebrew: `brew install node`;"
                " Linux: nodejs.org or your distro's package manager)"
            )
        resolved_node = found

    _refresh_pre_build_adornments(vault, refresh_related_inline=refresh_related_inline)

    # Quartz 4.5.x is pinned in package.json and reliably supports --output.
    # No version probe needed — always use the output-flag path.
    method: Literal["output-flag"] = "output-flag"

    builds_root = workspace / "builds"
    builds_root.mkdir(parents=True, exist_ok=True)

    build_id = _generate_build_id()
    build_dir = builds_root / build_id

    try:
        _run_build(
            node_path=resolved_node,
            workspace=workspace,
            vault=vault,
            build_dir=build_dir,
            build_id=build_id,
            timeout_seconds=timeout_seconds,
            env=env,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        # Clean up any partial output before re-raising so the next attempt
        # doesn't trip over a half-written tree.
        if build_dir.exists():
            shutil.rmtree(build_dir, ignore_errors=True)
        # Invalidate fastpath artifacts written by the Quartz subprocess before
        # it failed.  writeFastpathArtifacts() in build.ts fires after
        # emitContent() and is wrapped in try/catch, so it CAN succeed even
        # when the overall build exits non-zero.  If those artifacts are left on
        # disk, classify_edit will read fingerprints from the failed (never-
        # swapped) build and may route the next edit to build-partial, which
        # would emit HTML based on a stale/inconsistent contentmap.  Deleting
        # manifest.json forces classify_edit to return NON_TRIVIAL, so the next
        # watcher-triggered build starts fresh and writes consistent artifacts.
        # Best-effort: _invalidate_fastpath_manifest never raises.
        _invalidate_fastpath_manifest(workspace)
        if isinstance(exc, subprocess.CalledProcessError):
            raise BrainWikiBuildError(
                f"quartz build failed (exit {exc.returncode}) for vault {vault}"
            ) from exc
        if isinstance(exc, subprocess.TimeoutExpired):
            raise BrainWikiBuildError(
                f"quartz build exceeded {timeout_seconds}s for vault {vault}"
            ) from exc
        raise BrainWikiBuildError(
            f"quartz build raised {type(exc).__name__} for vault {vault}: {exc}"
        ) from exc

    # The two steps below — writing .build-id into the new build dir and
    # atomically retargeting the ``current`` symlink — are both "post-build,
    # pre-committed" operations: the subprocess already wrote fastpath
    # artifacts (manifest.json + contentmap.json) tagged with ``build_id``
    # to fastpath_dir.  If EITHER step raises, ``current`` still points at
    # the previous build while the manifest carries fingerprints from the new
    # (uncommitted) one.  We must invalidate the stale manifest on ANY
    # OSError here — not only on the swap itself — so the classifier routes
    # the next edit to a full build rather than a partial one on stale data.
    try:
        # Mark the build with its id. The reload-poller reads this file via
        # /.build-id (Caddy serves the build root directly).
        (build_dir / ".build-id").write_text(f"{build_id}\n", encoding="utf-8")
        _atomic_swap(workspace, build_id)
    except OSError as exc:
        # Deleting manifest.json forces classify_edit to return NON_TRIVIAL on
        # the next edit, routing to a full build that writes a fresh + consistent
        # manifest once the commit succeeds.
        _invalidate_fastpath_manifest(workspace)
        raise BrainWikiBuildError(
            f"quartz build swap failed for vault {vault}: {exc}"
        ) from exc

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


def _invalidate_fastpath_manifest(workspace: Path) -> None:
    """Delete ``fastpath_dir/manifest.json`` after a failed symlink swap.

    When :func:`_run_build` succeeds, the Quartz subprocess writes
    ``<workspace>/.cache/fastpath/manifest.json`` (and ``contentmap.json``)
    tagged with the new ``build_id``. If the subsequent :func:`_atomic_swap`
    then fails, ``current`` still points at the *previous* build while
    ``manifest.json`` carries fingerprints from the *new* (unswapped) build.

    Leaving the stale manifest on disk is dangerous: :func:`classify_edit`
    would read the new fingerprints and might route the next edit as
    ``TRIVIAL`` — launching ``build-partial`` against a live build dir whose
    HTML is from an earlier state.  Deleting the manifest file forces
    ``classify_edit`` to return ``NON_TRIVIAL`` (``ManifestError`` → full
    build), so the next watcher-driven build writes a fresh, consistent
    manifest once the swap succeeds.

    This function is **best-effort**: it never raises.  A missing file is
    silently ignored; any other ``OSError`` is logged at WARNING level so the
    operator knows but the caller's exception path is unaffected.
    """
    manifest_path = workspace / ".cache" / "fastpath" / "manifest.json"
    try:
        manifest_path.unlink()
        logger.debug(
            "wiki build: invalidated stale fastpath manifest after swap failure (%s)",
            manifest_path,
        )
    except FileNotFoundError:
        pass  # already gone — nothing to do
    except OSError as exc:
        logger.warning(
            "wiki build: could not invalidate stale fastpath manifest %s: %s",
            manifest_path,
            exc,
        )


def _check_workspace(workspace: Path) -> None:
    """Validate that ``workspace`` looks like a Quartz workspace.

    Performs two checks:

    1. ``quartz.config.ts`` — written by ``npx quartz create``, required by
       our overlay step.  Guards against pointing the builder at an empty dir.

    2. ``quartz/bootstrap-cli.mjs`` — the Node.js entry point that
       :func:`_run_build` invokes directly (instead of ``npx quartz``).  If
       the file is absent the workspace is incomplete — the repair path is
       ``cd <workspace> && npm install``.

    We do *not* validate ``package.json`` or ``node_modules`` beyond the
    bootstrap-cli check; the caller (brain-up cold start) has its own
    friendlier preflight for those.
    """
    if not workspace.is_dir():
        raise BrainWikiError(f"quartz workspace not found at {workspace}")
    if not (workspace / "quartz.config.ts").is_file():
        raise BrainWikiError(
            f"quartz workspace at {workspace} is missing quartz.config.ts"
        )
    bootstrap = workspace / "quartz" / "bootstrap-cli.mjs"
    if not bootstrap.is_file():
        raise BrainWikiBuildError(
            f"Quartz bootstrap CLI not found at {bootstrap};"
            f' run `cd "{workspace}" && npm install` to repair the workspace'
        )


def _generate_build_id() -> str:
    """Return a fresh ``YYYYMMDD-HHMMSS-<6 hex>`` id.

    Timestamp + 24 bits of randomness keeps ids monotonic-by-time (so
    sort-by-name in ``ls`` lines up with sort-by-mtime) but still
    collision-free if two builds race within the same second.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def _run_build(
    *,
    node_path: str,
    workspace: Path,
    vault: Path,
    build_dir: Path,
    build_id: str,
    timeout_seconds: float,
    env: dict[str, str] | None,
) -> None:
    """Invoke ``node <workspace>/quartz/bootstrap-cli.mjs build --output <build_dir>``.

    Implementation is split off :func:`build_and_swap` so the parent can
    own the cleanup-on-error semantics in one place — this function just
    runs the subprocess and lets every exception type bubble up untouched.

    ``node_path`` is the resolved Node.js binary (pre-validated by the
    caller); ``workspace / "quartz" / "bootstrap-cli.mjs"`` is the Quartz
    entry point (pre-validated by :func:`_check_workspace`).

    ``build_id`` is injected into the subprocess environment as
    ``QUARTZ_PARENT_BUILD_ID`` so the Quartz overlay can write the
    fastpath manifest (``manifest.json`` + ``contentmap.json``) tagged to
    this build.  Without it the overlay logs "skipping fastpath artifact
    write" and every subsequent :func:`build_partial` call hits an
    envelope mismatch and falls back to a full rebuild.

    Quartz 4.5.x is pinned in ``package.json`` and reliably supports
    ``--output``, so no method probe or fallback is needed.
    """
    import os  # local import — only needed inside the build path

    # Always build a merged env so QUARTZ_PARENT_BUILD_ID reaches the
    # Node subprocess.  Callers that pass env=None (production) still get
    # the ID injected; callers that pass a custom dict (tests) get their
    # overrides merged on top.
    merged_env = dict(os.environ)
    if env is not None:
        merged_env.update(env)
    merged_env["QUARTZ_PARENT_BUILD_ID"] = build_id

    args = [
        node_path,
        str(workspace / "quartz" / "bootstrap-cli.mjs"),
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
        env=merged_env,  # always a dict (never None) — QUARTZ_PARENT_BUILD_ID injected above
    )


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
