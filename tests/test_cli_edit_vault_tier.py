"""Tests for ``brain edit`` on vault-tier vs. ingested-tier documents.

Vault-tier docs route to the new file-backed editor flow (no JSON header).
Ingested-tier docs continue to use the legacy JSON-header + body editor.
The counter-test pins both branches in the same test file so future
regressions in either path show up next to each other.
"""
from __future__ import annotations

import stat
import uuid
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain import vault as vault_module
from brain.cli import app
from brain.vault.frontmatter import dump_frontmatter
from brain.vault.sync import sync_vault


def _make_fake_editor(tmp_path: Path, *, body: str, name: str = "ed.sh") -> Path:
    s = tmp_path / name
    s.write_text(body)
    s.chmod(s.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return s


def _init(p: Path) -> None:
    vault_module.init_vault(p)


def _write(path: Path, fields: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


def _seed_vault_doc(
    test_db: psycopg.Connection, fake_embedder, vault: Path, title: str = "VaultDoc"
) -> str:
    _init(vault)
    note_id = str(uuid.uuid4())
    _write(vault / "vaultdoc.md", {"id": note_id, "title": title}, "initial body\n")
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    return note_id


def test_edit_vault_doc_opens_file_directly(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault-tier doc + no flags → editor opens the .md file (no JSON header).
    Sync runs after editor exit; DB content matches edited file."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    note_id = _seed_vault_doc(test_db, fake_embedder, vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    editor = _make_fake_editor(
        tmp_path,
        body="#!/bin/sh\nprintf '\\nEDITED LINE\\n' >> \"$1\"\nexit 0\n",
    )
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)

    result = CliRunner().invoke(app, ["edit", note_id[:8]])
    assert result.exit_code == 0, result.output
    # File on disk has the appended line.
    assert "EDITED LINE" in (vault / "vaultdoc.md").read_text()
    # DB row reflects the new body.
    row = test_db.execute(
        "SELECT content FROM documents WHERE id=%s", (note_id,)
    ).fetchone()
    assert row is not None
    assert "EDITED LINE" in row[0]


def test_edit_vault_doc_rejects_mutating_flags(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault-tier doc + --title (or any mutating flag) → error, no DB change."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    note_id = _seed_vault_doc(test_db, fake_embedder, vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app, ["edit", note_id[:8], "--title", "should not apply"]
    )
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "file-backed" in combined or "vault" in combined.lower()
    # Title unchanged.
    row = test_db.execute(
        "SELECT title FROM documents WHERE id=%s", (note_id,)
    ).fetchone()
    assert row == ("VaultDoc",)


def test_edit_vault_doc_editor_nonzero_aborts(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    note_id = _seed_vault_doc(test_db, fake_embedder, vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    pre = (vault / "vaultdoc.md").read_text()
    editor = _make_fake_editor(tmp_path, body="#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["edit", note_id[:8]])
    assert result.exit_code == 1, result.output
    assert "aborted" in result.output.lower()
    # File untouched (editor exited non-zero before writing).
    assert (vault / "vaultdoc.md").read_text() == pre


def test_edit_vault_doc_missing_file_errors(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the vault row references a file that doesn't exist on disk, error."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    note_id = _seed_vault_doc(test_db, fake_embedder, vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    # Delete the file out from under the DB row.
    (vault / "vaultdoc.md").unlink()
    result = CliRunner().invoke(app, ["edit", note_id[:8]])
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "missing" in combined.lower() or "vault file" in combined.lower()


def test_edit_ingested_doc_still_uses_json_header(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    seed_doc: Callable[..., str],
) -> None:
    """Ingested-tier doc + no flags → JSON-header flow (existing behavior).

    A no-op editor exits 0 and the CLI reports "no changes" — the same
    contract as ``test_editor_no_change_is_noop`` in test_cli_edit_editor.py.
    """
    patch_embedder(fake_embedder)
    doc_id = seed_doc(title="Ingested doc", content="ingested body")
    editor = _make_fake_editor(tmp_path, body="#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 0, result.output
    assert "no changes" in result.output


def test_edit_ingested_doc_flag_mode_works(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    """Ingested-tier + --title (existing flag-mode flow) still works."""
    patch_embedder(fake_embedder)
    doc_id = seed_doc(title="Old", content="x")
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--title", "New title"]
    )
    assert result.exit_code == 0, result.output
    row = test_db.execute(
        "SELECT title FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert row == ("New title",)
