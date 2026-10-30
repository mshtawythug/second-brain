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
from brain.vault.note_builder import index_vault_note, write_vault_note


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


def test_create_vault_note_uniquifies_slug_on_collision(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
) -> None:
    """Calling twice with the same title must not overwrite the first note.

    The ``_codify`` path bypasses ``brain note new``'s pre-write collision
    guard, so the helper itself must uniquify the slug rather than clobber an
    existing file (data loss).
    """
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)
    cfg = Config.load()

    # Act — same title + DISTINCT bodies, twice.
    first_id = create_vault_note(
        test_db,
        cfg=cfg,
        vault_path=vault,
        title="Collision note",
        body="FIRST distinctive body.",
        tags=["tacit"],
        embedder=fake_embedder,
    )
    second_id = create_vault_note(
        test_db,
        cfg=cfg,
        vault_path=vault,
        title="Collision note",
        body="SECOND distinctive body.",
        tags=["tacit"],
        embedder=fake_embedder,
    )

    # Assert — two DISTINCT documents on two DISTINCT files.
    assert first_id != second_id
    first_file = vault / "collision-note.md"
    second_file = vault / "collision-note-2.md"
    assert first_file.is_file()
    assert second_file.is_file()

    # The first note's content is unchanged (not overwritten).
    first_fields, first_body = parse_frontmatter(first_file.read_text())
    assert "FIRST distinctive body." in first_body
    second_fields, second_body = parse_frontmatter(second_file.read_text())
    assert "SECOND distinctive body." in second_body

    # The frontmatter id of each file matches the returned id, and the indexed
    # DB rows correspond to the actually-written files.
    assert first_fields["id"] == first_id
    assert second_fields["id"] == second_id
    rows = test_db.execute(
        "SELECT id::text, content FROM documents WHERE id IN (%s, %s)",
        (first_id, second_id),
    ).fetchall()
    by_id = {r[0]: r[1] for r in rows}
    assert "FIRST distinctive body." in by_id[first_id]
    assert "SECOND distinctive body." in by_id[second_id]


def test_create_vault_note_rejects_folder_escaping_vault(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
) -> None:
    """A ``folder`` that escapes the vault root is rejected; nothing is written.

    Security guard: ``folder`` is used to place the note under the vault, so a
    traversal value (``..`` segments) must raise :class:`VaultNoteSyncError`
    before any directory or file is created outside the vault.
    """
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)
    cfg = Config.load()

    # Act / Assert — a sibling-of-vault escape inside tmp_path so the negative
    # filesystem assertion is deterministic.
    with pytest.raises(VaultNoteSyncError):
        create_vault_note(
            test_db,
            cfg=cfg,
            vault_path=vault,
            title="Escape attempt",
            body="content",
            tags=[],
            folder="../escapee",
            embedder=fake_embedder,
        )

    # Nothing authored outside the vault, and nothing inside it either.
    assert not (tmp_path / "escapee").exists()
    assert not (vault / "escape-attempt.md").exists()


def test_create_vault_note_rejects_absolute_folder_escape(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
) -> None:
    """An absolute ``folder`` (which pathlib lets replace the vault root) is rejected."""
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)
    cfg = Config.load()
    outside = tmp_path / "outside-abs"

    # Act / Assert
    with pytest.raises(VaultNoteSyncError):
        create_vault_note(
            test_db,
            cfg=cfg,
            vault_path=vault,
            title="Absolute escape",
            body="content",
            tags=[],
            folder=str(outside),
            embedder=fake_embedder,
        )
    assert not (outside / "absolute-escape.md").exists()


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


# ---------------------------------------------------------------------------
# RM-39: the write / index split and ``extra_frontmatter``.
# ---------------------------------------------------------------------------


def test_write_vault_note_writes_the_file_and_touches_no_database(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)

    # Act
    target, doc_id = write_vault_note(
        vault_path=vault, title="Written only", body="the body", tags=["Inbox"],
        folder="capture",
    )

    # Assert
    assert target == vault / "capture" / "written-only.md"
    fields, body = parse_frontmatter(target.read_text(encoding="utf-8"))
    assert fields["id"] == doc_id
    assert fields["tags"] == ["inbox"]
    assert body.strip() == "the body"
    assert test_db.execute("SELECT count(*) FROM documents").fetchone() == (0,)


def test_write_vault_note_refuses_before_writing(tmp_path: Path) -> None:
    # Arrange: no _templates/ at all, and a folder that escapes the vault.
    vault = tmp_path / "vault"
    vault.mkdir()

    # Act / Assert
    with pytest.raises(VaultNoteSyncError):
        write_vault_note(vault_path=vault, title="No template", body="x")
    _init_vault(vault)
    with pytest.raises(VaultNoteSyncError):
        write_vault_note(vault_path=vault, title="Escape", body="x", folder="../out")
    assert not (tmp_path / "out").exists()


