"""Integration tests for ``brain rm`` with vault-mirror file cleanup.

Covers Phase A of ``docs/plans/2026-05-01-watcher-followups.md``: when a doc
has a ``vault_path`` the on-disk mirror under ``cfg.vault_path / vault_path``
must also be unlinked, otherwise the next ``brain vault sync`` re-ingests
it from disk and silently undoes the rm.

The legacy DB-only behavior (no ``vault_path``) is still covered by
``test_cli_tag_rm.py``; the cases here focus on the file-system side
effect and the user-facing echo suffixes.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain import vault as vault_module
from brain.cli import app
from brain.vault.frontmatter import dump_frontmatter
from brain.vault.sync import sync_vault

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _set_env(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    """Wire DATABASE_URL + BRAIN_VAULT_PATH for the CLI under test.

    ``Config.load()`` reads both at command-entry time. The rm command's
    file-unlink path joins ``cfg.vault_path / vault_path`` to find the
    mirror, so BRAIN_VAULT_PATH must point at the test vault.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))


def _write_note(path: Path, fields: dict[str, Any], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


def _seed_vault_note(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    vault: Path,
    *,
    title: str = "Note",
    body: str = "body\n",
) -> str:
    """Create a vault-tier file (``kind='vault'``) and sync it. Return doc id."""
    vault_module.init_vault(vault)
    _write_note(vault / f"{title.lower()}.md", {"title": title}, body)
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    row = test_db.execute(
        "SELECT id::text FROM documents WHERE title=%s", (title,)
    ).fetchone()
    assert row is not None, f"sync did not create row for {title!r}"
    return str(row[0])


def _doc_count(doc_id: str) -> int:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT count(*) FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_rm_vault_doc_deletes_db_row_and_file(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) ``brain rm <vault-doc>`` deletes both the DB row and the mirror."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    doc_id = _seed_vault_note(test_db, fake_embedder, vault)
    _set_env(monkeypatch, vault)
    note_path = vault / "note.md"
    assert note_path.is_file()  # precondition

    result = CliRunner().invoke(app, ["rm", doc_id, "--yes"])
    assert result.exit_code == 0, result.output
    assert "(file: note.md)" in result.output
    assert _doc_count(doc_id) == 0
    assert not note_path.exists()


def test_rm_ingested_doc_with_vault_mirror_unlinks_file(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) Ingested-tier doc with a ``_ingested/manual/...`` mirror: file unlinked too.

    Exercises the case from the dedup pass that motivated this fix —
    ``kind='ingested'`` rows (Krisp/Slack/Gmail/manual exports) hold a
    ``vault_path`` pointing at their ``_ingested/<source>/<slug>.md``
    mirror; ``brain rm`` must clean up the mirror so the next sync
    doesn't resurrect the row.
    """
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault.mkdir()
    _set_env(monkeypatch, vault)

    doc_id = "11111111-1111-4111-8111-111111111111"
    rel_path = "_ingested/manual/2026-04-30-fixture.md"
    test_db.execute(
        "INSERT INTO documents (id, title, content, content_hash, content_type, "
        "tags, metadata, kind, vault_path) VALUES "
        "(%s, 'Fixture', 'body', 'h1', 'transcript', '{}', '{}'::jsonb, "
        "'ingested', %s)",
        (doc_id, rel_path),
    )
    abs_path = vault / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text("---\nid: 11111111\n---\nbody\n")
    assert abs_path.is_file()  # precondition

    result = CliRunner().invoke(app, ["rm", doc_id, "--yes"])
    assert result.exit_code == 0, result.output
    assert f"(file: {rel_path})" in result.output
    assert _doc_count(doc_id) == 0
    assert not abs_path.exists()


def test_rm_doc_with_null_vault_path_is_db_only(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) Stdin-style doc with NULL ``vault_path``: DB delete only, no file work."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault.mkdir()
    _set_env(monkeypatch, vault)

    doc_id = "22222222-2222-4222-8222-222222222222"
    test_db.execute(
        "INSERT INTO documents (id, title, content, content_hash, content_type, "
        "tags, metadata, kind, vault_path) VALUES "
        "(%s, 'Stdin', 'x', 'h-stdin', 'transcript', '{}', '{}'::jsonb, "
        "'ingested', NULL)",
        (doc_id,),
    )
    pre_state = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*"))

    result = CliRunner().invoke(app, ["rm", doc_id, "--yes"])
    assert result.exit_code == 0, result.output
    assert "(db only)" in result.output
    # Discriminate from the "(db only, file already gone)" suffix — the
    # NULL-vault_path branch must NOT mention "file" at all.
    assert "file" not in result.output
    assert _doc_count(doc_id) == 0

    # Vault directory contents are byte-for-byte identical pre/post.
    post_state = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*"))
    assert pre_state == post_state


def test_rm_doc_with_missing_mirror_is_db_only_file_already_gone(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) Doc with ``vault_path`` set but mirror absent: DB delete proceeds, no error."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault.mkdir()
    _set_env(monkeypatch, vault)

    doc_id = "33333333-3333-4333-8333-333333333333"
    rel_path = "_ingested/krisp/2026-04-30-already-gone.md"
    test_db.execute(
        "INSERT INTO documents (id, title, content, content_hash, content_type, "
        "tags, metadata, kind, vault_path) VALUES "
        "(%s, 'Gone', 'body', 'h-gone', 'transcript', '{}', '{}'::jsonb, "
        "'ingested', %s)",
        (doc_id, rel_path),
    )
    # Critical precondition: no file at the path.
    assert not (vault / rel_path).exists()

    result = CliRunner().invoke(app, ["rm", doc_id, "--yes"])
    assert result.exit_code == 0, result.output
    assert "(db only, file already gone)" in result.output
    assert _doc_count(doc_id) == 0


def test_rm_vault_doc_does_not_resurrect_on_next_sync(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(e) Critical regression: after rm, ``brain vault sync`` does NOT re-ingest.

    Pre-fix behavior: ``brain rm`` deleted the DB row but left the file
    on disk; the next ``vault sync`` walked the vault, saw the orphan
    file, and re-created the row by ``content_hash`` (or as a fresh row
    if the slug had drifted). This test reproduces that scenario and
    verifies the fix — the row count stays at 0 across the rm + sync
    cycle.
    """
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    doc_id = _seed_vault_note(test_db, fake_embedder, vault, title="Stable")
    _set_env(monkeypatch, vault)

    # Sanity check: row exists, file exists.
    assert _doc_count(doc_id) == 1
    assert (vault / "stable.md").is_file()

    rm_result = CliRunner().invoke(app, ["rm", doc_id, "--yes"])
    assert rm_result.exit_code == 0, rm_result.output
    assert _doc_count(doc_id) == 0
    assert not (vault / "stable.md").exists()

    # The bug was: a follow-up sync re-walks the vault, finds the file,
    # and re-ingests it. With the fix the file is also gone, so the
    # walker has nothing to ingest.
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)

    total = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert total is not None
    assert total[0] == 0, (
        "no document should exist after rm + sync — file was unlinked, "
        "so the sync walker has nothing to re-ingest"
    )


