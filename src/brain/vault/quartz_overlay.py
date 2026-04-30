"""Quartz overlay step — copies `quartz_overrides/` into a Quartz workspace.

The brain repo ships a small set of overrides under ``quartz_overrides/``
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
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..errors import BrainError

# brain: special case — see quartz_overrides/plugins/emitters/contentIndex.ts
# header for the why. The wrapper imports from `./_upstreamContentIndex`,
# so we rename Quartz's stock emitter out of the way before the copy.
_UPSTREAM_RENAME_FROM = Path("quartz/plugins/emitters/contentIndex.tsx")
_UPSTREAM_RENAME_TO = Path("quartz/plugins/emitters/_upstreamContentIndex.tsx")

# Where overlay sources live inside the brain repo.
_OVERLAY_SUBDIR = "quartz_overrides"

# Where overlay files land inside the Quartz workspace. The directory
# structure under ``quartz_overrides/`` mirrors the layout under
# ``<quartz_dir>/quartz/`` — e.g. ``quartz_overrides/components/Graph.tsx``
# → ``<quartz_dir>/quartz/components/Graph.tsx``.
_OVERLAY_DEST_SUBDIR = "quartz"


RenameState = Literal["needed", "already_applied", "missing_both"]


class OverlayError(BrainError):
    """Raised when the overlay step cannot proceed safely.

    Two cases trigger this: a missing ``quartz_overrides/`` source dir
    (brain repo layout is broken), or an inconsistent Quartz workspace
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

    repo_root: Path
    quartz_dir: Path
    pairs: tuple[tuple[Path, Path], ...]
    rename: tuple[Path, Path] | None
    rename_state: RenameState


def repo_root() -> Path:
    """Resolve the brain repo root from this module's location.

    ``src/brain/vault/quartz_overlay.py`` → ``parents[3]`` is the
    repo root. Computed (not user-supplied) so there's no path-
    traversal vector here.
    """
    return Path(__file__).resolve().parents[3]


def plan_overlay(repo_root_path: Path, quartz_dir: Path) -> OverlayPlan:
    """Enumerate every overlay file + figure out the upstream rename.

    Pure planning step — no filesystem mutations. Raises
    :class:`OverlayError` if the brain repo's ``quartz_overrides/``
    directory is missing, or if the Quartz workspace is in an
    inconsistent state we won't auto-resolve.
    """
    repo_root_resolved = repo_root_path.resolve()
    quartz_dir_resolved = quartz_dir.resolve()
    overrides_root = (repo_root_resolved / _OVERLAY_SUBDIR).resolve()
    if not overrides_root.is_dir():
        raise OverlayError(
            f"overlay source directory not found: {overrides_root}\n"
            f"  Expected `{_OVERLAY_SUBDIR}/` to live at the brain repo root."
        )

    pairs: list[tuple[Path, Path]] = []
    for src in sorted(overrides_root.rglob("*")):
        if not src.is_file():
            continue
        # Skip macOS metadata + dotfiles defensively; never legitimate
        # overlay content.
        if src.name.startswith("."):
            continue
        # Defense in depth: confirm src resolves inside overrides_root
        # before we honor it (in case a future symlink ever points out).
        try:
            relative = src.resolve().relative_to(overrides_root)
        except ValueError as e:
            raise OverlayError(
                f"overlay source escaped {overrides_root}: {src}"
            ) from e
        dest = quartz_dir_resolved / _OVERLAY_DEST_SUBDIR / relative
        pairs.append((src, dest))

    rename, rename_state = _plan_upstream_rename(quartz_dir_resolved)
    return OverlayPlan(
        repo_root=repo_root_resolved,
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
    """
    if plan.rename is not None:
        src, dest = plan.rename
        src.rename(dest)
    for src, dest in plan.pairs:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return list(plan.pairs)
