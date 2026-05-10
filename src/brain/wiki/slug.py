"""Python port of Quartz's slugifyFilePath (quartz/util/path.ts).

Provides vault-relative file-path → Quartz slug conversion.  Used by the
edit classifier (T3) and the watcher routing layer (T6).  Keeping slug
helpers in their own module prevents downstream code from importing them
via ``edit_classifier``.

Public surface:
    slugify_file_path(file_path: str) → str
    slugify_source_path(source_path: Path, vault_root: Path) → str
"""
from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Internals — mirror Quartz's sluggify() from quartz/util/path.ts
# ---------------------------------------------------------------------------

# Matches Quartz's getFileExtension: last dot + alphanumeric chars at end.
_EXT_RE: re.Pattern[str] = re.compile(r"\.[A-Za-z0-9]+$")


def _sluggify_segment(segment: str) -> str:
    """Apply Quartz's per-segment sluggify transforms.

    Mirrors the anonymous map function inside ``sluggify()`` in path.ts::

        .replace(/\\s/g, "-")
        .replace(/&/g, "-and-")
        .replace(/%/g, "-percent")
        .replace(/\\?/g, "")
        .replace(/#/g, "")
    """
    segment = re.sub(r"\s", "-", segment)
    segment = segment.replace("&", "-and-")
    segment = segment.replace("%", "-percent")
    segment = segment.replace("?", "")
    segment = segment.replace("#", "")
    return segment


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def slugify_file_path(file_path: str) -> str:
    """Convert a POSIX-style vault-relative file path to a Quartz slug.

    Matches Quartz's ``slugifyFilePath`` from ``quartz/util/path.ts``.
    Strips leading/trailing slashes, removes the ``.md``/``.html`` extension,
    applies per-segment sluggify transforms, and rewrites trailing ``_index``
    to ``index``.

    Args:
        file_path: POSIX-style path relative to the vault root, e.g.
            ``"folder/my note.md"`` or ``"index.md"``.

    Returns:
        Slug string without the ``.md`` extension, e.g. ``"folder/my-note"``.
    """
    # Mirror Quartz's stripSlashes(fp) — strip leading AND trailing slashes.
    fp = file_path.strip("/")

    # Get extension (last .alphanumeric sequence at end of string).
    ext_match = _EXT_RE.search(fp)
    ext: str | None = ext_match.group(0) if ext_match else None

    # Remove extension from path string.
    fp_no_ext = fp[: -len(ext)] if ext else fp

    # For .md / .html / no-extension, the slug carries no extension.
    keep_ext: str = ext if (ext is not None and ext not in (".md", ".html")) else ""

    # Apply sluggify to each segment, then re-join with "/".
    segments = fp_no_ext.split("/")
    slug = "/".join(_sluggify_segment(s) for s in segments)

    # Remove trailing slash (Quartz: .replace(/\/$/, "")).
    slug = slug.rstrip("/")

    # Treat _index as index — mirrors Quartz's endsWith check:
    #   endsWith(slug, "_index")  →  slug === "_index" || slug.endsWith("/_index")
    if slug == "_index" or slug.endswith("/_index"):
        slug = slug[: -len("_index")] + "index"

    return slug + keep_ext


def slugify_source_path(source_path: Path, vault_root: Path) -> str:
    """Compute the Quartz slug for an absolute source path.

    Args:
        source_path: Absolute path to the vault file.
        vault_root: Absolute path to the vault root directory.

    Returns:
        Slug string (POSIX, no leading slash, no ``.md`` extension).

    Raises:
        ValueError: If ``source_path`` is not inside ``vault_root``.
    """
    try:
        rel = source_path.relative_to(vault_root)
    except ValueError as exc:
        raise ValueError(
            f"source_path {source_path!r} is not inside vault_root {vault_root!r}"
        ) from exc
    return slugify_file_path(rel.as_posix())
