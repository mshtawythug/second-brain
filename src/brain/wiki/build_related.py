"""Generate precomputed semantic-related JSON files for the Quartz wiki.

P5.1 of the Wiki UX Overhaul. For each browseable, non-draft document with
chunk embeddings, write ``<vault>/static/related/<slug>.json`` containing the
top-K nearest neighboring documents by cosine similarity over averaged chunk
embeddings. Quartz's stock Static emitter copies the vault ``static/`` tree
into the build output, making the files fetchable as
``/static/related/<slug>.json``.
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
from ..vault._atomic import atomic_write_text

DEFAULT_RELATED_LIMIT = 10
SNIPPET_LENGTH = 240

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

    Failures are logged and returned in ``summary.errors`` rather than raised:
    related docs are a wiki enhancement and must not block the build.
    """
    try:
        with connect(cfg.database_url) as conn:
            return regenerate_related_json(conn, vault_path=cfg.vault_path, k=k)
    except (OSError, psycopg.Error) as exc:
        _logger.warning("wiki related docs: refresh failed: %s", exc)
        return RelatedSummary(errors=[str(exc)])


def regenerate_related_json(
    conn: psycopg.Connection[Any],
    *,
    vault_path: Path,
    k: int = DEFAULT_RELATED_LIMIT,
) -> RelatedSummary:
    """Write ``static/related/<slug>.json`` files for eligible documents."""
    if k < 1:
        raise ValueError("k must be >= 1")

    grouped: dict[str, list[RelatedEntry]] = defaultdict(list)
    source_slugs: set[str] = set()
    for row in _iter_related_rows(conn, k=k):
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


def _iter_related_rows(conn: psycopg.Connection[Any], *, k: int) -> list[tuple[Any, ...]]:
    """Return source/neighbor rows from pgvector centroid ranking."""
    return list(
        conn.execute(
            """
            WITH doc_embeddings AS MATERIALIZED (
              SELECT
                d.id::text AS id,
                d.title AS title,
                d.vault_path AS vault_path,
                COALESCE(s.kind, 'vault') AS source,
                LEFT(regexp_replace(d.content, '\\s+', ' ', 'g'), %s) AS snippet,
                avg(c.embedding) AS embedding
              FROM documents d
              JOIN chunks c ON c.document_id = d.id
              LEFT JOIN sources s ON s.id = d.source_id
              WHERE d.draft = FALSE
                AND d.vault_path IS NOT NULL
                AND c.embedding IS NOT NULL
              GROUP BY d.id, d.title, d.vault_path, s.kind, d.content
            )
            SELECT
              src.vault_path AS source_vault_path,
              rel.vault_path AS related_vault_path,
              rel.title AS related_title,
              rel.source AS related_source,
              rel.score AS related_score,
              rel.snippet AS related_snippet
            FROM doc_embeddings src
            LEFT JOIN LATERAL (
              SELECT
                other.vault_path,
                other.title,
                other.source,
                1.0 - (other.embedding <=> src.embedding) AS score,
                other.snippet
              FROM doc_embeddings other
              WHERE other.id <> src.id
              ORDER BY other.embedding <=> src.embedding, other.title, other.id
              LIMIT %s
            ) rel ON TRUE
            ORDER BY src.vault_path, rel.score DESC NULLS LAST, rel.title NULLS LAST
            """,
            (SNIPPET_LENGTH, k),
        )
    )


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
