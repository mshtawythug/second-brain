"""Tests for ``brain vault prune-orphans``.

The command lists (dry-run) or deletes (``--apply``) ``_ingested/`` mirror
files whose frontmatter ``id`` has no matching ``documents.id`` row.

Setup pattern: real test DB, ``BRAIN_VAULT_PATH`` sandboxed via
``monkeypatch.setenv``. Each test seeds a vault dir + (when needed) DB rows
that map to specific frontmatter ids so we can assert which files survive.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document
from brain.vault.frontmatter import dump_frontmatter

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _write_mirror(
    path: Path, *, doc_id: str, title: str = "x", body: str = "body\n"
) -> None:
    """Drop a mirror file with frontmatter ``id``/``title`` at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        dump_frontmatter({"id": doc_id, "title": title}, body),
        encoding="utf-8",
    )


def _ingest_one(
    test_db: psycopg.Connection, fake_embedder: Any, *, body: str, title: str
) -> str:
    """Ingest one stdin doc → returns its UUID. Used to pin a 'known' id.

    Each call must have a unique body so the partial UNIQUE on
    ``content_hash`` doesn't dedupe seeds away.
    """
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title=title,
            content=body,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    assert result.document_id is not None
    return result.document_id


def test_prune_orphans_dry_run_lists_without_deleting(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run prints the candidates and leaves disk untouched.

    Setup: two orphan mirror files (frontmatter id has no DB row) + an empty
    DB.
    Exercise: ``brain vault prune-orphans`` without ``--apply``.
    Verify: stdout names both files, both still exist, output advertises
    the ``--apply`` flag.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    vault = tmp_path / "vault"
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    a = vault / "_ingested" / "manual" / "orphan-a.md"
    b = vault / "_ingested" / "krisp" / "orphan-b.md"
    _write_mirror(a, doc_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    _write_mirror(b, doc_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

    result = CliRunner().invoke(app, ["vault", "prune-orphans"])
    assert result.exit_code == 0, result.output
    assert "would delete" in result.output
    assert str(a) in result.output
    assert str(b) in result.output
    assert "dry-run" in result.output
    assert "--apply" in result.output
    # Files survived.
    assert a.is_file()
    assert b.is_file()


def test_prune_orphans_apply_deletes_files(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--apply`` deletes orphans, leaves a known-id mirror untouched.

    Setup: three orphan files + one mirror whose frontmatter id matches a
    real DB row.
    Exercise: ``brain vault prune-orphans --apply``.
    Verify: orphans gone, known-id file survives, summary reports
    ``deleted: 3``.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    vault = tmp_path / "vault"
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    real_id = _ingest_one(
        test_db, fake_embedder, body="real body for known id", title="known"
    )
    known = vault / "_ingested" / "manual" / "known.md"
    _write_mirror(known, doc_id=real_id, title="known")

    orphans = []
    for letter in ("a", "b", "c"):
        path = vault / "_ingested" / "manual" / f"orphan-{letter}.md"
        _write_mirror(path, doc_id=f"{letter*8}-{letter*4}-4{letter*3}-8{letter*3}-{letter*12}")
        orphans.append(path)

    result = CliRunner().invoke(app, ["vault", "prune-orphans", "--apply"])
    assert result.exit_code == 0, result.output
    assert "deleted: 3" in result.output
    for path in orphans:
        assert not path.exists(), f"{path} should have been deleted"
    assert known.is_file()


def test_prune_orphans_skips_files_without_frontmatter(
    test_db: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_ingested/`` files lacking parseable frontmatter are NEVER deleted.

    Setup: one ``_ingested/junk/no-frontmatter.md`` containing only plain
    text (no opening ``---``).
    Exercise: ``brain vault prune-orphans --apply``.
    Verify: file survives — "no frontmatter" is intentional, not orphan.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    vault = tmp_path / "vault"
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    no_fm = vault / "_ingested" / "junk" / "no-frontmatter.md"
    no_fm.parent.mkdir(parents=True, exist_ok=True)
    no_fm.write_text("Just plain prose, no YAML header.\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["vault", "prune-orphans", "--apply"])
    assert result.exit_code == 0, result.output
    assert no_fm.is_file()
    assert "0 orphan files" in result.output


def test_prune_orphans_refuses_when_ingested_dir_missing(
    test_db: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault without ``_ingested/`` → exit 2 with a guiding message.

    Defensive guard against running the command on a fresh / mis-configured
    vault, where the silent zero-result would be more confusing than an
    explicit error.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    result = CliRunner().invoke(app, ["vault", "prune-orphans"])
    assert result.exit_code == 2
    combined = result.output + (result.stderr if result.stderr else "")
    assert "_ingested" in combined
