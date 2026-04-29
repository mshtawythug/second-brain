"""Integration tests for ``brain note rename`` and ``apply_rename``.

These cover the destructive write path — frontmatter title rewrite, file
move, reference refactor across multiple files, and the
snapshot/restore-on-failure contract.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain import vault as vault_module
from brain.cli import app
from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter
from brain.vault.rename import apply_rename, plan_rename
from brain.vault.sync import sync_vault


def _init(p: Path) -> None:
    vault_module.init_vault(p)


def _write(path: Path, fields: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


def _seed_three_note_vault(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> tuple[Path, dict[str, str]]:
    """Set up a vault with target.md + two referencing files; returns ids."""
    vault = tmp_path / "vault"
    _init(vault)
    _write(vault / "target.md", {"title": "Target"}, "primary body\n")
    _write(
        vault / "alpha.md",
        {"title": "Alpha"},
        "see [[Target]] for context\n",
    )
    _write(
        vault / "beta.md",
        {"title": "Beta"},
        "embed: ![[Target]]\nalias: [[Target|the famous one]]\n",
    )
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    rows = test_db.execute(
        "SELECT title, id::text FROM documents WHERE kind='vault'"
    ).fetchall()
    return vault, {str(t): str(i) for t, i in rows}


def test_rename_full_flow(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault, ids = _seed_three_note_vault(test_db, fake_embedder, tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app, ["note", "rename", ids["Target"], "person-x conversation"]
    )
    assert result.exit_code == 0, result.output
    # Old file gone, new file exists.
    assert not (vault / "target.md").exists()
    assert (vault / "person-a-conversation.md").is_file()
    # References rewritten.
    alpha = (vault / "alpha.md").read_text()
    assert "[[person-x conversation]]" in alpha
    assert "[[Target]]" not in alpha
    beta = (vault / "beta.md").read_text()
    assert "![[person-x conversation]]" in beta
    assert "[[person-x conversation|the famous one]]" in beta
    # DB row title updated by the post-rename sync.
    row = test_db.execute(
        "SELECT title FROM documents WHERE id=%s", (ids["Target"],)
    ).fetchone()
    assert row == ("person-x conversation",)


def test_rename_dry_run_makes_no_changes(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault, ids = _seed_three_note_vault(test_db, fake_embedder, tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    pre_alpha = (vault / "alpha.md").read_text()
    pre_target = (vault / "target.md").read_text()
    result = CliRunner().invoke(
        app, ["note", "rename", ids["Target"], "Renamed", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "would" in result.output.lower()
    # Files untouched.
    assert (vault / "target.md").read_text() == pre_target
    assert (vault / "alpha.md").read_text() == pre_alpha
    # DB title untouched.
    row = test_db.execute(
        "SELECT title FROM documents WHERE id=%s", (ids["Target"],)
    ).fetchone()
    assert row == ("Target",)


def test_rename_no_link_refactor_skips_others(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-link-refactor``: source-file moves + frontmatter updates, others
    keep their old ``[[Target]]`` text."""
    patch_embedder(fake_embedder)
    vault, ids = _seed_three_note_vault(test_db, fake_embedder, tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app,
        [
            "note",
            "rename",
            ids["Target"],
            "Renamed",
            "--no-link-refactor",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (vault / "renamed.md").is_file()
    # Other files keep their old links — they'll show up as unresolved on
    # the next vault sync, which is the user's signal to clean them up.
    assert "[[Target]]" in (vault / "alpha.md").read_text()


def test_rename_collision_rejected(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault, ids = _seed_three_note_vault(test_db, fake_embedder, tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app, ["note", "rename", ids["Target"], "Alpha"]
    )
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "already exists" in combined


def test_rename_resync_resolves_links(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After rename, a full sync produces zero unresolved links."""
    patch_embedder(fake_embedder)
    vault, ids = _seed_three_note_vault(test_db, fake_embedder, tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    CliRunner().invoke(app, ["note", "rename", ids["Target"], "person-x conversation"])
    # Post-rename, run a full sync to update every other file's links.
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    cnt = test_db.execute("SELECT count(*) FROM unresolved_links").fetchone()
    assert cnt is not None and cnt[0] == 0


def test_rename_atomic_restore_on_failure(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a write mid-rename fails, every snapshotted file is restored.

    We simulate the failure by injecting an exception inside the helper
    that rewrites references — *after* one file's writes have completed,
    *before* the source file is rewritten — and assert the modified file
    is restored to its pre-call bytes.
    """
    vault, ids = _seed_three_note_vault(test_db, fake_embedder, tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["Target"],
        new_title="person-x conversation",
    )

    pre_target = (vault / "target.md").read_text()
    pre_alpha = (vault / "alpha.md").read_text()
    pre_beta = (vault / "beta.md").read_text()

    real_write = Path.write_text
    call_count = {"n": 0}

    def flaky_write_text(self: Path, data: str, *args, **kwargs) -> int:
        # Allow snapshot writes (those go to backup_dir, not into vault).
        if vault.resolve() not in self.resolve().parents and self.resolve() != vault.resolve():
            return real_write(self, data, *args, **kwargs)
        call_count["n"] += 1
        # Let the first vault write through (rewrites alpha.md or beta.md);
        # blow up on the second so the source file rewrite never happens.
        if call_count["n"] >= 2:
            raise OSError("simulated failure mid-rename")
        return real_write(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky_write_text)
    with pytest.raises(OSError, match="simulated failure"):
        apply_rename(
            test_db,
            embedder=fake_embedder,
            vault_path=vault,
            op=op,
        )
    # Restore patched method so we can read back without side effects.
    monkeypatch.setattr(Path, "write_text", real_write)

    # All three files restored to pre-call bytes.
    assert (vault / "target.md").read_text() == pre_target
    assert (vault / "alpha.md").read_text() == pre_alpha
    assert (vault / "beta.md").read_text() == pre_beta
    # The new path was never created (or was cleaned up).
    assert not (vault / "person-a-conversation.md").exists()


def test_rename_invalid_id_errors(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app, ["note", "rename", "00000000", "X"]
    )
    assert result.exit_code != 0


def test_rename_ingested_doc_rejected(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    seed_doc: Callable[..., str],
) -> None:
    """Ingested-tier docs can't be renamed via this command."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    ingested_id = seed_doc(title="Ingested only", content="x")
    result = CliRunner().invoke(
        app, ["note", "rename", ingested_id, "Renamed"]
    )
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "vault" in combined.lower()


def test_rename_updates_frontmatter_title(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The renamed file's frontmatter ``title`` is updated."""
    patch_embedder(fake_embedder)
    vault, ids = _seed_three_note_vault(test_db, fake_embedder, tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    CliRunner().invoke(app, ["note", "rename", ids["Target"], "New title"])
    fields, _ = parse_frontmatter((vault / "new-title.md").read_text())
    assert fields["title"] == "New title"
    assert "updated" in fields  # the apply phase stamps the timestamp


def test_rename_same_slug_in_place(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Title change that doesn't change the slug → file stays at same path."""
    patch_embedder(fake_embedder)
    vault, ids = _seed_three_note_vault(test_db, fake_embedder, tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app, ["note", "rename", ids["Target"], "Target!"]
    )
    assert result.exit_code == 0, result.output
    # Same slug → file stays put.
    assert (vault / "target.md").is_file()
    fields, _ = parse_frontmatter((vault / "target.md").read_text())
    assert fields["title"] == "Target!"


def test_rename_handles_self_referencing_note(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A note that links to its own title gets the body rewrite too."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    _write(
        vault / "self.md",
        {"title": "Self"},
        "this is the [[Self]] note (links to itself)\n",
    )
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    self_id = test_db.execute(
        "SELECT id::text FROM documents WHERE title='Self'"
    ).fetchone()
    assert self_id is not None
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app, ["note", "rename", str(self_id[0]), "Renamed Self"]
    )
    assert result.exit_code == 0, result.output
    new_body = (vault / "renamed-self.md").read_text()
    assert "[[Renamed Self]]" in new_body
    assert "[[Self]]" not in new_body


def test_rename_skips_files_with_malformed_frontmatter(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file with malformed frontmatter is skipped in the reference scan
    (the apply rename to other files still works)."""
    patch_embedder(fake_embedder)
    vault, ids = _seed_three_note_vault(test_db, fake_embedder, tmp_path)
    # Stick a malformed file into the vault (its [[Target]] should NOT be
    # rewritten — we don't know where the body starts).
    bad = vault / "bad.md"
    bad.write_text("---\nfoo: [unclosed\n---\nsee [[Target]] here\n")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app, ["note", "rename", ids["Target"], "Renamed"]
    )
    assert result.exit_code == 0, result.output
    # Other valid files were rewritten.
    assert "[[Renamed]]" in (vault / "alpha.md").read_text()
    # The malformed file was untouched.
    assert "[[Target]]" in bad.read_text()


def test_rename_no_references_only_title_change(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A note with no inbound references can still be renamed."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    _write(vault / "lonely.md", {"title": "Lonely"}, "no references here\n")
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    lonely_id = test_db.execute(
        "SELECT id::text FROM documents WHERE title='Lonely'"
    ).fetchone()
    assert lonely_id is not None
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app, ["note", "rename", str(lonely_id[0]), "Reclusive"]
    )
    assert result.exit_code == 0, result.output
    assert (vault / "reclusive.md").is_file()
    assert not (vault / "lonely.md").exists()


def test_rename_dry_run_no_references(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run output for a rename with zero references prints clean message."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    _write(vault / "lonely.md", {"title": "Lonely"}, "x\n")
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    lonely_id = test_db.execute(
        "SELECT id::text FROM documents WHERE title='Lonely'"
    ).fetchone()
    assert lonely_id is not None
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app,
        ["note", "rename", str(lonely_id[0]), "Reclusive", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "no references" in result.output.lower()


def test_rename_doc_with_no_vault_path_errors(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vault-tier doc with NULL vault_path (data inconsistency) errors."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    note_id = "11111111-1111-4111-8111-111111111111"
    test_db.execute(
        "INSERT INTO documents (id, title, content, content_hash, content_type, "
        "tags, metadata, kind, vault_path) VALUES (%s, 'Orphan', 'body', 'h', 'note', "
        "'{}', '{}'::jsonb, 'vault', NULL)",
        (note_id,),
    )
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app, ["note", "rename", note_id, "Renamed"]
    )
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "vault_path" in combined or "sync" in combined.lower()


def test_rename_cleans_up_partial_new_file_on_failure(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the source-write succeeds but the old-file unlink fails, restore +
    remove the new file so the vault is byte-identical to pre-call state."""
    vault, ids = _seed_three_note_vault(test_db, fake_embedder, tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["Target"],
        new_title="Renamed",
    )
    pre_target = (vault / "target.md").read_text()
    pre_alpha = (vault / "alpha.md").read_text()
    pre_beta = (vault / "beta.md").read_text()

    real_unlink = Path.unlink

    def flaky_unlink(self: Path, *args, **kwargs) -> None:
        # Only fail when unlinking the source file mid-rename — every other
        # unlink (e.g. from snapshot cleanup) goes through.
        if self.resolve() == op.old_path.resolve():
            raise OSError("simulated unlink failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    with pytest.raises(OSError, match="simulated unlink failure"):
        apply_rename(
            test_db,
            embedder=fake_embedder,
            vault_path=vault,
            op=op,
        )
    monkeypatch.setattr(Path, "unlink", real_unlink)

    # Restore happened: every original file is byte-identical, and the
    # half-written new file was removed.
    assert (vault / "target.md").read_text() == pre_target
    assert (vault / "alpha.md").read_text() == pre_alpha
    assert (vault / "beta.md").read_text() == pre_beta
    assert not (vault / "renamed.md").exists()


def test_rename_doc_with_missing_file_errors(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vault-tier doc whose file vanished from disk → clear error."""
    patch_embedder(fake_embedder)
    vault, ids = _seed_three_note_vault(test_db, fake_embedder, tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    (vault / "target.md").unlink()
    result = CliRunner().invoke(
        app, ["note", "rename", ids["Target"], "X"]
    )
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "missing" in combined.lower() or "prune" in combined.lower()
