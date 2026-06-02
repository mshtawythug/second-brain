"""Integration tests for ``brain.vault.note_builder.create_vault_note``.

These exercise the standalone helper directly (no Typer CLI), the way the
tacit-knowledge elicitation session loop will call it. A real Postgres test DB
is used; the embedder is the deterministic :class:`FakeEmbedder` fixture.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from brain.config import Config
from brain.errors import VaultNoteSyncError
from brain.vault import create_vault_note, init_vault
from brain.vault.frontmatter import parse_frontmatter


def _init_vault(vault_path: Path) -> None:
    init_vault(vault_path)


def test_create_vault_note_returns_doc_id(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
) -> None:
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)
    cfg = Config.load()

    # Act
    doc_id = create_vault_note(
        test_db,
        cfg=cfg,
        vault_path=vault,
        title="Tacit rule X",
        body="The rule is Y.",
        tags=["tacit"],
        embedder=fake_embedder,
    )

    # Assert
    assert doc_id
    row = test_db.execute(
        "SELECT kind, title FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == "vault"
    assert row[1] == "Tacit rule X"


def test_create_vault_note_writes_provided_body(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
) -> None:
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)
    cfg = Config.load()

    # Act
    doc_id = create_vault_note(
        test_db,
        cfg=cfg,
        vault_path=vault,
        title="Body bearing note",
        body="A distinctive authored sentence.",
        tags=["tacit"],
        embedder=fake_embedder,
    )

    # Assert — body lands on disk and in the indexed DB row.
    target = vault / "body-bearing-note.md"
    assert target.is_file()
    _fields, body = parse_frontmatter(target.read_text())
    assert "A distinctive authored sentence." in body
    row = test_db.execute(
        "SELECT content FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    assert "A distinctive authored sentence." in row[0]


def test_create_vault_note_forces_canonical_frontmatter(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
) -> None:
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)
    cfg = Config.load()

    # Act
    doc_id = create_vault_note(
        test_db,
        cfg=cfg,
        vault_path=vault,
        title="Canonical fields",
        body="content",
        tags=["alpha"],
        embedder=fake_embedder,
    )

    # Assert
    fields, _ = parse_frontmatter((vault / "canonical-fields.md").read_text())
    assert fields["id"] == doc_id
    assert fields["title"] == "Canonical fields"
    assert fields["kind"] == "vault"
    assert "created" in fields
    assert "updated" in fields
    assert fields["tags"] == ["alpha"]


def test_create_vault_note_honors_folder(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
) -> None:
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)
    cfg = Config.load()

    # Act
    create_vault_note(
        test_db,
        cfg=cfg,
        vault_path=vault,
        title="Nested note",
        body="content",
        tags=[],
        folder="elicited",
        embedder=fake_embedder,
    )

    # Assert
    assert (vault / "elicited" / "nested-note.md").is_file()


def test_create_vault_note_normalizes_tags(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
) -> None:
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)
    cfg = Config.load()

    # Act — mixed casing + separators should canonicalize.
    doc_id = create_vault_note(
        test_db,
        cfg=cfg,
        vault_path=vault,
        title="Tag normalization",
        body="content",
        tags=["Tacit Knowledge", "tacit-knowledge"],
        embedder=fake_embedder,
    )

    # Assert — duplicate canonical form is deduped to a single tag.
    fields, _ = parse_frontmatter((vault / "tag-normalization.md").read_text())
    assert fields["tags"] == ["tacit-knowledge"]
    row = test_db.execute(
        "SELECT tags FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == ["tacit-knowledge"]


def test_create_vault_note_missing_template_raises(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
) -> None:
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)
    cfg = Config.load()

    # Act / Assert
    with pytest.raises(VaultNoteSyncError) as excinfo:
        create_vault_note(
            test_db,
            cfg=cfg,
            vault_path=vault,
            title="No such template",
            body="content",
            tags=[],
            template="does-not-exist",
            embedder=fake_embedder,
        )
    assert excinfo.value.errors
    # Nothing was written to disk.
    assert not (vault / "no-such-template.md").exists()


def test_create_vault_note_builds_embedder_from_cfg_when_absent(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no embedder is injected, the helper builds one from ``cfg`` via
    its ``_build_embedder`` factory (patched here to the fake)."""
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)
    cfg = Config.load()
    monkeypatch.setattr(
        "brain.vault.note_builder._build_embedder", lambda _cfg: fake_embedder
    )

    # Act
    doc_id = create_vault_note(
        test_db,
        cfg=cfg,
        vault_path=vault,
        title="Factory embedder",
        body="content",
        tags=[],
    )

    # Assert
    row = test_db.execute(
        "SELECT kind FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == "vault"
