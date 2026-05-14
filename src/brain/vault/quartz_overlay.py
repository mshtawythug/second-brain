"""Quartz overlay step — copies ``brain/quartz_overrides/`` into a Quartz workspace.

The brain package ships a small set of overrides under ``brain/quartz_overrides/``
that customize how Quartz renders the vault (custom Graph component,
extended contentIndex emitter, derived-fence transformer, layout, scss).
``brain vault render --overlay`` copies these files over the user's
Quartz workspace right before invoking ``npx quartz build``.

Special case — `_upstreamContentIndex.tsx` rename:
    Our overlay's ``contentIndex.ts`` is a thin wrapper that imports
    Quartz's stock emitter via ``./_upstreamContentIndex``. To make
    that import resolve, this module renames the stock
    ``contentIndex.tsx`` shipped by Quartz out of the way before
    copying. The rename is idempotent — on a workspace that has
    already been overlaid, only ``_upstreamContentIndex.tsx`` is
    present and no rename is needed.

The module exposes a pure :func:`plan_overlay` (no filesystem
mutations) plus :func:`apply_overlay` (performs rename + copy). The
two-step shape lets the CLI implement ``--print-overlay`` (plan
without applying) and lets unit tests exercise planning and applying
separately.
"""
import os
import shutil
from dataclasses import dataclass
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Literal

from ..errors import BrainError

# brain: special case — see brain/quartz_overrides/quartz/plugins/emitters/contentIndex.ts
# header for the why. The wrapper imports from `./_upstreamContentIndex`,
# so we rename Quartz's stock emitter out of the way before the copy.
_UPSTREAM_RENAME_FROM = Path("quartz/plugins/emitters/contentIndex.tsx")
_UPSTREAM_RENAME_TO = Path("quartz/plugins/emitters/_upstreamContentIndex.tsx")


RenameState = Literal["needed", "already_applied", "missing_both"]


class OverlayError(BrainError):
    """Raised when the overlay step cannot proceed safely.

    Two cases trigger this: a missing ``brain/quartz_overrides/`` source dir
    (brain package is broken), or an inconsistent Quartz workspace
    where BOTH the upstream ``contentIndex.tsx`` AND the renamed
    ``_upstreamContentIndex.tsx`` are present at the same time. The
    second case we refuse to auto-resolve — picking one would silently
    discard the other, which may be a user customization.
    """


@dataclass(frozen=True)
class OverlayPlan:
    """A computed-but-not-applied overlay snapshot.

    ``pairs`` is the ordered list of (source, destination) absolute
    paths the copy step would write, in deterministic (sorted-by-src)
    order. ``rename`` is the upstream-rename pair if it should fire
    on this workspace, else ``None``. ``rename_state`` distinguishes
    the two ``rename is None`` sub-cases (already applied vs. neither
    file present) so ``--print-overlay`` can surface the difference.
    """

    quartz_dir: Path
    pairs: tuple[tuple[Path, Path], ...]
    rename: tuple[Path, Path] | None
    rename_state: RenameState


def _overlay_source_root() -> Path:
    """Resolve the ``quartz_overrides/`` tree from inside the installed brain package.

    Uses ``importlib.resources.files("brain.quartz_overrides")`` so the path is
    valid in both editable installs (``pip install -e``) and wheel-installed pipx
    environments — importlib.resources handles both cases.

    Raises :class:`OverlayError` if the package resource is not backed by a regular
    filesystem directory (e.g. if the package were installed inside a zip archive,
    which is not expected for brain).
    """
    root = resource_files("brain.quartz_overrides")
    if not isinstance(root, os.PathLike):
        raise OverlayError(
            "brain.quartz_overrides must be installed as a directory, not inside a "
            "zip archive. Re-install brain with 'pip install brain' (not as a zipapp)."
        )
    return Path(root)


