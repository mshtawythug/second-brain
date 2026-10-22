"""A watcher move keeps the right document's identity (2026-10-22).

Debouncing coalesces a pending upsert or delete of P into a later move
P → Q. If P had been re-populated by a DIFFERENT document (another
frontmatter id) before the rename, the path-only ``UPDATE`` in
``_handle_move`` renamed the previous document's row onto Q, and syncing Q
then collided with it on ``documents_vault_path_idx``. Plan:
``docs/plans/2026-10-22-vault-watcher-move-identity.md``.

Each test drives ``_worker_loop`` synchronously with ONE move job, which is
what the debouncer delivers after coalescing, against the test DB.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import psycopg

from brain.vault.frontmatter import dump_frontmatter, read_frontmatter_id
from brain.vault.sync import sync_vault
from brain.vault.watch import WatchConfig, _Job, _WatcherState, _worker_loop


def _write(path: Path, fields: dict[str, Any], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


def _move_through_worker(
    conn: psycopg.Connection, embedder: Any, vault: Path, src: Path, dst: Path
) -> _WatcherState:
    """Run exactly one coalesced ``move`` job through the worker, synchronously."""
    state = _WatcherState()
    state.jobs.put(_Job(action="move", abs_path=src, dest_path=dst))
    state.jobs.put(None)  # sentinel closes the worker loop
    _worker_loop(conn, embedder, WatchConfig(vault_path=vault, debounce_ms=10), state)
    return state


def _rows_at(conn: psycopg.Connection, vault_path: str) -> list[str]:
    rows = conn.execute(
        "SELECT id::text FROM documents WHERE kind = 'vault' AND vault_path = %s",
        (vault_path,),
    ).fetchall()
    return [r[0] for r in rows]


def _incoming(conn: psycopg.Connection, src_id: str, dst_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM links WHERE src_document_id = %s AND dst_document_id = %s",
        (src_id, dst_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_move_of_a_different_document_does_not_carry_the_old_row(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """(a) The residue: P deleted (doc A), recreated as doc B, renamed P → Q.

    The coalesced job is one move P → Q. A's row must not be renamed onto
    Q: A is gone, as the coalesced delete intended, and B is the row at Q.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    a_id, b_id = str(uuid.uuid4()), str(uuid.uuid4())
    p, q = vault / "p.md", vault / "q.md"
    _write(p, {"id": a_id, "title": "Alpha"}, "alpha body\n")
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    assert _rows_at(test_db, "p.md") == [a_id]

    p.unlink()
    _write(p, {"id": b_id, "title": "Beta"}, "beta body\n")
    p.rename(q)
    state = _move_through_worker(test_db, fake_embedder, vault, p, q)

    assert _rows_at(test_db, "q.md") == [b_id], (
        "the previous document's row was renamed onto the destination"
    )
    assert state.errors == 0, "the move failed (documents_vault_path_idx collision)"
    gone = test_db.execute("SELECT count(*) FROM documents WHERE id = %s", (a_id,)).fetchone()
    assert gone is not None and gone[0] == 0, "the deleted document's row survived"


def _linked_pair(
    conn: psycopg.Connection, embedder: Any, vault: Path, b_id_on_disk: str
) -> tuple[str, str]:
    """``a.md`` links to ``b.md``; return (a_id, b_id) after a full sync."""
    a_id = str(uuid.uuid4())
    _write(vault / "a.md", {"id": a_id, "title": "A"}, "see [[B]]\n")
    _write(vault / "b.md", {"id": b_id_on_disk, "title": "B"}, "b body\n")
    sync_vault(conn, embedder=embedder, vault_path=vault)
    b_id = str(uuid.UUID(b_id_on_disk))
    assert _incoming(conn, a_id, b_id) == 1, "fixture: the a → b backlink must exist"
    return a_id, b_id


def test_same_id_rename_stays_in_place_and_keeps_backlinks(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """(b) The common case is unchanged: a rename that keeps the id is in place."""
    vault = tmp_path / "vault"
    vault.mkdir()
    a_id, b_id = _linked_pair(test_db, fake_embedder, vault, str(uuid.uuid4()))

    (vault / "b.md").rename(vault / "b2.md")
    state = _move_through_worker(test_db, fake_embedder, vault, vault / "b.md", vault / "b2.md")

    assert state.errors == 0
    assert _rows_at(test_db, "b2.md") == [b_id], "the renamed document lost its row id"
    assert _incoming(test_db, a_id, b_id) == 1, "the incoming backlink was destroyed"


def test_id_less_rename_stays_in_place_and_keeps_backlinks(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """(c) The ruling: an id-less destination is renamed in place.

    A committed row whose file lost its id (a failed write-back) is the state
    ``_sync_one``'s recovery branch identifies BY PATH. The rename keeps the
    row and its backlinks, and the sync then writes the id back.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    a_id, b_id = _linked_pair(test_db, fake_embedder, vault, str(uuid.uuid4()))
    _write(vault / "b.md", {"title": "B"}, "b body\n")  # the id is gone from disk

    (vault / "b.md").rename(vault / "b2.md")
    state = _move_through_worker(test_db, fake_embedder, vault, vault / "b.md", vault / "b2.md")

    assert state.errors == 0
    assert _rows_at(test_db, "b2.md") == [b_id], "an id-less rename did not keep the row"
    assert _incoming(test_db, a_id, b_id) == 1, "an id-less rename destroyed the backlink"
    assert read_frontmatter_id(vault / "b2.md") == b_id, "the sync did not write the id back"


def test_rename_with_a_differently_cased_id_is_the_same_document(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """(d) ``documents.id`` is a uuid: an id differing only in case is the same document."""
    vault = tmp_path / "vault"
    vault.mkdir()
    a_id, b_id = _linked_pair(test_db, fake_embedder, vault, str(uuid.uuid4()).upper())

    (vault / "b.md").rename(vault / "b2.md")
    state = _move_through_worker(test_db, fake_embedder, vault, vault / "b.md", vault / "b2.md")

    assert state.errors == 0
    assert _rows_at(test_db, "b2.md") == [b_id], "a case-only id difference broke the rename"
    assert _incoming(test_db, a_id, b_id) == 1, "a case-only id difference destroyed the backlink"
