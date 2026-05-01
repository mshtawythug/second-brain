"""Integration tests for ``brain.vault.rename.plan_rename``.

Real-DB pattern: each test seeds a vault on disk, syncs it, then exercises
``plan_rename`` directly. The plan phase is read-only — these tests verify
reference detection, code-fence skipping, and collision rejection without
touching ``apply_rename`` (the apply path is covered in
``tests/test_cli_note_rename.py``).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from brain.vault.frontmatter import dump_frontmatter
from brain.vault.rename import RenameError, plan_rename
from brain.vault.sync import sync_vault


def _write(path: Path, fields: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


def _seed_and_sync(
    test_db: psycopg.Connection,
    fake_embedder,
    vault: Path,
    files: dict[str, tuple[dict, str]],
) -> dict[str, str]:
    """Write each file, run a full sync, and return the assigned ids by path.

    Each ``files`` entry maps a vault-relative POSIX path to ``(fields, body)``.
    Returns a dict from the same path to the doc id ``brain vault sync``
    assigned (lets tests look up "the renamed doc's id" without hardcoding
    UUIDs).
    """
    for relative, (fields, body) in files.items():
        _write(vault / relative, fields, body)
    # ``link_rewrite=False`` keeps the seeded ``[[Title]]`` references in
    # their authored bare form so these tests can assert on exact link
    # text. The rename module's path-form fallback is exercised by
    # dedicated tests in ``tests/test_vault_link_rewrite.py``.
    sync_vault(
        test_db,
        embedder=fake_embedder,
        vault_path=vault,
        link_rewrite=False,
    )
    rows = test_db.execute(
        "SELECT id::text, vault_path FROM documents WHERE kind='vault'"
    ).fetchall()
    return {str(vault_path): str(doc_id) for doc_id, vault_path in rows}


def test_plan_detects_simple_reference(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    ids = _seed_and_sync(
        test_db,
        fake_embedder,
        vault,
        {
            "target.md": ({"title": "Target"}, "body\n"),
            "other.md": ({"title": "Other"}, "see [[Target]] for details\n"),
        },
    )
    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["target.md"],
        new_title="Renamed Target",
    )
    assert len(op.references) == 1
    ref = op.references[0]
    assert ref.file_path == vault / "other.md"
    assert ref.old_text == "[[Target]]"
    assert ref.new_text == "[[Renamed Target]]"


def test_plan_detects_alias_form(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``[[Target|display]]`` rewrites to ``[[New|display]]`` (display kept)."""
    vault = tmp_path / "vault"
    ids = _seed_and_sync(
        test_db,
        fake_embedder,
        vault,
        {
            "target.md": ({"title": "Target"}, "body\n"),
            "other.md": (
                {"title": "Other"},
                "see [[Target|the famous one]] for context\n",
            ),
        },
    )
    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["target.md"],
        new_title="Renamed",
    )
    assert len(op.references) == 1
    assert op.references[0].old_text == "[[Target|the famous one]]"
    assert op.references[0].new_text == "[[Renamed|the famous one]]"


def test_plan_detects_heading_form(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    ids = _seed_and_sync(
        test_db,
        fake_embedder,
        vault,
        {
            "target.md": ({"title": "Target"}, "body\n"),
            "other.md": ({"title": "Other"}, "see [[Target#section-3]]\n"),
        },
    )
    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["target.md"],
        new_title="Renamed",
    )
    assert op.references[0].old_text == "[[Target#section-3]]"
    assert op.references[0].new_text == "[[Renamed#section-3]]"


def test_plan_detects_embed_form(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    ids = _seed_and_sync(
        test_db,
        fake_embedder,
        vault,
        {
            "target.md": ({"title": "Target"}, "body\n"),
            "other.md": ({"title": "Other"}, "embedded: ![[Target]] right here\n"),
        },
    )
    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["target.md"],
        new_title="Renamed",
    )
    assert op.references[0].old_text == "![[Target]]"
    assert op.references[0].new_text == "![[Renamed]]"


def test_plan_is_case_insensitive(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``[[target]]`` matches doc with title ``Target`` (mirrors resolver)."""
    vault = tmp_path / "vault"
    ids = _seed_and_sync(
        test_db,
        fake_embedder,
        vault,
        {
            "target.md": ({"title": "Target"}, "body\n"),
            "other.md": ({"title": "Other"}, "see [[target]] (lowercased)\n"),
        },
    )
    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["target.md"],
        new_title="Renamed",
    )
    assert len(op.references) == 1
    # The user wrote [[target]]; the rewrite produces [[Renamed]] (the new
    # canonical form, not lowercased).
    assert op.references[0].new_text == "[[Renamed]]"


def test_plan_skips_code_fences(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``[[Target]]`` inside a code fence is NOT rewritten."""
    vault = tmp_path / "vault"
    body_with_code = (
        "outside [[Target]] reference\n"
        "\n"
        "```python\n"
        "x = '[[Target]]'  # this should NOT be rewritten\n"
        "```\n"
        "\n"
        "and `[[Target]]` inline code\n"
    )
    ids = _seed_and_sync(
        test_db,
        fake_embedder,
        vault,
        {
            "target.md": ({"title": "Target"}, "body\n"),
            "other.md": ({"title": "Other"}, body_with_code),
        },
    )
    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["target.md"],
        new_title="Renamed",
    )
    # Only the "outside" reference is matched. Line numbers are file-relative
    # (they include the frontmatter block above the body).
    assert len(op.references) == 1
    assert op.references[0].old_text == "[[Target]]"
    # The "outside" reference is the only match — neither the fenced code
    # nor the inline-code variants slip through.
    text = (vault / "other.md").read_text()
    assert text.splitlines()[op.references[0].line_no - 1].strip() == (
        "outside [[Target]] reference"
    )