def plan_overlay(quartz_dir: Path) -> OverlayPlan:
    """Enumerate every overlay file + figure out the upstream rename.

    Pure planning step — no filesystem mutations. Raises
    :class:`OverlayError` if the brain package's ``quartz_overrides/``
    directory is missing or unreadable, or if the Quartz workspace is in
    an inconsistent state we won't auto-resolve.
    """
    overrides_root = _overlay_source_root()
    quartz_dir_resolved = quartz_dir.resolve()
    if not overrides_root.is_dir():
        raise OverlayError(
            f"overlay source directory not found: {overrides_root}\n"
            f"  Expected the brain package's quartz_overrides/ tree to be installed "
            f"at that path. Try reinstalling brain."
        )

    pairs: list[tuple[Path, Path]] = []
    for src in sorted(overrides_root.rglob("*")):
        if not src.is_file():
            continue
        # Skip macOS metadata + dotfiles defensively; never legitimate
        # overlay content.
        if src.name.startswith("."):
            continue
        # Skip Python package metadata files and bytecode caches — they are
        # not overlay content. __pycache__/ is generated by the Python
        # interpreter when the package is imported and must not be copied.
        if src.suffix in {".py", ".pyc"}:
            continue
        if "__pycache__" in src.parts:
            continue
        # Defense in depth: confirm src resolves inside overrides_root
        # before we honor it (in case a future symlink ever points out).
        try:
            relative = src.resolve().relative_to(overrides_root)
        except ValueError as e:
            raise OverlayError(
                f"overlay source escaped {overrides_root}: {src}"
            ) from e
        dest = quartz_dir_resolved / relative
        pairs.append((src, dest))

    rename, rename_state = _plan_upstream_rename(quartz_dir_resolved)
    return OverlayPlan(
        quartz_dir=quartz_dir_resolved,
        pairs=tuple(pairs),
        rename=rename,
        rename_state=rename_state,
    )


def _plan_upstream_rename(
    quartz_dir: Path,
) -> tuple[tuple[Path, Path] | None, RenameState]:
    """Inspect the workspace and decide if the upstream rename should fire.

    Three states:
      * ``needed`` — only the original ``contentIndex.tsx`` is present;
        rename it out of the way.
      * ``already_applied`` — only ``_upstreamContentIndex.tsx`` is
        present; rename has already happened, no-op.
      * ``missing_both`` — neither is present; workspace is in an
        unexpected state but we don't pre-empt it. The wrapper's
        defensive load-time guard will surface a clear error when the
        build runs.

    Raises :class:`OverlayError` if BOTH files exist at once — that's
    an inconsistent state we refuse to auto-resolve.
    """
    src = quartz_dir / _UPSTREAM_RENAME_FROM
    dest = quartz_dir / _UPSTREAM_RENAME_TO
    src_exists = src.is_file()
    dest_exists = dest.is_file()
    if src_exists and dest_exists:
        raise OverlayError(
            f"both upstream and renamed contentIndex files exist:\n"
            f"  {src}\n"
            f"  {dest}\n"
            f"  Delete whichever is stale and re-run. Keep "
            f"`_upstreamContentIndex.tsx` if you want the brain wrapper; "
            f"keep `contentIndex.tsx` if you want stock Quartz."
        )
    if src_exists:
        return ((src, dest), "needed")
    if dest_exists:
        return (None, "already_applied")
    return (None, "missing_both")


def apply_overlay(plan: OverlayPlan) -> list[tuple[Path, Path]]:
    """Apply an overlay plan: rename upstream (if needed), then copy files.

    Returns the list of (src, dest) pairs actually copied. The rename
    runs first so the wrapper's ``./_upstreamContentIndex`` import
    resolves once the build runs. Existing destinations are
    overwritten via ``shutil.copy2`` — that's the whole point of the
    overlay.

    Re-runnable after a partial failure: the rename step is idempotent
    via the three-state detection in :func:`_plan_upstream_rename`
    (already-applied state is a no-op), and copies always overwrite —
    so a fresh ``plan_overlay`` + ``apply_overlay`` pass converges
    without manual cleanup.

    Raises :class:`OverlayError` if any filesystem operation fails;
    callers (CLI, MCP) need only catch ``OverlayError`` to convert
    every failure mode into a friendly user-facing error.
    """
    if plan.rename is not None:
        src, dest = plan.rename
        try:
            src.rename(dest)
        except OSError as e:
            raise OverlayError(
                f"overlay rename failed: {src} → {dest}: {e}"
            ) from e
    for src, dest in plan.pairs:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            # Shebanged files must be executable. shutil.copy2 preserves
            # mode bits, so an overlay source checked in without +x
            # produces a non-executable workspace copy (and an npx-cache
            # install with the same mode), which makes `npx <bin>` fail
            # with "Permission denied" at the shell layer. Restoring +x
            # here guarantees the destination is runnable regardless of
            # the source-file's tracked mode.
            if _has_shebang(dest):
                dest.chmod(dest.stat().st_mode | 0o111)
        except OSError as e:
            raise OverlayError(
                f"overlay copy failed: {src} → {dest}: {e}"
            ) from e
    return list(plan.pairs)


def _has_shebang(path: Path) -> bool:
    """Return True when ``path`` starts with ``#!`` (Unix shebang)."""
    try:
        with path.open("rb") as f:
            return f.read(2) == b"#!"
    except OSError:
        return False
