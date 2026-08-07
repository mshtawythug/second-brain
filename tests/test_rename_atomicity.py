"""``apply_rename``'s watcher-safe move semantics (F8).

Pre-F8 the apply phase wrote the new file and then unlinked the old one.
Watchdog therefore emitted ``created(new)`` + ``deleted(old)`` rather than a
move, and the watcher's delete branch issues
``DELETE FROM documents WHERE vault_path = <old_rel>`` — whose
``ON DELETE CASCADE`` wipes every incoming ``links`` row. That is the
backlink-destruction failure recorded from the 2026-07-12 overhaul.

The fix has two observable consequences, both pinned here:

1. The file is relocated with ``Path.replace`` (``os.rename``), so the
   **inode is preserved** and watchdog emits ``moved(old → new)``, which
   routes to the watcher's non-destructive in-place-UPDATE branch.
2. ``documents.vault_path`` is repointed **before** ``sync_one_file`` runs,
   so a stale delete job matches zero rows from that microsecond on.

Plus the cross-device fallback, since ``os.rename`` cannot span mounts.

All fixture data is synthetic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import vault as vault_module
from brain.vault import rename as rename_mod
from brain.vault.frontmatter import dump_frontmatter
from brain.vault.rename import apply_rename, plan_rename
from brain.vault.sync import sync_vault


def _write(path: Path, fields: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


def _seed(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> tuple[Path, str]:
    """One vault-tier note at ``inbox/weekly-sync-platform.md``. Returns its id."""
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    _write(
        vault / "inbox" / "weekly-sync-platform.md",
        {"title": "Weekly Sync Platform"},
        "primary body\n",
    )
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    row = test_db.execute(
        "SELECT id::text FROM documents WHERE title=%s", ("Weekly Sync Platform",)
    ).fetchone()
    assert row is not None
    return vault, str(row[0])


def _plan_move(
    test_db: psycopg.Connection, vault: Path, doc_id: str
) -> rename_mod.RenameOp:
    return plan_rename(
        test_db,
        vault_path=vault,
        document_id=doc_id,
        new_title="Weekly Sync Platform",
        new_folder="projects/atlas",
    )


def test_apply_rename_preserves_inode_so_watcher_sees_a_move(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder: Any
) -> None:
    """RED before F8: write-new + unlink-old produced a *new* inode.

    A new inode is exactly what makes watchdog report ``deleted(old)``
    instead of ``moved(old → new)``, which is what reaches the destructive
    branch. Preserving it is the whole fix.
    """
    vault, doc_id = _seed(test_db, fake_embedder, tmp_path)
    old_path = vault / "inbox" / "weekly-sync-platform.md"
    original_inode = old_path.stat().st_ino

    op = _plan_move(test_db, vault, doc_id)
    apply_rename(test_db, embedder=fake_embedder, vault_path=vault, op=op)

    new_path = vault / "projects" / "atlas" / "weekly-sync-platform.md"
    assert new_path.is_file()
    assert not old_path.exists()
    assert new_path.stat().st_ino == original_inode, (
        "the move must be an os.rename — a new inode makes watchdog emit "
        "deleted(old), which routes to the branch that cascades backlinks away"
    )


def test_vault_path_updated_before_sync(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder: Any,
    mocker: Any,
) -> None:
    """The watcher-race guarantee.

    By the time ``sync_one_file`` runs, the DB row must already point at the
    new path — that is what makes a racing ``DELETE … WHERE vault_path =
    <old_rel>`` match zero rows.
    """
    vault, doc_id = _seed(test_db, fake_embedder, tmp_path)
    observed: list[str | None] = []
    real_sync = rename_mod.sync_one_file

    def _spy(conn: Any, **kwargs: Any) -> Any:
        row = conn.execute(
            "SELECT vault_path FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
        observed.append(None if row is None else row[0])
        return real_sync(conn, **kwargs)

    mocker.patch("brain.vault.rename.sync_one_file", side_effect=_spy)

    op = _plan_move(test_db, vault, doc_id)
    apply_rename(test_db, embedder=fake_embedder, vault_path=vault, op=op)

    assert observed == ["projects/atlas/weekly-sync-platform.md"], (
        "vault_path must be repointed BEFORE sync_one_file runs, not by it"
    )


def test_vault_path_is_persisted_after_the_move(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder: Any
) -> None:
    """End state: the row points at the new path and keeps its id."""
    vault, doc_id = _seed(test_db, fake_embedder, tmp_path)

    op = _plan_move(test_db, vault, doc_id)
    apply_rename(test_db, embedder=fake_embedder, vault_path=vault, op=op)

    row = test_db.execute(
        "SELECT vault_path FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert row is not None, "the document id must survive a move"
    assert row[0] == "projects/atlas/weekly-sync-platform.md"


def test_cross_device_oserror_falls_back_to_write_and_unlink(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder: Any,
    mocker: Any,
) -> None:
    """EXDEV degrades to the old sequence — correct, just not watcher-friendly."""
    vault, doc_id = _seed(test_db, fake_embedder, tmp_path)
    op = _plan_move(test_db, vault, doc_id)
    # Patched after planning so the plan phase's own path work is unaffected.
    mocker.patch(
        "pathlib.Path.replace",
        side_effect=OSError(18, "Invalid cross-device link"),
    )

    apply_rename(test_db, embedder=fake_embedder, vault_path=vault, op=op)

    new_path = vault / "projects" / "atlas" / "weekly-sync-platform.md"
    assert new_path.is_file(), "the file must still reach its destination"
    assert not (vault / "inbox" / "weekly-sync-platform.md").exists()
    assert "primary body" in new_path.read_text()


def test_failure_mid_apply_restores_every_snapshotted_file(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder: Any,
    mocker: Any,
) -> None:
    """The snapshot/restore contract still holds on the move path.

    A failure after the source has been rewritten must leave the vault
    byte-identical to its pre-call state, with nothing stranded at the
    destination.
    """
    vault, doc_id = _seed(test_db, fake_embedder, tmp_path)
    old_path = vault / "inbox" / "weekly-sync-platform.md"
    before = old_path.read_bytes()

    op = _plan_move(test_db, vault, doc_id)
    # A non-OSError failure so the EXDEV fallback does NOT swallow it: this
    # must reach the snapshot-restore handler.
    mocker.patch(
        "pathlib.Path.replace", side_effect=RuntimeError("synthetic move failure")
    )

    with pytest.raises(RuntimeError, match="synthetic move failure"):
        apply_rename(test_db, embedder=fake_embedder, vault_path=vault, op=op)

    assert old_path.read_bytes() == before, "the source must be restored verbatim"
    assert not (vault / "projects" / "atlas" / "weekly-sync-platform.md").exists()
