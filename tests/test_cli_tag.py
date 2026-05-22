"""Integration tests for ``brain tag`` writing to vault frontmatter.

These cover Phase 2 of the brain-tag-frontmatter-write plan: the DB write +
file frontmatter rewrite + ``--regenerate-file`` flag for missing-mirror
recovery. The companion ``test_cli_tag_rm.py`` covers the legacy DB-only
behavior on docs without a ``vault_path``.
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
from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter
from brain.vault.sync import sync_vault

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _set_env(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    """Wire DATABASE_URL + BRAIN_VAULT_PATH for the CLI subprocess.

    The CLI's ``Config.load()`` reads both, and the file-writeback code path
    builds ``cfg.vault_path / vault_path`` to find the on-disk mirror.
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
    tags: list[str] | None = None,
) -> str:
    """Create a vault-tier file and sync it; return the doc id."""
    vault_module.init_vault(vault)
    fields: dict[str, Any] = {"title": title}
    if tags is not None:
        fields["tags"] = tags
    _write_note(vault / f"{title.lower()}.md", fields, body)
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    row = test_db.execute(
        "SELECT id::text FROM documents WHERE title=%s", (title,)
    ).fetchone()
    assert row is not None, f"sync did not create row for {title!r}"
    return str(row[0])


def _tags_for(doc_id: str) -> list[str]:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT tags FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
    assert row is not None
    return list(row[0] or [])


def _frontmatter_tags(path: Path) -> list[str]:
    fields, _ = parse_frontmatter(path.read_text())
    raw = fields.get("tags") or []
    assert isinstance(raw, list), f"tags must be a list, got {type(raw).__name__}"
    return [str(t) for t in raw]


def test_tag_vault_doc_writes_db_and_file(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) brain tag <vault-doc> +foo updates DB + frontmatter."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    doc_id = _seed_vault_note(test_db, fake_embedder, vault)
    _set_env(monkeypatch, vault)

    result = CliRunner().invoke(app, ["tag", doc_id, "+foo"])
    assert result.exit_code == 0, result.output
    assert "(file)" in result.output

    assert _tags_for(doc_id) == ["foo"]
    assert _frontmatter_tags(vault / "note.md") == ["foo"]