def test_rm_unknown_id_errors_cleanly(
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — fixture resets the schema
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """(f) ``brain rm <bogus-id>`` errors via ``_resolve_id``; no regression."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _set_env(monkeypatch, vault)

    # 8 hex chars — passes the IdPrefixTooShort gate, fails the lookup.
    result = CliRunner().invoke(app, ["rm", "deadbeef", "--yes"])
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    # Phrasing comes from brain.errors.IdPrefixNotFound: "document not found: ...".
    assert "not found" in combined.lower()


def test_brain_rm_unlinks_mirror_after_ingest(
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — fixture resets the schema
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for bug #3: ``brain rm`` cleans up the mirror after stdin ingest.

    Pre-fix: ``brain ingest-stdin`` created the on-disk mirror via
    ``regenerate_vault_file`` but left ``documents.vault_path = NULL``, so the
    follow-up ``brain rm`` hit the ``vault_path is None`` branch in
    ``_rm_unlink_vault_mirror`` and reported ``" (db only)"`` while the file
    sat orphaned in the vault. With ``regenerate_vault_file`` now updating
    ``documents.vault_path`` after the write, the rm path finds the relative
    path and unlinks the file, surfacing the ``(file: ...)`` suffix.
    """
    # Setup — sandbox both the DB URL and the vault to ``tmp_path``.
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    # Exercise — ingest via stdin so the post-ingest hook writes the mirror
    # and (with this fix) populates ``documents.vault_path``.
    body = "Some manual snippet to mirror + remove.\n"
    ingest_result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", "manual",
            "--external-id", "rm-mirror-1",
            "--title", "Rm mirror smoke",
            "--content-type", "transcript",
        ],
        input=body,
    )
    assert ingest_result.exit_code == 0, ingest_result.output

    mirror_dir = tmp_path / "_ingested" / "manual"
    mirrors = list(mirror_dir.glob("*.md"))
    assert len(mirrors) == 1, f"expected one mirror file, got {mirrors}"
    mirror_path = mirrors[0]
    assert mirror_path.is_file()

    # Resolve the doc id via the title for a clean integration call.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT id::text FROM documents WHERE title = 'Rm mirror smoke'"
        ).fetchone()
    assert row is not None
    doc_id = str(row[0])

    rm_result = CliRunner().invoke(app, ["rm", doc_id, "-y"])

    # Verify — file gone AND CLI suffix is the populated-path branch.
    assert rm_result.exit_code == 0, rm_result.output
    assert not mirror_path.exists(), (
        "brain rm must unlink the mirror once vault_path is populated by "
        "the ingest-time regenerate_vault_file call"
    )
    assert "(file:" in rm_result.output, (
        f"expected '(file: ...)' suffix (path-populated branch); got: "
        f"{rm_result.output!r}"
    )
    assert "(db only)" not in rm_result.output
