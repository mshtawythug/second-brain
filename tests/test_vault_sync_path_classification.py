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


def test_files_under_hidden_directories_are_ignored(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Markdown files under hidden directories (``.quartz/``, ``.git/``,
    ``.obsidian/``) are NOT walked.

    Regression test for the bug where ``brain vault sync`` descended into
    Quartz's own ``.quartz/`` workspace and tried to ingest its issue
    templates and docs as vault notes — producing 72 unwanted vault-tier
    rows and a flurry of "missing or empty 'title' in frontmatter" errors
    on Quartz's own ``.md`` files (which use a different frontmatter
    convention than ours). The fix mirrors the watcher's ``_filter_path``:
    skip any path whose components include a hidden directory.
    """
    vault = tmp_path / "vault"

    # A Quartz workspace fragment — Quartz's docs use frontmatter without
    # our required ``title`` field, which previously surfaced as errors.
    (vault / ".quartz" / "docs" / "features").mkdir(parents=True)
    (vault / ".quartz" / "docs" / "features" / "upcoming features.md").write_text(
        "---\nstatus: draft\n---\n\nQuartz docs body — not ours.\n"
    )
    (vault / ".quartz" / ".github" / "ISSUE_TEMPLATE").mkdir(parents=True)
    (vault / ".quartz" / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").write_text(
        "---\nname: Bug report\nabout: file a bug\n---\n\nNot our format.\n"
    )

    # Other typical hidden directories that should also be skipped.
    (vault / ".git").mkdir()
    (vault / ".git" / "HEAD").write_text("ref: refs/heads/master\n")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "config.md").write_text(
        "---\nfoo: bar\n---\n\nObsidian internal.\n"
    )

    # And one legitimate vault note alongside, to confirm the walker still
    # produces useful work — without this assertion a passing test could
    # mean "we filtered everything, including legit files".
    note_id = str(uuid.uuid4())
    _write(vault / "real-note.md", {"id": note_id, "title": "Real Note"}, "body\n")

    report = sync_vault(test_db, embedder=fake_embedder, vault_path=vault)

    # The legit note creates one row; nothing else should.
    assert report.created == 1
    assert report.errors == []
    rows = test_db.execute(
        "SELECT vault_path FROM documents ORDER BY vault_path"
    ).fetchall()
    assert [r[0] for r in rows] == ["real-note.md"]


def test_sync_one_file_rejects_hidden_directory_paths(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``sync_one_file`` rejects a path with a hidden component.

    Same skip rule as the full walker — a path under ``.quartz/`` or
    ``.git/`` is not a syncable note and shouldn't sneak in via the
    one-file authoring helper either.
    """
    from brain.vault.sync import sync_one_file

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".quartz").mkdir()
    quartz_doc = vault / ".quartz" / "internal.md"
    quartz_doc.write_text("---\ntitle: Quartz Internal\n---\n\nbody\n")

    report = sync_one_file(
        test_db, embedder=fake_embedder, vault_path=vault, file_path=quartz_doc
    )
    assert report.created == 0
    assert report.errors
    assert "hidden" in report.errors[0][1].lower()