def test_tag_persists_through_sync(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) Regression for the original bug: tag survives the next vault sync.

    Previously ``brain tag`` only wrote to ``documents.tags``; the next sync
    re-read ``tags: []`` from disk and overwrote the DB. With Phase 2 the
    file's frontmatter is also updated, so the round-trip is stable.
    """
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    doc_id = _seed_vault_note(test_db, fake_embedder, vault)
    _set_env(monkeypatch, vault)

    result = CliRunner().invoke(app, ["tag", doc_id, "+keep-me"])
    assert result.exit_code == 0, result.output
    assert _tags_for(doc_id) == ["keep-me"]

    # Run sync via the same code path the CLI uses; the bug surfaced when the
    # next sync overwrote the DB tags from disk's ``tags: []``.
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)

    assert _tags_for(doc_id) == ["keep-me"], (
        "tag should survive vault sync now that frontmatter mirrors the DB"
    )


def test_tag_doc_with_null_vault_path_is_db_only(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) Stdin-style doc with NULL vault_path → DB write only, no file work."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault.mkdir()
    _set_env(monkeypatch, vault)

    # Insert a row directly with vault_path=NULL (mimics manual / stdin ingest
    # before any export pass touched the row).
    doc_id = "22222222-2222-4222-8222-222222222222"
    test_db.execute(
        "INSERT INTO documents (id, title, content, content_hash, content_type, "
        "tags, metadata, kind, vault_path) VALUES "
        "(%s, 'Stdin', 'x', 'h1', 'transcript', '{}', '{}'::jsonb, "
        "'ingested', NULL)",
        (doc_id,),
    )
    pre_state = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*"))

    result = CliRunner().invoke(app, ["tag", doc_id, "+foo"])
    assert result.exit_code == 0, result.output
    assert "(db only)" in result.output
    # No yellow warning should be emitted on the NULL-vault_path branch.
    assert "file missing" not in result.output

    assert _tags_for(doc_id) == ["foo"]
    # Vault directory contents are byte-for-byte identical pre/post (no file
    # was created, and crucially no _ingested/ mirror was synthesized).
    post_state = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*"))
    assert pre_state == post_state


def test_tag_missing_ingested_file_warns_db_only(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) Ingested doc whose mirror is gone → DB-only + yellow warning."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault.mkdir()
    _set_env(monkeypatch, vault)

    doc_id = "33333333-3333-4333-8333-333333333333"
    test_db.execute(
        "INSERT INTO documents (id, title, content, content_hash, content_type, "
        "tags, metadata, kind, vault_path) VALUES "
        "(%s, 'Gone', 'body', 'h2', 'transcript', '{}', '{}'::jsonb, "
        "'ingested', '_ingested/krisp/2026-04-30-x-gone.md')",
        (doc_id,),
    )

    result = CliRunner().invoke(app, ["tag", doc_id, "+foo"])
    assert result.exit_code == 0, result.output
    combined = result.output + (result.stderr or "")
    # Suffix path is hit (printed on stdout next to the tag summary).
    assert "(db only, file missing)" in result.output
    # Warning text is emitted on stderr — assert on a phrase UNIQUE to the
    # warning so a regression that silently drops the warning still fails
    # this test (the substring "file missing" appears in both the suffix
    # AND the warning, so it is not discriminating).
    assert "Pass --regenerate-file" in combined
    assert _tags_for(doc_id) == ["foo"]
    # No file should have been written by the warning path.
    assert not (vault / "_ingested/krisp/2026-04-30-x-gone.md").exists()


def test_tag_missing_vault_file_warns_db_only(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d2) Vault-tier doc whose file is gone (no flag) → DB-only + warning.

    Mirrors the ingested-tier ``(db only, file missing)`` branch, but for
    ``kind='vault'``: a missing authored note must NOT be regenerated from
    the DB without an explicit flag (regenerating risks data loss vs. the
    canonical on-disk source). Without ``--regenerate-file`` the CLI should
    warn and apply the tag in the DB only — exit 0, no file created.
    """
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault.mkdir()
    _set_env(monkeypatch, vault)

    doc_id = "66666666-6666-4666-8666-666666666666"
    rel_path = "missing-authored.md"
    test_db.execute(
        "INSERT INTO documents (id, title, content, content_hash, content_type, "
        "tags, metadata, kind, vault_path) VALUES "
        "(%s, 'MissingAuthored', 'authored body', 'h5', 'note', "
        "'{}', '{}'::jsonb, 'vault', %s)",
        (doc_id, rel_path),
    )
    pre_state = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*"))

    result = CliRunner().invoke(app, ["tag", doc_id, "+foo"])
    assert result.exit_code == 0, result.output
    combined = result.output + (result.stderr or "")
    # Suffix is printed on stdout for the vault-missing branch.
    assert "(db only, vault file missing)" in result.output
    # Warning text is emitted on stderr — assert on a phrase UNIQUE to the
    # vault-missing warning (the suffix uses "vault file missing" while the
    # warning uses "vault-tier authored note is missing on disk").
    assert "vault-tier authored note is missing on disk" in combined

    # DB tag was applied even though the file write was skipped.
    assert _tags_for(doc_id) == ["foo"]

    # No file was synthesized at the missing path; vault directory contents
    # are byte-for-byte identical pre/post.
    post_state = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*"))
    assert pre_state == post_state
    assert not (vault / rel_path).exists()


