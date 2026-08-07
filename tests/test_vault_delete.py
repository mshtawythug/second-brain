"""The extracted delete path — ``brain.vault.delete`` (F8).

``brain rm``'s four steps (read title + vault_path, DELETE the row, drop the
doc from the people graph, unlink the on-disk mirror) used to live inline in
the CLI command body. Wave 5's ``brain ui`` and the MCP ``brain_rm`` tool
need the same sequence, so it moved here.

These tests pin the extraction's contract directly, without going through
Typer: the display-suffix strings (asserted separately end-to-end in
``tests/test_cli_rm.py``), the machine-readable ``mirror_action``, the
"read before delete" ordering, and the missing-file tolerance that stops a
vanished mirror from stranding the caller with a half-applied delete.

All fixture data is synthetic.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.vault.delete import (
    MIRROR_ABSENT,
    MIRROR_DB_ONLY,
    MIRROR_UNLINKED,
    DeleteReport,
    delete_document,
    describe_delete_target,
    unlink_vault_mirror,
)


class RecordingGraphSyncer:
    """Test double for the one ``GraphSyncer`` method the delete path uses.

    Records the calls it receives so a test can assert the graph step ran
    *after* the row delete. Not a monkeypatch — it is injected through
    ``delete_document``'s ``graph_syncer`` parameter, which exists for
    exactly this reason.
    """

    def __init__(self) -> None:
        self.removed: list[str] = []
        self.row_existed_at_remove: list[bool] = []

    def remove(self, conn: psycopg.Connection[Any], document_id: str) -> None:
        self.removed.append(document_id)
        row = conn.execute(
            "SELECT 1 FROM documents WHERE id=%s", (document_id,)
        ).fetchone()
        self.row_existed_at_remove.append(row is not None)


def _seed_doc(
    conn: psycopg.Connection[Any],
    *,
    title: str = "Quarterly Planning Notes",
    vault_path: str | None = None,
) -> str:
    """Insert a minimal document row directly. Returns its id.

    ``content_hash`` is NOT NULL and UNIQUE, so it is derived from the title
    to keep multi-row tests collision-free without pulling in the whole
    ingest pipeline.
    """
    row = conn.execute(
        "INSERT INTO documents "
        "(title, content, content_type, kind, vault_path, content_hash) "
        "VALUES (%s, %s, 'note', 'vault', %s, %s) RETURNING id::text",
        (
            title,
            "synthetic body\n",
            vault_path,
            sha256(f"{title}|{vault_path}".encode()).hexdigest(),
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _write_mirror(vault: Path, relative: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntitle: Quarterly Planning Notes\n---\n\nbody\n")
    return path


# ---------------------------------------------------------------------------
# unlink_vault_mirror — the pure half
# ---------------------------------------------------------------------------


def test_unlink_returns_db_only_when_vault_path_is_null(tmp_path: Path) -> None:
    action, suffix = unlink_vault_mirror(vault_root=tmp_path, vault_path_rel=None)

    assert action == MIRROR_DB_ONLY
    assert suffix == " (db only)"


def test_unlink_removes_the_file_and_names_it_in_the_suffix(
    tmp_path: Path,
) -> None:
    mirror = _write_mirror(tmp_path, "projects/quarterly-planning-notes.md")

    action, suffix = unlink_vault_mirror(
        vault_root=tmp_path,
        vault_path_rel="projects/quarterly-planning-notes.md",
    )

    assert action == MIRROR_UNLINKED
    assert suffix == " (file: projects/quarterly-planning-notes.md)"
    assert not mirror.exists()


def test_unlink_tolerates_an_already_missing_file(tmp_path: Path) -> None:
    """A vanished mirror must not raise — the DB row is already gone."""
    action, suffix = unlink_vault_mirror(
        vault_root=tmp_path, vault_path_rel="gone/never-existed.md"
    )

    assert action == MIRROR_ABSENT
    assert suffix == " (db only, file already gone)"


# ---------------------------------------------------------------------------
# describe_delete_target — the read used by every confirmation surface
# ---------------------------------------------------------------------------


def test_describe_returns_title_and_vault_path_without_deleting(
    test_db: psycopg.Connection,
) -> None:
    doc_id = _seed_doc(test_db, vault_path="inbox/quarterly-planning-notes.md")

    target = describe_delete_target(test_db, document_id=doc_id)

    assert target is not None
    assert target.document_id == doc_id
    assert target.title == "Quarterly Planning Notes"
    assert target.vault_path == "inbox/quarterly-planning-notes.md"
    still_there = test_db.execute(
        "SELECT 1 FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert still_there is not None, "describe must not delete anything"


def test_describe_returns_none_for_an_unknown_id(
    test_db: psycopg.Connection,
) -> None:
    assert (
        describe_delete_target(
            test_db, document_id="00000000-0000-0000-0000-000000000000"
        )
        is None
    )


# ---------------------------------------------------------------------------
# delete_document — the whole sequence
# ---------------------------------------------------------------------------


def test_delete_removes_row_graph_entry_and_mirror(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    relative = "projects/quarterly-planning-notes.md"
    mirror = _write_mirror(tmp_path, relative)
    doc_id = _seed_doc(test_db, vault_path=relative)
    syncer = RecordingGraphSyncer()

    report = delete_document(
        test_db, document_id=doc_id, vault_root=tmp_path, graph_syncer=syncer
    )

    assert isinstance(report, DeleteReport)
    assert report.document_id == doc_id
    assert report.title == "Quarterly Planning Notes"
    assert report.vault_path == relative
    assert report.mirror_action == MIRROR_UNLINKED
    assert report.suffix == f" (file: {relative})"
    assert not mirror.exists()
    assert (
        test_db.execute(
            "SELECT 1 FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
        is None
    )
    assert syncer.removed == [doc_id]


def test_graph_removal_runs_after_the_row_delete(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    """Ordering regression: the graph GC must see the cascade already done.

    ``GraphSyncer.remove`` GCs person vertices orphaned by the cascade. If it
    ran *before* the DELETE the cascade would not have happened yet and the
    orphans would survive.
    """
    doc_id = _seed_doc(test_db, vault_path=None)
    syncer = RecordingGraphSyncer()

    delete_document(
        test_db, document_id=doc_id, vault_root=tmp_path, graph_syncer=syncer
    )

    assert syncer.row_existed_at_remove == [False]


def test_delete_tolerates_a_missing_mirror(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    """The row still goes, and the report says the cleanup was a no-op."""
    doc_id = _seed_doc(test_db, vault_path="inbox/manually-removed.md")

    report = delete_document(
        test_db,
        document_id=doc_id,
        vault_root=tmp_path,
        graph_syncer=RecordingGraphSyncer(),
    )

    assert report.mirror_action == MIRROR_ABSENT
    assert report.suffix == " (db only, file already gone)"
    assert (
        test_db.execute(
            "SELECT 1 FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
        is None
    )


def test_delete_reports_db_only_for_a_row_with_no_vault_path(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    doc_id = _seed_doc(test_db, vault_path=None)

    report = delete_document(
        test_db,
        document_id=doc_id,
        vault_root=tmp_path,
        graph_syncer=RecordingGraphSyncer(),
    )

    assert report.mirror_action == MIRROR_DB_ONLY
    assert report.suffix == " (db only)"


def test_delete_without_a_graph_syncer_still_deletes(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    """``graph_syncer=None`` mirrors ``update_document``'s optional contract."""
    doc_id = _seed_doc(test_db, vault_path=None)

    report = delete_document(
        test_db, document_id=doc_id, vault_root=tmp_path, graph_syncer=None
    )

    assert report.document_id == doc_id
    assert (
        test_db.execute(
            "SELECT 1 FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
        is None
    )


def test_delete_raises_valueerror_for_an_unknown_id(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    """A caller that skipped id resolution gets a typed failure, not a no-op."""
    with pytest.raises(ValueError, match="document not found"):
        delete_document(
            test_db,
            document_id="00000000-0000-0000-0000-000000000000",
            vault_root=tmp_path,
            graph_syncer=RecordingGraphSyncer(),
        )


def test_delete_module_imports_without_typer_or_cli() -> None:
    """Wave 5 imports this from a request handler — it must stay CLI-free.

    A stray ``import typer`` (or a back-import of ``brain.cli``) would make
    the module unusable from the web surface and would silently pull the
    whole 9k-line CLI into an HTTP request path.
    """
    import subprocess
    import sys

    code = (
        "import sys, brain.vault.delete;"
        "bad=[m for m in ('typer','brain.cli') if m in sys.modules];"
        "sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True)

    assert proc.returncode == 0, (
        "brain.vault.delete must import without pulling in typer or brain.cli: "
        f"{proc.stderr.decode()}"
    )


def test_create_vault_note_rejects_a_non_autocommit_connection(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """The silent disk/DB divergence, converted into a loud failure (F8).

    ``create_vault_note`` writes the file BEFORE the DB row and reports success
    via its return value. On a non-autocommit connection that produced N files
    on disk, N ids returned, and 1 surviving row — with no exception. The
    orphaned files are unreconcilable: nothing downstream compares disk against
    the DB, so the inconsistency is permanent and invisible.

    Asserting the guard fires is only half of it — the test also asserts NO
    FILE was written, because a guard that raises after the write would still
    leave the orphan it exists to prevent.
    """
    from brain import vault as vault_module
    from brain.config import Config
    from brain.errors import VaultNoteSyncError
    from brain.vault.note_builder import create_vault_note

    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    test_db.autocommit = False

    with pytest.raises(VaultNoteSyncError, match="autocommit"):
        create_vault_note(
            test_db,
            cfg=Config(database_url="postgresql://unused/unused", vault_path=vault),
            vault_path=vault,
            title="Should Not Exist",
            tags=[],
            template="note",
            folder="",
        )

    assert not (vault / "should-not-exist.md").exists(), (
        "the guard must fire BEFORE the file is written, or it still orphans"
    )
