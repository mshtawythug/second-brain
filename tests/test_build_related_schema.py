"""Phase F.B — JSON schema contract tests for Related-docs.

Plan: ``docs/plans/2026-05-06-related-docs-rebuild.md`` (Tests / JSON
schema tests section).

The emitted ``static/related/<slug>.json`` payload is consumed by the
TypeScript frontend (``quartz_overrides/quartz/components/scripts/relatedDocs.inline.ts``)
which expects each entry to have ``slug``, ``title``, ``score``,
``source``, and ``snippet``. These tests pin that shape so a future
regression on the Python side is caught before it ships to Quartz.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import psycopg

from brain.queries import sync_chunk_search_metadata
from brain.wiki.build_related import (
    DEFAULT_RELATED_LIMIT,
    regenerate_related_json,
)

VECTOR_DIM = 4096

REQUIRED_KEYS = {"slug", "title", "score", "source", "snippet"}


def _vector(*components: float) -> str:
    values = [0.0] * VECTOR_DIM
    for index, value in enumerate(components):
        if index >= VECTOR_DIM:
            break
        values[index] = value
    return "[" + ",".join(str(v) for v in values) + "]"


def _insert(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    vault_path: str,
    chunk_contents: list[str],
    chunk_vectors: list[str],
    source_kind: str | None = None,
) -> str:
    source_id: str | None = None
    if source_kind is not None:
        row = conn.execute(
            "INSERT INTO sources (kind, external_id, metadata) "
            "VALUES (%s, %s, '{}'::jsonb) RETURNING id::text",
            (source_kind, f"{source_kind}-{title}-{vault_path}"),
        ).fetchone()
        assert row is not None
        source_id = str(row[0])
    row = conn.execute(
        """
        INSERT INTO documents
          (source_id, title, content, content_hash, content_type, vault_path,
           draft, kind)
        VALUES (%s, %s, %s, %s, 'note', %s, FALSE, %s)
        RETURNING id::text
        """,
        (
            source_id,
            title,
            "\n".join(chunk_contents),
            f"hash-{title}-{vault_path}",
            vault_path,
            "ingested" if source_id is not None else "vault",
        ),
    ).fetchone()
    assert row is not None
    doc_id = str(row[0])
    for index, content in enumerate(chunk_contents):
        conn.execute(
            "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
            "VALUES (%s::uuid, %s, %s, %s::vector)",
            (doc_id, index, content, chunk_vectors[index]),
        )
    sync_chunk_search_metadata(conn, doc_id)
    return doc_id


def _seed_corpus(conn: psycopg.Connection[Any]) -> None:
    src_vec = _vector(1.0, 0.0)
    near_vec = _vector(0.99, 0.05)
    _insert(
        conn,
        title="SCHEMATEST alpha",
        vault_path="alpha.md",
        chunk_contents=["SHAREDPHRASE alpha body one."],
        chunk_vectors=[src_vec],
    )
    _insert(
        conn,
        title="SCHEMATEST beta",
        vault_path="_ingested/gmail/beta.md",
        chunk_contents=["SHAREDPHRASE beta body two."],
        chunk_vectors=[near_vec],
        source_kind="gmail",
    )
    _insert(
        conn,
        title="SCHEMATEST gamma",
        vault_path="_ingested/krisp/gamma.md",
        chunk_contents=["SHAREDPHRASE gamma body three."],
        chunk_vectors=[near_vec],
        source_kind="krisp",
    )


def _all_entries(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for json_path in root.rglob("*.json"):
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert isinstance(payload, list), f"{json_path} is not a JSON array"
        for entry in payload:
            entries.append(entry)
    return entries


def test_emitted_json_matches_relateddocs_inline_contract(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Every entry has exactly the five required keys with correct types."""
    _seed_corpus(test_db)
    regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )

    entries = _all_entries(tmp_path / "static" / "related")
    assert entries, "expected at least one related-doc entry"
    for entry in entries:
        assert REQUIRED_KEYS.issubset(entry.keys()), entry
        assert isinstance(entry["slug"], str)
        assert isinstance(entry["title"], str)
        assert isinstance(entry["score"], float)
        assert isinstance(entry["source"], str)
        assert isinstance(entry["snippet"], str)
        assert entry["slug"], "slug must be non-empty"
        assert entry["title"], "title must be non-empty"


def test_score_is_finite_in_unit_interval(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """All scores are finite floats in [0, 1] (RRF max << 1)."""
    _seed_corpus(test_db)
    regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )

    entries = _all_entries(tmp_path / "static" / "related")
    for entry in entries:
        score = entry["score"]
        assert isinstance(score, float)
        assert math.isfinite(score)
        assert 0.0 <= score <= 1.0


def test_emitted_json_source_field_uses_known_kinds(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Source kind values come from sources.kind (gmail / krisp / vault…)."""
    _seed_corpus(test_db)
    regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )

    entries = _all_entries(tmp_path / "static" / "related")
    sources_seen = {entry["source"] for entry in entries}
    # The seed corpus includes a vault doc, a gmail doc, and a krisp doc.
    # Each appears as a neighbor of the others, so every kind should
    # surface in the union over all emitted entries.
    assert {"gmail", "krisp", "vault"}.issubset(sources_seen), sources_seen


def test_no_extra_keys_in_payload(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Emitted entries have exactly the five contract keys, no extras.

    A leaked internal field (e.g. ``document_id``) would expose a UUID
    to the public Quartz JSON. Pin the contract.
    """
    _seed_corpus(test_db)
    regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )

    entries = _all_entries(tmp_path / "static" / "related")
    assert entries
    for entry in entries:
        assert set(entry.keys()) == REQUIRED_KEYS, entry