def test_tag_missing_ingested_with_regenerate_file(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(e) ``--regenerate-file`` recreates the mirror and lands the new tag."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault.mkdir()
    _set_env(monkeypatch, vault)

    doc_id = "44444444-4444-4444-8444-444444444444"
    rel_path = "_ingested/krisp/2026-04-30-x-recover.md"
    test_db.execute(
        "INSERT INTO documents (id, title, content, content_hash, content_type, "
        "tags, metadata, kind, vault_path) VALUES "
        "(%s, 'Recover', 'recovered body', 'h3', 'transcript', "
        "'{}', '{}'::jsonb, 'ingested', %s)",
        (doc_id, rel_path),
    )

    result = CliRunner().invoke(
        app, ["tag", doc_id, "+foo", "--regenerate-file"]
    )
    assert result.exit_code == 0, result.output
    assert "(file regenerated)" in result.output

    target = vault / rel_path
    assert target.is_file(), "regenerate_vault_file should have re-created the mirror"
    assert _tags_for(doc_id) == ["foo"]
    assert _frontmatter_tags(target) == ["foo"]
    assert "recovered body" in target.read_text()


def test_tag_missing_vault_with_regenerate_file_errors(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(f) ``--regenerate-file`` is rejected for vault-tier authored notes."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault.mkdir()
    _set_env(monkeypatch, vault)

    doc_id = "55555555-5555-4555-8555-555555555555"
    rel_path = "lost-note.md"
    test_db.execute(
        "INSERT INTO documents (id, title, content, content_hash, content_type, "
        "tags, metadata, kind, vault_path) VALUES "
        "(%s, 'Lost', 'authored body', 'h4', 'note', '{}', '{}'::jsonb, "
        "'vault', %s)",
        (doc_id, rel_path),
    )

    result = CliRunner().invoke(
        app, ["tag", doc_id, "+foo", "--regenerate-file"]
    )
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "vault-tier" in combined or "restore from backup" in combined
    # No file was created — the early reject runs before any file write.
    assert not (vault / rel_path).exists()


def test_tag_is_idempotent_on_disk(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(g) Re-tagging with the same value is a no-op on the second pass.

    Asserts via byte-identity of the on-disk file rather than mtime so the
    test stays deterministic across filesystems with low-resolution mtimes.
    The first call writes (and bumps ``updated:``); the second call's
    ``rewrite_tags`` short-circuits because ``current_tags == desired_tags``.
    """
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    doc_id = _seed_vault_note(test_db, fake_embedder, vault)
    _set_env(monkeypatch, vault)

    first = CliRunner().invoke(app, ["tag", doc_id, "+foo"])
    assert first.exit_code == 0, first.output
    assert "(file)" in first.output
    after_first_bytes = (vault / "note.md").read_bytes()

    second = CliRunner().invoke(app, ["tag", doc_id, "+foo"])
    assert second.exit_code == 0, second.output
    after_second_bytes = (vault / "note.md").read_bytes()

    assert after_first_bytes == after_second_bytes, (
        "second +foo call must not rewrite the file (rewrite_tags is idempotent)"
    )


def test_brain_tag_writes_file_after_ingest(
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — fixture resets the schema
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for bug #2: ``brain tag`` writes to the file after stdin ingest.

    Pre-fix: ``brain ingest-stdin`` materialized the on-disk mirror but left
    ``documents.vault_path = NULL``. The follow-up ``brain tag`` hit the
    ``vault_path is None`` short-circuit in ``_tag_file_writeback`` and
    reported ``" (db only)"`` — the DB tag list was right but the file's
    frontmatter ``tags:`` stayed empty and would be re-flushed back over
    the DB on the next vault sync. With ``vault_path`` populated post-
    ingest, the file-rewrite branch lights up and the suffix becomes
    ``" (file)"``.
    """
    # Setup — sandbox both the DB URL and the vault.
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    # Exercise — ingest a manual snippet via stdin (mirror is written and
    # ``vault_path`` is populated as part of the same call).
    ingest_result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", "manual",
            "--external-id", "tag-mirror-1",
            "--title", "Tag mirror smoke",
            "--content-type", "transcript",
        ],
        input="A note that needs tagging post-ingest.\n",
    )
    assert ingest_result.exit_code == 0, ingest_result.output

    mirror_path = tmp_path / "_ingested" / "manual" / "tag-mirror-smoke.md"
    assert mirror_path.is_file(), f"missing mirror at {mirror_path}"
    # Precondition: file's tags are empty before the tag command runs.
    assert _frontmatter_tags(mirror_path) == []

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT id::text FROM documents WHERE title = 'Tag mirror smoke'"
        ).fetchone()
    assert row is not None
    doc_id = str(row[0])

    tag_result = CliRunner().invoke(app, ["tag", doc_id, "+new-tag"])

    # Verify — DB + file are both updated; suffix is the file-rewrite branch.
    assert tag_result.exit_code == 0, tag_result.output
    assert "(file)" in tag_result.output, (
        f"expected '(file)' suffix (path-populated branch); got: "
        f"{tag_result.output!r}"
    )
    assert "(db only)" not in tag_result.output
    assert _tags_for(doc_id) == ["new-tag"]
    assert _frontmatter_tags(mirror_path) == ["new-tag"], (
        "brain tag must write to the file's frontmatter once vault_path is "
        "populated by the ingest-time regenerate_vault_file call"
    )
