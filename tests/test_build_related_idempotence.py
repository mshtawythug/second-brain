"""Phase F.B — idempotency tests for the new hybrid Related-docs signal.

Plan: ``docs/plans/2026-05-06-related-docs-rebuild.md`` (Phase F.B Tests
section).

These tests cover the file-write side of ``regenerate_related_json``:

- Re-running on an unchanged corpus is a no-op (no writes, all skips).
- Garbage in an existing JSON file is overwritten cleanly.
- Files for deleted docs are pruned on the next run.

The hybrid-signal correctness lives in ``test_build_related_signal.py``;
these tests treat the score values as opaque — we only assert *file-level*
behavior. That separation lets the idempotence guarantees survive future
scoring tweaks (cosine floor adjustments, RRF parameter changes, …)
without rewriting these assertions.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.queries import sync_chunk_search_metadata
from brain.wiki.build_related import (
    DEFAULT_RELATED_LIMIT,
    regenerate_related_json,
)

VECTOR_DIM = 4096


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
) -> str:
    row = conn.execute(
        """
        INSERT INTO documents
          (title, content, content_hash, content_type, vault_path, draft, kind)
        VALUES (%s, %s, %s, 'note', %s, FALSE, 'vault')
        RETURNING id::text
        """,
        (
            title,
            "\n".join(chunk_contents),
            f"hash-{title}-{vault_path}",
            vault_path,
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


def _seed_corpus(
    conn: psycopg.Connection[Any],
) -> dict[str, str]:
    """Seed three docs that share a body lexeme so the hybrid signal
    produces non-empty neighbor lists. Returns a ``{vault_path: doc_id}``
    mapping for follow-up mutations.
    """
    src_vec = _vector(1.0, 0.0)
    near_vec = _vector(0.99, 0.05)
    return {
        "alpha.md": _insert(
            conn,
            title="ALPHA",
            vault_path="alpha.md",
            chunk_contents=["SHAREDTERM body content one."],
            chunk_vectors=[src_vec],
        ),
        "beta.md": _insert(
            conn,
            title="BETA",
            vault_path="beta.md",
            chunk_contents=["SHAREDTERM body content two."],
            chunk_vectors=[near_vec],
        ),
        "gamma.md": _insert(
            conn,
            title="GAMMA",
            vault_path="gamma.md",
            chunk_contents=["SHAREDTERM body content three."],
            chunk_vectors=[near_vec],
        ),
    }


def test_regenerate_twice_is_no_op(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Two consecutive regenerations: first writes, second skips everything."""
    _seed_corpus(test_db)

    first = regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )
    second = regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )

    assert first.written == 3
    assert first.skipped == 0
    assert first.errors == []
    assert second.written == 0
    assert second.skipped == 3
    assert second.pruned == 0
    assert second.errors == []


def test_corrupted_json_overwritten(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Garbage in an existing related JSON is overwritten with valid JSON."""
    _seed_corpus(test_db)

    regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )
    alpha_path = tmp_path / "static" / "related" / "alpha.json"
    assert alpha_path.is_file()
    alpha_path.write_text("THIS IS NOT JSON\n", encoding="utf-8")

    second = regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )

    # The corrupted file's bytes differed from the deterministic render,
    # so it gets overwritten — counted in ``written``, not ``skipped``.
    assert second.written >= 1
    payload = json.loads(alpha_path.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    assert all(isinstance(entry, dict) for entry in payload)


def test_stale_json_pruned(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Removing a doc and regenerating prunes its related JSON."""
    docs = _seed_corpus(test_db)

    regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )
    related_root = tmp_path / "static" / "related"
    assert (related_root / "gamma.json").is_file()

    test_db.execute("DELETE FROM chunks WHERE document_id = %s::uuid", (docs["gamma.md"],))
    test_db.execute("DELETE FROM documents WHERE id = %s::uuid", (docs["gamma.md"],))

    second = regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )

    assert second.pruned >= 1
    assert not (related_root / "gamma.json").is_file()


def test_unchanged_corpus_does_not_rewrite_files(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """File mtimes prove the writer respected the skip-on-equal-bytes path."""
    _seed_corpus(test_db)

    regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )
    alpha_path = tmp_path / "static" / "related" / "alpha.json"
    pre_bytes = alpha_path.read_bytes()
    pre_mtime = alpha_path.stat().st_mtime_ns

    regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )

    # No re-write — bytes and mtime preserved.
    assert alpha_path.read_bytes() == pre_bytes
    assert alpha_path.stat().st_mtime_ns == pre_mtime


def test_regenerate_rejects_invalid_k(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """k < 1 is a programmer error — surface with ValueError, not silently."""
    with pytest.raises(ValueError, match="k must be"):
        regenerate_related_json(
            test_db, vault_path=tmp_path, k=0, vector_sim_floor=0.0
        )


def test_regenerate_with_empty_corpus_returns_zero_summary(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Empty corpus → no writes, no skips, no prunes (no static/related dir).

    Exercises the early-return branch in ``_prune_stale_related_files``
    when the related-docs directory doesn't exist on disk.
    """
    summary = regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )

    assert summary.written == 0
    assert summary.skipped == 0
    assert summary.pruned == 0
    assert summary.errors == []
    assert not (tmp_path / "static" / "related").exists()
