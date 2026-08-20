"""Emit precomputed semantic-related JSON files for the Quartz wiki.

Phase F of ``docs/plans/2026-05-06-related-docs-rebuild.md``. For each
browseable, non-draft document with at least one embedded chunk, write
``<vault>/static/related/<slug>.json`` containing the top-K most-related
documents. Quartz's stock Static emitter copies the vault ``static/`` tree
into the build output, making the files fetchable as
``/static/related/<slug>.json``.

The ranking itself — the hybrid FTS + vector RRF signal that decides *which*
documents are related — lives in :mod:`brain.related` so it can be reused
without importing the wiki package. This module is the I/O half: it groups
the ranked rows by source slug, writes each JSON file atomically, and prunes
stale files.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import psycopg

from ..config import Config
from ..db import connect
from ..related import DEFAULT_RELATED_LIMIT, _iter_hybrid_neighbors
from ..vault._atomic import atomic_write_text

__all__ = [
    "DEFAULT_RELATED_LIMIT",
    "RelatedEntry",
    "RelatedSummary",
    "refresh_related",
    "regenerate_related_json",
]

_logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class RelatedSummary:
    """Outcome counts for a related-docs refresh."""

    written: int = 0
    skipped: int = 0
    pruned: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RelatedEntry:
    """One related-doc JSON row."""

    slug: str
    title: str
    score: float
    source: str
    snippet: str

    def to_json(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "score": self.score,
            "source": self.source,
            "snippet": self.snippet,
        }


def refresh_related(
    cfg: Config, *, k: int = DEFAULT_RELATED_LIMIT
) -> RelatedSummary:
    """Refresh related-doc JSON using ``cfg``'s DB and vault path.

    Plumbs ``cfg.vector_sim_floor`` through to the hybrid neighbor query so
    the precompute and runtime ``brain search`` share the same cosine floor
    (single source of truth — see plan ``docs/plans/2026-05-06-related-docs-rebuild.md``,
    "Cosine-floor reuse").

    Connection/query failures are logged and returned in ``summary.errors``
    rather than raised: the related-docs sidebar is one secondary surface, and
    a DB blip here is transient — the next build repairs it with no human
    involved. Aborting the whole build over it would let a five-second
    Postgres restart stop a markdown-only edit from ever reaching the reader.
    Contrast :func:`brain.wiki.build_swap._refresh_pre_build_adornments`,
    where an unloadable ``Config`` DOES abort: that one is a persistent
    deployment misconfiguration affecting every DB-derived surface.

    The log is at ERROR, not WARNING, on purpose. The caller discards this
    summary (the build proceeds either way), so the log line is the ONLY
    signal that the sidebar silently stopped updating — and a WARNING is
    exactly what let the 2026-07-26 wiki staleness run for twelve days
    unnoticed. An empty summary from a healthy but empty DB is NOT an error
    and does not log here: ``written=0`` on a fresh brain is correct.
    """
    try:
        with connect(cfg.database_url) as conn:
            return regenerate_related_json(
                conn,
                vault_path=cfg.vault_path,
                k=k,
                vector_sim_floor=cfg.vector_sim_floor,
            )
    except (OSError, psycopg.Error) as exc:
        _logger.error(
            "wiki related docs: refresh FAILED (%s) — the related-docs sidebar is"
            " now stale and will stay stale until a build succeeds with the DB"
            " reachable. The build itself continues.",
            exc,
        )
        return RelatedSummary(errors=[str(exc)])


def regenerate_related_json(
    conn: psycopg.Connection[Any],
    *,
    vault_path: Path,
    k: int = DEFAULT_RELATED_LIMIT,
    vector_sim_floor: float,
) -> RelatedSummary:
    """Write ``static/related/<slug>.json`` files for eligible documents.

    ``vector_sim_floor`` is required (no default — callers must pass an
    explicit value or wire ``cfg.vector_sim_floor`` through). Mirrors the
    runtime ``brain search`` cosine floor so the precompute can't silently
    diverge from the user-facing ranking.
    """
    if k < 1:
        raise ValueError("k must be >= 1")

    grouped: dict[str, list[RelatedEntry]] = defaultdict(list)
    source_slugs: set[str] = set()
    for row in _iter_hybrid_neighbors(conn, k=k, vector_sim_floor=vector_sim_floor):
        source_vault_path = row[0]
        if not isinstance(source_vault_path, str):
            continue
        source_slug = _slug_from_vault_path(source_vault_path)
        if source_slug is None:
            continue
        source_slugs.add(source_slug)

        related_vault_path = row[1]
        if related_vault_path is None:
            continue
        if not isinstance(related_vault_path, str):
            continue
        related_slug = _slug_from_vault_path(related_vault_path)
        if related_slug is None:
            continue

        title = str(row[2])
        source = str(row[3] or "vault")
        score = round(float(row[4]), 6)
        snippet = str(row[5] or "")
        grouped[source_slug].append(
            RelatedEntry(
                slug=related_slug,
                title=title,
                score=score,
                source=source,
                snippet=snippet,
            )
        )

    related_root = vault_path / "static" / "related"
    written = skipped = 0
    expected_paths: set[Path] = set()

    for slug in sorted(source_slugs):
        target = _target_path_for_slug(related_root, slug)
        if target is None:
            continue
        expected_paths.add(target)
        payload = [entry.to_json() for entry in grouped.get(slug, [])[:k]]
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if target.is_file():
            try:
                if target.read_text(encoding="utf-8") == rendered:
                    skipped += 1
                    continue
            except OSError:
                pass
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, rendered)
        written += 1

    pruned = _prune_stale_related_files(related_root, expected=expected_paths)
    return RelatedSummary(written=written, skipped=skipped, pruned=pruned)


def _slug_from_vault_path(vault_path: str) -> str | None:
    """Convert ``foo/bar.md`` to safe fetch slug ``foo/bar``."""
    normalized = vault_path.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute():
        return None
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    if path.suffix != ".md":
        return None
    slug_path = path.with_suffix("")
    slug_parts = [_quartz_slugify_segment(part) for part in slug_path.parts]
    if slug_parts[-1].endswith("_index"):
        slug_parts[-1] = slug_parts[-1][: -len("_index")] + "index"
    return PurePosixPath(*slug_parts).as_posix()


def _quartz_slugify_segment(segment: str) -> str:
    """Mirror Quartz's slugifyFilePath segment transform for fetch paths."""
    return (
        re.sub(r"\s", "-", segment)
        .replace("&", "-and-")
        .replace("%", "-percent")
        .replace("?", "")
        .replace("#", "")
    )


def _target_path_for_slug(root: Path, slug: str) -> Path | None:
    """Return the JSON target path for ``slug`` if it is safe."""
    path = PurePosixPath(slug)
    if path.is_absolute():
        return None
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return None
    target = root.joinpath(*path.parts)
    return target.with_name(f"{target.name}.json")


def _prune_stale_related_files(root: Path, *, expected: set[Path]) -> int:
    """Remove stale JSON files from prior related-doc generations."""
    if not root.is_dir():
        return 0
    pruned = 0
    for path in root.rglob("*.json"):
        if path in expected:
            continue
        try:
            path.unlink()
        except OSError as exc:
            _logger.warning("wiki related docs: failed to prune %s: %s", path, exc)
            continue
        pruned += 1
    return pruned
