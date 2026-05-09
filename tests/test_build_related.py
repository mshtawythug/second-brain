"""Tests for semantic-related JSON generation (P5.1)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg

from brain.config import Config
from brain.wiki.build_related import (
    DEFAULT_RELATED_LIMIT,
    RelatedSummary,
    refresh_related,
    regenerate_related_json,
)

VECTOR_DIM = 4096


def _vector(first: float, second: float = 0.0) -> str:
    values = [0.0] * VECTOR_DIM
    values[0] = first
    values[1] = second
    return "[" + ",".join(str(v) for v in values) + "]"


def _source(conn: psycopg.Connection[Any], kind: str, external_id: str) -> str:
    row = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES (%s, %s, '{}'::jsonb) RETURNING id::text",
        (kind, external_id),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _doc(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    vault_path: str,
    vector: str,
    source_id: str | None = None,
    source_kind: str | None = None,
    draft: bool = False,
) -> str:
    if source_id is None and source_kind is not None:
        source_id = _source(conn, source_kind, f"{source_kind}-{title}")
    row = conn.execute(
        """
        INSERT INTO documents
          (source_id, title, content, content_hash, content_type, kind,
           vault_path, draft)
        VALUES
          (%s, %s, %s, %s, 'note', %s, %s, %s)
        RETURNING id::text
        """,
        (
            source_id,
            title,
            f"{title} body with a useful snippet.\nSecond sentence.",
            f"hash-{title}",
            "ingested" if source_id is not None else "vault",
            vault_path,
            draft,
        ),
    ).fetchone()
    assert row is not None
    doc_id = str(row[0])
    conn.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
        "VALUES (%s::uuid, 0, %s, %s::vector)",
        (doc_id, f"{title} chunk text", vector),
    )
    return doc_id


def _read_json(vault: Path, slug: str) -> list[dict[str, Any]]:
    path = vault / "static" / "related" / f"{slug}.json"
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_regenerate_related_json_writes_per_slug_shape_and_order(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    _doc(
        test_db,
        title="Alpha",
        vault_path="alpha.md",
        vector=_vector(1.0, 0.0),
    )
    _doc(
        test_db,
        title="Beta",
        vault_path="_ingested/gmail/beta.md",
        vector=_vector(0.9, 0.1),
        source_kind="gmail",
    )
    _doc(
        test_db,
        title="Gamma",
        vault_path="_ingested/krisp/gamma.md",
        vector=_vector(0.0, 1.0),
        source_kind="krisp",
    )

    summary = regenerate_related_json(test_db, vault_path=tmp_path, k=2, vector_sim_floor=0.0)

    assert summary == RelatedSummary(written=3, skipped=0, pruned=0)
    payload = _read_json(tmp_path, "alpha")
    assert [item["slug"] for item in payload] == [
        "_ingested/gmail/beta",
        "_ingested/krisp/gamma",
    ]
    assert payload[0]["title"] == "Beta"
    assert payload[0]["source"] == "gmail"
    assert isinstance(payload[0]["score"], float)
    # Hybrid scoring (RRF blend over FTS+vector) can produce ties on
    # synthetic fixtures where every doc shares the same generic chunk
    # tokens ("chunk", "text"); rank order is enforced by the slug
    # assertion above and by the deterministic title tie-breaker in
    # ``_neighbors_for_source``. The strict ``>`` from the pure-vector
    # era is loosened to ``>=`` per the plan's guidance for shifted
    # scoring (docs/plans/2026-05-06-related-docs-rebuild.md).
    assert payload[0]["score"] >= payload[1]["score"]
    assert payload[0]["score"] > 0
    assert payload[0]["snippet"]


def test_regenerate_related_json_caps_at_k_and_skips_drafts(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    _doc(test_db, title="Alpha", vault_path="alpha.md", vector=_vector(1.0, 0.0))
    _doc(test_db, title="Beta", vault_path="beta.md", vector=_vector(0.9, 0.1))
    _doc(test_db, title="Gamma", vault_path="gamma.md", vector=_vector(0.8, 0.2))
    _doc(
        test_db,
        title="Hidden Draft",
        vault_path="hidden-draft.md",
        vector=_vector(1.0, 0.0),
        draft=True,
    )

    regenerate_related_json(test_db, vault_path=tmp_path, k=1, vector_sim_floor=0.0)

    alpha = _read_json(tmp_path, "alpha")
    assert [item["slug"] for item in alpha] == ["beta"]
    assert not (tmp_path / "static" / "related" / "hidden-draft.json").exists()
    assert "hidden-draft" not in json.dumps(alpha)


def test_regenerate_related_json_uses_quartz_slug_paths(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    _doc(
        test_db,
        title="Alpha Note",
        vault_path="Alpha Note.md",
        vector=_vector(1.0, 0.0),
    )
    _doc(test_db, title="Beta", vault_path="Beta.md", vector=_vector(0.9, 0.1))

    regenerate_related_json(test_db, vault_path=tmp_path, k=1, vector_sim_floor=0.0)

    assert _read_json(tmp_path, "Alpha-Note")[0]["slug"] == "Beta"
    assert not (tmp_path / "static" / "related" / "Alpha Note.json").exists()


def test_regenerate_related_json_appends_json_without_collapsing_slug_dots(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    _doc(
        test_db,
        title="Alpha Version",
        vault_path="alpha.v1.md",
        vector=_vector(1.0, 0.0),
    )
    _doc(test_db, title="Beta Version", vault_path="beta.v2.md", vector=_vector(0.9, 0.1))

    regenerate_related_json(test_db, vault_path=tmp_path, k=1, vector_sim_floor=0.0)

    payload = _read_json(tmp_path, "alpha.v1")
    assert payload[0]["slug"] == "beta.v2"
    assert not (tmp_path / "static" / "related" / "alpha.json").exists()


def test_regenerate_related_json_is_idempotent_and_prunes_stale_files(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    _doc(test_db, title="Alpha", vault_path="alpha.md", vector=_vector(1.0, 0.0))
    _doc(test_db, title="Beta", vault_path="beta.md", vector=_vector(0.9, 0.1))
    stale = tmp_path / "static" / "related" / "stale.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("[]\n", encoding="utf-8")

    first = regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )
    alpha_path = tmp_path / "static" / "related" / "alpha.json"
    first_bytes = alpha_path.read_bytes()
    second = regenerate_related_json(
        test_db, vault_path=tmp_path, k=DEFAULT_RELATED_LIMIT, vector_sim_floor=0.0
    )

    assert first.pruned == 1
    assert first.written == 2
    assert second == RelatedSummary(written=0, skipped=2, pruned=0)
    assert alpha_path.read_bytes() == first_bytes
    assert not stale.exists()


def test_refresh_related_swallows_db_errors(tmp_path: Path) -> None:
    cfg = Config(
        database_url="postgresql://no:no@localhost:1/no_such_db",
        vault_path=tmp_path,
    )

    summary = refresh_related(cfg)

    assert summary.written == 0
    assert summary.skipped == 0
    assert summary.pruned == 0
    assert summary.errors


def test_build_and_swap_calls_refresh_related(
    monkeypatch, tmp_path: Path
) -> None:
    import brain.wiki.build_swap as bs

    calls: list[Config] = []

    def fake_refresh_homepage(_cfg: Config) -> tuple[bool, bool]:
        return (False, False)

    def fake_refresh_related(cfg: Config) -> RelatedSummary:
        calls.append(cfg)
        return RelatedSummary(written=1, skipped=0, pruned=0)

    def fake_run_build(*args: Any, **kwargs: Any) -> None:
        build_dir = kwargs["build_dir"]
        build_dir.mkdir(parents=True)
        (build_dir / "index.html").write_text("", encoding="utf-8")

    monkeypatch.setenv("DATABASE_URL", "postgresql://x:x@localhost:5432/x")
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    monkeypatch.setattr(
        "brain.wiki.build_homepage.refresh_homepage", fake_refresh_homepage
    )
    monkeypatch.setattr(
        "brain.wiki.build_related.refresh_related", fake_refresh_related
    )
    monkeypatch.setattr(bs, "_run_build", fake_run_build)

    quartz = vault / ".quartz"
    quartz.mkdir()
    (quartz / "quartz.config.ts").write_text("", encoding="utf-8")
    # bootstrap-cli.mjs must exist for _check_workspace to pass (Task 5: node-direct).
    (quartz / "quartz").mkdir()
    (quartz / "quartz" / "bootstrap-cli.mjs").write_text("// stub\n")
    # Pass node_path explicitly so the test doesn't depend on node being on PATH.
    # _run_build is already patched above; node_path just bypasses shutil.which().
    bs.build_and_swap(vault, quartz_dir=quartz, node_path="node")

    assert len(calls) == 1
    assert calls[0].vault_path == vault.resolve()