def test_plan_only_matches_target_title(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``[[Other]]`` doesn't get rewritten when renaming ``Target``."""
    vault = tmp_path / "vault"
    ids = _seed_and_sync(
        test_db,
        fake_embedder,
        vault,
        {
            "target.md": ({"title": "Target"}, "body\n"),
            "other.md": ({"title": "Other"}, "body\n"),
            "linker.md": (
                {"title": "Linker"},
                "links to [[Target]] and [[Other]] separately\n",
            ),
        },
    )
    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["target.md"],
        new_title="Renamed",
    )
    assert len(op.references) == 1
    assert op.references[0].old_text == "[[Target]]"


def test_plan_collects_multiple_references_per_file(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A single file with three matches yields three references in order."""
    vault = tmp_path / "vault"
    ids = _seed_and_sync(
        test_db,
        fake_embedder,
        vault,
        {
            "target.md": ({"title": "Target"}, "body\n"),
            "other.md": (
                {"title": "Other"},
                "first [[Target]]\nsecond [[Target]]\nthird [[Target]]\n",
            ),
        },
    )
    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["target.md"],
        new_title="X",
    )
    assert len(op.references) == 3
    line_nos = [r.line_no for r in op.references]
    assert line_nos == sorted(line_nos)  # document order


def test_plan_rejects_collision_with_existing_file(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Renaming to a slug that's already used → RenameError."""
    vault = tmp_path / "vault"
    ids = _seed_and_sync(
        test_db,
        fake_embedder,
        vault,
        {
            "alpha.md": ({"title": "Alpha"}, "body\n"),
            "beta.md": ({"title": "Beta"}, "body\n"),
        },
    )
    with pytest.raises(RenameError, match="already exists"):
        plan_rename(
            test_db,
            vault_path=vault,
            document_id=ids["alpha.md"],
            new_title="Beta",  # slugifies to "beta", collides with beta.md
        )


def test_plan_rejects_empty_title(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    ids = _seed_and_sync(
        test_db,
        fake_embedder,
        vault,
        {"alpha.md": ({"title": "Alpha"}, "body\n")},
    )
    with pytest.raises(RenameError, match="empty"):
        plan_rename(
            test_db,
            vault_path=vault,
            document_id=ids["alpha.md"],
            new_title="   ",
        )


def test_plan_rejects_unknown_doc_id(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bogus = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(RenameError, match="not found"):
        plan_rename(
            test_db,
            vault_path=vault,
            document_id=bogus,
            new_title="X",
        )


def test_plan_rejects_ingested_doc(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
    seed_doc: Callable[..., str],
) -> None:
    """Only vault-tier docs can be renamed."""
    ingested_id = seed_doc(title="Ingested Doc", content="x")
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(RenameError, match="not 'vault'"):
        plan_rename(
            test_db,
            vault_path=vault,
            document_id=ingested_id,
            new_title="New Title",
        )


def test_plan_allows_same_slug_title_change(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Title change that doesn't change the slug → file stays put, no collision."""
    vault = tmp_path / "vault"
    ids = _seed_and_sync(
        test_db,
        fake_embedder,
        vault,
        {"alpha.md": ({"title": "Alpha"}, "body\n")},
    )
    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["alpha.md"],
        new_title="Alpha!",  # slugify("Alpha!") == "alpha"
    )
    # Same slug → new_path == old_path.
    assert op.new_path == op.old_path


def test_plan_skips_non_title_links(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Source-prefixed links (``[[brain:...]]``, ``[[krisp:...]]``) are NOT
    rewritten by a title-rename — those bind to ids, not titles."""
    vault = tmp_path / "vault"
    ids = _seed_and_sync(
        test_db,
        fake_embedder,
        vault,
        {
            "target.md": ({"title": "Target"}, "body\n"),
            "other.md": (
                {"title": "Other"},
                "see [[Target]] and [[brain:7c2a8b9f]] and [[krisp:abc123]]\n",
            ),
        },
    )
    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["target.md"],
        new_title="Renamed",
    )
    # Only the title-form ``[[Target]]`` is matched. The brain: and krisp:
    # forms are scoped by id / external_id and are not affected by the rename.
    assert len(op.references) == 1
    assert op.references[0].old_text == "[[Target]]"


def test_plan_skips_references_in_frontmatter(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A ``[[Target]]`` inside YAML frontmatter is NOT a link."""
    vault = tmp_path / "vault"
    other_path = vault / "other.md"
    other_path.parent.mkdir(parents=True, exist_ok=True)
    # Hand-write so the [[Target]] sits inside the frontmatter block.
    other_path.write_text(
        "---\n"
        "title: Other\n"
        "comment: \"see [[Target]] later\"\n"
        "---\n"
        "\n"
        "real body, no references here.\n"
    )
    _write(vault / "target.md", {"title": "Target"}, "body\n")
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    target_id = test_db.execute(
        "SELECT id::text FROM documents WHERE title='Target'"
    ).fetchone()
    assert target_id is not None
    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=str(target_id[0]),
        new_title="Renamed",
    )
    assert op.references == ()