def test_extra_frontmatter_lands_but_never_overrides_brain_managed_fields(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder
) -> None:
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)

    # Act
    doc_id = create_vault_note(
        test_db,
        cfg=Config.load(),
        vault_path=vault,
        title="With extras",
        body="content",
        tags=["inbox"],
        embedder=fake_embedder,
        extra_frontmatter={
            "sensitivity": "confidential",
            "capture_import": "abc123",
            "id": "not-a-real-id",
            "title": "forged",
            "kind": "ingested",
            "tags": ["forged"],
        },
    )

    # Assert
    row = test_db.execute(
        "SELECT title, kind, tags, sensitivity, metadata, vault_path FROM documents "
        "WHERE id = %s",
        (doc_id,),
    ).fetchone()
    assert row is not None
    title, kind, tags, sensitivity, metadata, vault_path = row
    assert (title, kind, tags, sensitivity) == ("With extras", "vault", ["inbox"], "confidential")
    assert metadata.get("capture_import") == "abc123"
    fields, _ = parse_frontmatter((vault / vault_path).read_text(encoding="utf-8"))
    assert fields["id"] == doc_id


def test_create_vault_note_is_write_then_index(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder
) -> None:
    """create_vault_note's behaviour is unchanged by the split: one file, one row,
    the row's id is the file's id, and an index error leaves the file on disk."""
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)
    cfg = Config.load()

    # Act
    target, doc_id = write_vault_note(vault_path=vault, title="Two step", body="b")
    index_vault_note(test_db, cfg=cfg, vault_path=vault, target=target, embedder=fake_embedder)

    # Assert
    row = test_db.execute("SELECT vault_path FROM documents WHERE id = %s", (doc_id,)).fetchone()
    assert row == ("two-step.md",)


def test_index_vault_note_raises_on_a_sync_error_and_keeps_the_file(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder
) -> None:
    # Arrange: a note whose frontmatter has no title is a per-file sync error.
    vault = tmp_path / "vault"
    _init_vault(vault)
    target = vault / "broken.md"
    target.write_text("---\nid: x\n---\nbody\n", encoding="utf-8")

    # Act / Assert
    with pytest.raises(VaultNoteSyncError):
        index_vault_note(
            test_db, cfg=Config.load(), vault_path=vault, target=target, embedder=fake_embedder
        )
    assert target.exists()


def test_extra_tags_never_replace_the_brain_managed_tags(tmp_path: Path) -> None:
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)

    # Act: no tags argument, and a forged ``tags`` in the extras.
    target, _ = write_vault_note(
        vault_path=vault, title="No tags", body="b", extra_frontmatter={"tags": "forged"}
    )

    # Assert
    fields, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
    assert fields["tags"] == []


def test_a_template_with_malformed_frontmatter_still_makes_a_note(tmp_path: Path) -> None:
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)
    (vault / "_templates" / "broken.md").write_text(
        "---\ntitle: [unclosed\n---\ntemplate body\n", encoding="utf-8"
    )

    # Act
    target, doc_id = write_vault_note(vault_path=vault, title="From broken", template="broken")

    # Assert: a fresh header, and the raw rendered template as the body.
    fields, body = parse_frontmatter(target.read_text(encoding="utf-8"))
    assert (fields["id"], fields["title"], fields["tags"]) == (doc_id, "From broken", [])
    assert "template body" in body


def test_a_template_without_tags_gets_an_empty_list(tmp_path: Path) -> None:
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)
    (vault / "_templates" / "bare.md").write_text("---\nnote: x\n---\nbody\n", encoding="utf-8")

    # Act
    target, _ = write_vault_note(vault_path=vault, title="Bare", template="bare")

    # Assert
    fields, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
    assert fields["tags"] == []


def test_a_third_note_with_one_title_is_suffixed_3(tmp_path: Path) -> None:
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)

    # Act
    targets = [write_vault_note(vault_path=vault, title="Same", body="b")[0] for _ in range(3)]

    # Assert
    assert [t.name for t in targets] == ["same.md", "same-2.md", "same-3.md"]


def test_the_default_embedder_comes_from_config() -> None:
    from brain.vault.note_builder import _build_embedder

    embedder = _build_embedder(Config.load())

    assert embedder.dim > 0


def test_a_name_taken_by_a_dangling_symlink_is_never_written_through(tmp_path: Path) -> None:
    """rev-111 2: the note file is created exclusively (O_EXCL), so a name that is taken,
    even by a dangling symlink, moves the note to ``-2`` instead of writing through it."""
    # Arrange
    vault = tmp_path / "vault"
    _init_vault(vault)
    outside = tmp_path / "outside.md"
    (vault / "taken.md").symlink_to(outside)

    # Act
    target, _ = write_vault_note(vault_path=vault, title="Taken", body="b")

    # Assert
    assert target.name == "taken-2.md"
    assert not outside.exists()


def test_a_note_that_fails_to_write_leaves_no_partial_file(tmp_path: Path) -> None:
    # Arrange: a lone surrogate cannot be encoded, so the write fails after the create.
    vault = tmp_path / "vault"
    _init_vault(vault)

    # Act / Assert
    with pytest.raises(UnicodeEncodeError):
        write_vault_note(vault_path=vault, title="Unwritable", body="bad \ud800 text")
    assert not (vault / "unwritable.md").exists()
