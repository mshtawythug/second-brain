"""Integration tests for ``brain note new``.

The fake editor pattern matches ``test_cli_edit_editor.py`` — a tiny shell
script writes deterministic content into the file the CLI hands it. No real
``$EDITOR`` is ever launched.
"""
from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain import vault as vault_module
from brain.cli import app
from brain.vault.frontmatter import parse_frontmatter


def _make_fake_editor(tmp_path: Path, *, body: str, name: str = "fake.sh") -> Path:
    script = tmp_path / name
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _init_vault(vault_path: Path) -> None:
    vault_module.init_vault(vault_path)


def test_note_new_writes_file_and_syncs(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    result = CliRunner().invoke(
        app, ["note", "new", "person-x Q1 review", "--no-edit"]
    )
    assert result.exit_code == 0, result.output
    target = vault / "person-x-q1-review.md"
    assert target.is_file()

    fields, body = parse_frontmatter(target.read_text())
    assert fields["title"] == "person-x Q1 review"
    assert "id" in fields
    assert fields["kind"] == "vault"
    assert "created" in fields
    assert "updated" in fields
    # DB row exists with the same id.
    row = test_db.execute(
        "SELECT title FROM documents WHERE id=%s", (fields["id"],)
    ).fetchone()
    assert row == ("person-x Q1 review",)


def test_note_new_honors_folder(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    result = CliRunner().invoke(
        app,
        [
            "note",
            "new",
            "Sprint planning",
            "--folder",
            "projects",
            "--no-edit",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (vault / "projects" / "sprint-planning.md").is_file()


def test_note_new_rejects_existing_target(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    runner = CliRunner()
    runner.invoke(app, ["note", "new", "Same title", "--no-edit"])
    result = runner.invoke(app, ["note", "new", "Same title", "--no-edit"])
    assert result.exit_code == 1, result.output
    combined = result.stdout + (result.stderr or "")
    assert "already exists" in combined


def test_note_new_rejects_unknown_template(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app,
        [
            "note",
            "new",
            "X",
            "--template",
            "nonexistent",
            "--no-edit",
        ],
    )
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "not found" in combined or "nonexistent" in combined


def test_note_new_rejects_missing_templates_dir(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    # Point at a vault that has no _templates/ directory.
    vault = tmp_path / "raw-vault"
    vault.mkdir()
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(app, ["note", "new", "X", "--no-edit"])
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "vault init" in combined


def test_note_new_invokes_editor_then_syncs(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --no-edit the editor runs and the post-exit sync re-indexes."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    # Editor appends a deterministic line to the file.
    editor = _make_fake_editor(
        tmp_path,
        body="#!/bin/sh\nprintf 'EDITED LINE\\n' >> \"$1\"\n",
    )
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)

    result = CliRunner().invoke(app, ["note", "new", "Editable note"])
    assert result.exit_code == 0, result.output
    target = vault / "editable-note.md"
    assert "EDITED LINE" in target.read_text()
    # DB row reflects the edited body.
    row = test_db.execute(
        "SELECT content FROM documents WHERE title='Editable note'"
    ).fetchone()
    assert row is not None
    assert "EDITED LINE" in row[0]


def test_note_new_assigns_initial_tags(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app,
        [
            "note",
            "new",
            "Tagged",
            "--tag",
            "interview",
            "--tag",
            "career",
            "--no-edit",
        ],
    )
    assert result.exit_code == 0, result.output
    target = vault / "tagged.md"
    fields, _ = parse_frontmatter(target.read_text())
    assert sorted(fields["tags"]) == ["career", "interview"]
    row = test_db.execute(
        "SELECT tags FROM documents WHERE title='Tagged'"
    ).fetchone()
    assert row is not None
    assert sorted(row[0]) == ["career", "interview"]


def test_note_new_uses_vault_flag_override(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--vault`` overrides the configured BRAIN_VAULT_PATH."""
    patch_embedder(fake_embedder)
    vault_a = tmp_path / "default-vault"
    vault_b = tmp_path / "override-vault"
    _init_vault(vault_a)
    _init_vault(vault_b)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault_a))
    result = CliRunner().invoke(
        app,
        ["note", "new", "Side door", "--no-edit", "--vault", str(vault_b)],
    )
    assert result.exit_code == 0, result.output
    assert (vault_b / "side-door.md").is_file()
    assert not (vault_a / "side-door.md").exists()


def test_note_new_editor_nonzero_keeps_file(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editor non-zero exit: file stays in place, initial sync row is intact."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    editor = _make_fake_editor(tmp_path, body="#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["note", "new", "Halfway"])
    # Note new still exits 0 — the file was created and synced; only the
    # post-creation editor session aborted.
    assert result.exit_code == 0, result.output
    target = vault / "halfway.md"
    assert target.is_file()
    # DB row exists from the initial sync.
    row = test_db.execute(
        "SELECT title FROM documents WHERE title='Halfway'"
    ).fetchone()
    assert row is not None


def test_note_new_creates_folder_on_demand(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app,
        [
            "note",
            "new",
            "Deep one",
            "--folder",
            "areas/projects/2026",
            "--no-edit",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (vault / "areas" / "projects" / "2026" / "deep-one.md").is_file()


def test_note_new_uses_template(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom template's body survives note creation (frontmatter forced)."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    custom = vault / "_templates" / "custom.md"
    custom.write_text(
        "---\ntitle: \"{{title}}\"\ntags: [auto]\n---\n\n# {{title}}\n\nCustom body line.\n"
    )
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app,
        [
            "note",
            "new",
            "Custom note",
            "--template",
            "custom",
            "--no-edit",
        ],
    )
    assert result.exit_code == 0, result.output
    text = (vault / "custom-note.md").read_text()
    fields, body = parse_frontmatter(text)
    assert fields["title"] == "Custom note"
    assert fields["kind"] == "vault"
    assert "Custom body line." in body
    # CLI-supplied tags would override; no tags here means we keep template's
    # "auto" tag (it's pulled into existing_fields by parse_frontmatter).
    assert fields.get("tags") == ["auto"]


def test_note_new_cli_tags_override_template_tags(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    custom = vault / "_templates" / "custom.md"
    custom.write_text(
        "---\ntitle: \"{{title}}\"\ntags: [auto, default]\n---\n\nbody\n"
    )
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app,
        [
            "note",
            "new",
            "Override",
            "--template",
            "custom",
            "--tag",
            "user-supplied",
            "--no-edit",
        ],
    )
    assert result.exit_code == 0, result.output
    fields, _ = parse_frontmatter((vault / "override.md").read_text())
    assert fields["tags"] == ["user-supplied"]


def test_note_new_prints_short_id(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app, ["note", "new", "Echo title", "--no-edit"]
    )
    assert result.exit_code == 0, result.output
    assert "created echo-title.md" in result.output
    # The short id (8 chars) appears in the summary.
    fields, _ = parse_frontmatter((vault / "echo-title.md").read_text())
    short = fields["id"][:8]
    assert short in result.output


def _enable_dummy_editor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Convenience: install a no-op editor on $EDITOR for tests that don't
    care what the editor does — they just want it to not block / not error.
    """
    script = _make_fake_editor(tmp_path, body="#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("EDITOR", str(script))
    monkeypatch.delenv("VISUAL", raising=False)


def test_note_new_editor_default_works(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --no-edit, default $EDITOR is launched and exits cleanly."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    _enable_dummy_editor(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["note", "new", "Default editor"])
    assert result.exit_code == 0, result.output
    assert (vault / "default-editor.md").is_file()


def test_folder_path_traversal_rejected(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--folder ../../etc`` must be rejected BEFORE any file is written.

    Regression test for the path-traversal hole: previously the file would
    land at ``<vault>/../../etc/<slug>.md`` (outside the vault) and only
    sync would catch it after the write. Now we reject up front.
    """
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    # Snapshot the vault tree pre-call so we can assert no leakage.
    pre_files = sorted(p.name for p in vault.iterdir())

    result = CliRunner().invoke(
        app,
        [
            "note",
            "new",
            "Escape attempt",
            "--folder",
            "../../etc",
            "--no-edit",
        ],
    )
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "--folder" in combined or "vault" in combined.lower()

    # Vault tree unchanged.
    assert sorted(p.name for p in vault.iterdir()) == pre_files
    # And nothing was written to ``tmp_path/etc`` (one level out).
    assert not (tmp_path / "etc").exists()
    # No DB row created.
    cnt = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert cnt is not None
    assert cnt[0] == 0


def test_post_edit_sync_errors_surface_to_stderr(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If post-edit ``sync_one_file`` produces errors, they appear on stderr.

    Regression test for #2: previously every caller ignored the SyncReport
    returned from ``_run_post_write_editor_and_sync`` so any post-edit
    sync error was invisible. The fix moves error printing into the helper.

    We trigger a sync error by having the fake editor corrupt the file's
    frontmatter to malformed YAML — ``sync_one_file`` then records a
    per-file error in ``report.errors``, which the helper now prints to
    stderr.
    """
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    # Editor overwrites the file with malformed YAML inside the frontmatter
    # block — post-edit sync_one_file will raise + record a parse failure.
    bad_payload = "---\nfoo: [unclosed\n---\nbody after edit\n"
    editor = _make_fake_editor(
        tmp_path,
        body=f"#!/bin/sh\ncat > \"$1\" <<'BRAIN_EOF'\n{bad_payload}\nBRAIN_EOF\nexit 0\n",
    )
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)

    result = CliRunner().invoke(app, ["note", "new", "Sync break"])
    # Exit 0 — the file was created and saved; only the post-edit sync
    # emitted an error. The user got the file on disk regardless.
    assert result.exit_code == 0, result.stdout
    # The sync error must surface — Typer's CliRunner combines stdout+stderr
    # by default in this version, so check the combined output.
    combined = result.stdout + (result.stderr or "")
    assert "post-edit sync error" in combined
    assert "frontmatter" in combined.lower()


def test_note_new_with_no_editor_env(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing $EDITOR + $VISUAL with no PATH fallback — surfaces a warning,
    doesn't crash. The file is still created and synced."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    result = CliRunner().invoke(app, ["note", "new", "No editor"])
    # The file was created and synced before the editor attempt.
    assert (vault / "no-editor.md").is_file()
    # Whether the editor failure exits non-zero or just prints a warning is
    # implementation-specific; both behaviors are accepted as long as the
    # note ends up on disk + in the DB.
    _ = os  # silence unused warning if `os` not referenced elsewhere
    row = test_db.execute(
        "SELECT title FROM documents WHERE title='No editor'"
    ).fetchone()
    assert row is not None
    assert result.exit_code in (0, 1)
