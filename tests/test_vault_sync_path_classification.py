"""Path-based kind classification for brain.vault.sync.

The sync engine derives ``documents.kind`` from the file's path: anything
under ``_ingested/`` is ingested-tier, everything else is vault-tier. The
file's frontmatter ``kind:`` is informational only — when it disagrees with
the path, the path wins (with a warning logged).
"""
import logging
import uuid
from pathlib import Path

import psycopg

from brain.vault.frontmatter import dump_frontmatter
from brain.vault.sync import sync_vault


def _write(path: Path, fields: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


def _kind_of(conn: psycopg.Connection, doc_id: str) -> str:
    row = conn.execute("SELECT kind FROM documents WHERE id = %s", (doc_id,)).fetchone()
    assert row is not None
    return str(row[0])


def test_file_at_vault_root_is_vault_tier(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(vault / "root-note.md", {"id": note_id, "title": "Root Note"}, "x\n")
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    assert _kind_of(test_db, note_id) == "vault"


def test_file_under_subfolder_is_vault_tier(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "projects" / "company-id" / "person-a.md",
        {"id": note_id, "title": "person-x"},
        "x\n",
    )
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    assert _kind_of(test_db, note_id) == "vault"


def test_file_under_ingested_krisp_is_ingested_tier(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "_ingested" / "krisp" / "2026-04-15-call.md",
        {"id": note_id, "title": "Call"},
        "transcript\n",
    )
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    assert _kind_of(test_db, note_id) == "ingested"


def test_file_under_ingested_slack_is_ingested_tier(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "_ingested" / "slack" / "thread.md",
        {"id": note_id, "title": "Thread"},
        "x\n",
    )
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    assert _kind_of(test_db, note_id) == "ingested"


def test_path_wins_over_frontmatter_kind(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
    caplog,
) -> None:
    """A file under ``_ingested/`` whose frontmatter says ``kind: vault`` is
    classified as ingested anyway — the path is the canonical signal."""
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "_ingested" / "krisp" / "weird.md",
        {"id": note_id, "title": "Weird", "kind": "vault"},
        "x\n",
    )
    with caplog.at_level(logging.WARNING, logger="brain.vault.sync"):
        sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    assert _kind_of(test_db, note_id) == "ingested"
    # A warning was logged about the inconsistency.
    assert any(
        "declares kind=" in rec.getMessage() for rec in caplog.records
    )


def test_path_wins_over_frontmatter_kind_inverse(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
    caplog,
) -> None:
    """A file at the vault root with ``kind: ingested`` is still classified vault."""
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "weird.md",
        {"id": note_id, "title": "Weird", "kind": "ingested"},
        "x\n",
    )
    with caplog.at_level(logging.WARNING, logger="brain.vault.sync"):
        sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    assert _kind_of(test_db, note_id) == "vault"


def test_files_directly_under_ingested_root_are_ingested_tier(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A file at ``_ingested/foo.md`` (no source subfolder) still ingested.

    Edge case: a user moves a file into ``_ingested/`` without nesting it.
    The classification is based on the first path segment, so this is
    correctly tagged ingested.
    """
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "_ingested" / "loose.md",
        {"id": note_id, "title": "Loose"},
        "x\n",
    )
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    assert _kind_of(test_db, note_id) == "ingested"


def test_files_under_templates_are_ignored_entirely(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``_templates/*.md`` files are not synced regardless of frontmatter."""
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "_templates" / "weird.md",
        {"id": note_id, "title": "T"},
        "{{date}}\n",
    )
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    row = test_db.execute(
        "SELECT count(*) FROM documents WHERE id = %s", (note_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == 0


def test_invalid_kind_in_frontmatter_is_silently_ignored(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
) -> None:
    """A bogus ``kind:`` value (e.g. ``kind: unknown``) doesn't trigger the
    inconsistency warning — only valid-but-mismatched ``vault``/``ingested``
    values do."""
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "n.md",
        {"id": note_id, "title": "X", "kind": "unknown"},
        "x\n",
    )
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    # Path-based classification still applies.
    assert _kind_of(test_db, note_id) == "vault"
