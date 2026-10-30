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

from brain.near_dup import shingles
from brain.set_similarity import jaccard
from brain.vault import move_identity
from brain.vault.derived_links.fence import FENCE_END_MARKER, FENCE_START_MARKER
from brain.vault.frontmatter import dump_frontmatter, read_frontmatter_id
from brain.vault.move_identity import is_another_document
from brain.vault.sync import sync_vault
from brain.vault.watch import (
    WatchConfig,
    _Job,
    _schedule_debounced,
    _WatcherState,
    _worker_loop,
)
from tests.watch_link import worker_link


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
    _worker_loop(worker_link(conn), embedder, WatchConfig(vault_path=vault, debounce_ms=10), state)
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
    conn: psycopg.Connection,
    embedder: Any,
    vault: Path,
    b_id_on_disk: str,
    b_body: str = "b body\n",
) -> tuple[str, str]:
    """``a.md`` links to ``b.md``; return (a_id, b_id) after a full sync."""
    a_id = str(uuid.uuid4())
    _write(vault / "a.md", {"id": a_id, "title": "A"}, "see [[B]]\n")
    _write(vault / "b.md", {"id": b_id_on_disk, "title": "B"}, b_body)
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


def test_rename_with_a_non_uuid_id_keeps_the_row_and_its_backlinks(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """(e) An id that is not a UUID cannot name a document: the row must survive.

    Treating it as "another document" would DELETE the row, cascading away
    its backlinks, while the follow-up sync still fails on the bad id. The
    row is kept for the user to fix the id; the sync reports the error.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    a_id, b_id = _linked_pair(test_db, fake_embedder, vault, str(uuid.uuid4()))
    _write(vault / "b.md", {"id": "not-a-uuid", "title": "B"}, "b body\n")

    (vault / "b.md").rename(vault / "b2.md")
    state = _move_through_worker(test_db, fake_embedder, vault, vault / "b.md", vault / "b2.md")

    assert _rows_at(test_db, "b2.md") == [b_id], "a non-UUID id deleted the renamed row"
    assert _incoming(test_db, a_id, b_id) == 1, "a non-UUID id destroyed the backlink"
    assert state.errors == 1, "the sync of the bad id was not reported"


def test_rename_onto_malformed_frontmatter_stays_in_place(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """(f) A destination whose frontmatter does not parse cannot prove anything.

    The id reads as None, so the rename stays in place, keeping the row and
    its backlink; the sync then reports the malformed frontmatter itself.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    a_id, b_id = _linked_pair(test_db, fake_embedder, vault, str(uuid.uuid4()))
    (vault / "b.md").write_text("---\nid: [unclosed\ntitle: B\n---\nb body\n")

    (vault / "b.md").rename(vault / "b2.md")
    _move_through_worker(test_db, fake_embedder, vault, vault / "b.md", vault / "b2.md")

    assert _rows_at(test_db, "b2.md") == [b_id], "malformed frontmatter broke the rename"
    assert _incoming(test_db, a_id, b_id) == 1, "malformed frontmatter destroyed the backlink"


def test_no_row_at_src_is_not_another_document(test_db: psycopg.Connection, tmp_path: Path) -> None:
    """With no vault row at src there is nothing to protect: not another document."""
    vault = tmp_path / "vault"
    vault.mkdir()
    dst = vault / "q.md"
    _write(dst, {"id": str(uuid.uuid4()), "title": "Q"}, "q body\n")

    assert is_another_document(test_db, "never-synced.md", dst) is False, (
        "a src with no vault row was treated as another document"
    )


# --- RM-25: an id-less destination is compared by content (H-1) -------------
#
# Every body below is synthetic: ``_words(stem, n)`` is ``n`` distinct tokens,
# so ``n`` words give exactly ``n - 4`` word 5-gram shingles, and two bodies
# that share a ``k``-word prefix share exactly ``k - 4`` of them (a shingle that
# crosses into the unshared tail holds a token the other body lacks).


def _words(stem: str, n: int) -> str:
    """``n`` distinct synthetic tokens, ``stem0 stem1 ...``, as one line."""
    return " ".join(f"{stem}{i}" for i in range(n)) + "\n"


def _shared_prefix(stem: str, shared: int, total: int, tail: str) -> str:
    """The first ``shared`` tokens of ``_words(stem, ...)``, then fresh ``tail`` tokens."""
    head = [f"{stem}{i}" for i in range(shared)]
    rest = [f"{tail}{i}" for i in range(total - shared)]
    return " ".join(head + rest) + "\n"


class _ManualTimer:
    """A debounce timer the test fires by hand (no threads, no sleeps)."""

    def __init__(self, callback: Any) -> None:
        self._callback = callback

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        pass

    def fire(self) -> None:
        self._callback()


def test_rm25_constants_are_the_d1_thresholds() -> None:
    """RM-25.D1: 8 shingles and a Jaccard of 0.2. A change here is a ruling change."""
    assert move_identity.MIN_IDENTITY_SHINGLES == 8
    assert move_identity.MIN_IDENTITY_JACCARD == 0.2


def test_ac1_id_less_replacement_renamed_in_one_window_does_not_inherit_identity(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """AC1, the H-1 sequence, end to end through the debouncer.

    ``b.md`` (doc B, linked from ``a.md``) is replaced by an unrelated id-less
    file and renamed ``b.md`` → ``c.md`` inside one debounce window. The
    delete, the create and the rename coalesce into ONE move job; the worker
    must delete B's row and sync ``c.md`` as a fresh document, so neither B's
    id nor its incoming backlink lands on the new file.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    a_id, b_id = _linked_pair(test_db, fake_embedder, vault, str(uuid.uuid4()), _words("orig", 40))
    b, c = vault / "b.md", vault / "c.md"
    b.unlink()
    _write(b, {"title": "Replacement"}, _words("fresh", 40))  # id-less, never synced
    b.rename(c)

    timers: list[_ManualTimer] = []

    def _factory(_delay: float, callback: Any) -> _ManualTimer:
        timers.append(_ManualTimer(callback))
        return timers[-1]

    state = _WatcherState(timer_factory=_factory)
    config = WatchConfig(vault_path=vault, debounce_ms=10)
    _schedule_debounced(config, state, "delete", b)
    _schedule_debounced(config, state, "upsert", b)
    _schedule_debounced(config, state, "move", b, c)
    timers[-1].fire()
    assert state.jobs.qsize() == 1, "fixture: the window must coalesce into one move job"
    state.jobs.put(None)
    _worker_loop(worker_link(test_db), fake_embedder, config, state)

    assert state.errors == 0
    rows = _rows_at(test_db, "c.md")
    assert len(rows) == 1 and rows[0] != b_id, "the replacement inherited the old note's id"
    gone = test_db.execute("SELECT count(*) FROM documents WHERE id = %s", (b_id,)).fetchone()
    assert gone is not None and gone[0] == 0, "the replaced note's row survived"
    assert _incoming(test_db, a_id, rows[0]) == 0, "the replacement inherited the backlink"
    assert read_frontmatter_id(c) == rows[0], "the fresh sync did not write its own id back"


def test_ac2_id_less_atomic_save_of_the_same_content_keeps_the_id(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """AC2: an editor's atomic save of the same note (id lost on disk) keeps its row."""
    vault = tmp_path / "vault"
    vault.mkdir()
    body = _words("note", 40)
    a_id, b_id = _linked_pair(test_db, fake_embedder, vault, str(uuid.uuid4()), body)
    _write(vault / "b.md", {"title": "B"}, body)  # same content, the id is gone

    (vault / "b.md").rename(vault / "b2.md")
    state = _move_through_worker(test_db, fake_embedder, vault, vault / "b.md", vault / "b2.md")

    assert state.errors == 0
    assert _rows_at(test_db, "b2.md") == [b_id], "an atomic save of the same note lost its id"
    assert _incoming(test_db, a_id, b_id) == 1, "an atomic save destroyed the backlink"


def test_ac3_rewrite_keeping_exactly_a_fifth_of_the_shingles_keeps_the_id(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """AC3, the documented boundary: Jaccard exactly 0.2 is still the same document.

    120 shingles each side, 40 shared: 40 / (120 + 120 - 40) = 0.2.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    a_id, b_id = _linked_pair(test_db, fake_embedder, vault, str(uuid.uuid4()), _words("orig", 124))
    _write(vault / "b.md", {"title": "B"}, _shared_prefix("orig", 44, 124, "edit"))

    (vault / "b.md").rename(vault / "b2.md")
    state = _move_through_worker(test_db, fake_embedder, vault, vault / "b.md", vault / "b2.md")

    assert state.errors == 0
    assert _rows_at(test_db, "b2.md") == [b_id], "a rewrite at Jaccard 0.2 lost its id"
    assert _incoming(test_db, a_id, b_id) == 1, "a rewrite at Jaccard 0.2 lost its backlink"


def test_ac3_rewrite_just_below_a_fifth_is_another_document(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """AC3's other side: 39 of 120 shared is 39 / 201 < 0.2, so another document."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _write(vault / "p.md", {"id": str(uuid.uuid4()), "title": "P"}, _words("orig", 124))
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    dst = vault / "q.md"
    _write(dst, {"title": "Q"}, _shared_prefix("orig", 43, 124, "edit"))

    assert is_another_document(test_db, "p.md", dst) is True


def test_ac4_short_id_less_destination_keeps_the_id_and_backlinks(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """AC4: a 7-shingle destination is too short to compare, even against a long row."""
    vault = tmp_path / "vault"
    vault.mkdir()
    a_id, b_id = _linked_pair(test_db, fake_embedder, vault, str(uuid.uuid4()), _words("orig", 40))
    _write(vault / "b.md", {"title": "B"}, _words("short", 11))  # 7 shingles, unrelated

    (vault / "b.md").rename(vault / "b2.md")
    state = _move_through_worker(test_db, fake_embedder, vault, vault / "b.md", vault / "b2.md")

    assert state.errors == 0
    assert _rows_at(test_db, "b2.md") == [b_id], "a short id-less rename lost its id"
    assert _incoming(test_db, a_id, b_id) == 1, "a short id-less rename lost its backlink"


def test_ac4_short_stored_row_keeps_the_id_and_backlinks(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """AC4: a 7-shingle stored row is too short to compare, even against a long file."""
    vault = tmp_path / "vault"
    vault.mkdir()
    a_id, b_id = _linked_pair(test_db, fake_embedder, vault, str(uuid.uuid4()), _words("short", 11))
    _write(vault / "b.md", {"title": "B"}, _words("long", 40))  # unrelated, id-less

    (vault / "b.md").rename(vault / "b2.md")
    state = _move_through_worker(test_db, fake_embedder, vault, vault / "b.md", vault / "b2.md")

    assert state.errors == 0
    assert _rows_at(test_db, "b2.md") == [b_id], "a short stored row lost its id"
    assert _incoming(test_db, a_id, b_id) == 1, "a short stored row lost its backlink"


def test_ac4_empty_id_less_note_keeps_the_id_and_backlinks(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """AC4: an empty note on both sides (``jaccard`` of two empty sets is 0.0) keeps its id."""
    vault = tmp_path / "vault"
    vault.mkdir()
    a_id, b_id = _linked_pair(test_db, fake_embedder, vault, str(uuid.uuid4()), "")
    _write(vault / "b.md", {"title": "B"}, "")

    (vault / "b.md").rename(vault / "b2.md")
    state = _move_through_worker(test_db, fake_embedder, vault, vault / "b.md", vault / "b2.md")

    assert state.errors == 0
    assert _rows_at(test_db, "b2.md") == [b_id], "an empty id-less rename lost its id"
    assert _incoming(test_db, a_id, b_id) == 1, "an empty id-less rename lost its backlink"


def test_ac4_floor_is_eight_shingles_on_each_side(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """The floor's boundary: unrelated 8-shingle notes compare, 7-shingle ones do not."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _write(vault / "p8.md", {"id": str(uuid.uuid4()), "title": "P8"}, _words("old", 12))
    _write(vault / "p7.md", {"id": str(uuid.uuid4()), "title": "P7"}, _words("old", 11))
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    q8, q7 = vault / "q8.md", vault / "q7.md"
    _write(q8, {"title": "Q8"}, _words("new", 12))
    _write(q7, {"title": "Q7"}, _words("new", 11))

    assert is_another_document(test_db, "p8.md", q8) is True, "8 shingles each must compare"
    assert is_another_document(test_db, "p7.md", q7) is False, "7 shingles each must not"


def test_id_bearing_same_id_rewrite_is_not_compared_by_content(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """PR #52's ruling stands: a matching id wins over an entirely rewritten body."""
    vault = tmp_path / "vault"
    vault.mkdir()
    b_disk = str(uuid.uuid4())
    a_id, b_id = _linked_pair(test_db, fake_embedder, vault, b_disk, _words("orig", 40))
    _write(vault / "b.md", {"id": b_disk, "title": "B"}, _words("rewrite", 40))

    (vault / "b.md").rename(vault / "b2.md")
    state = _move_through_worker(test_db, fake_embedder, vault, vault / "b.md", vault / "b2.md")

    assert state.errors == 0
    assert _rows_at(test_db, "b2.md") == [b_id], "a same-id rewrite lost its row"
    assert _incoming(test_db, a_id, b_id) == 1, "a same-id rewrite lost its backlink"


def test_id_bearing_different_id_with_identical_content_is_another_document(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """PR #52's ruling stands: a different id is another document, whatever the body says."""
    vault = tmp_path / "vault"
    vault.mkdir()
    body = _words("same", 40)
    _write(vault / "p.md", {"id": str(uuid.uuid4()), "title": "P"}, body)
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    dst = vault / "q.md"
    _write(dst, {"id": str(uuid.uuid4()), "title": "Q"}, body)

    assert is_another_document(test_db, "p.md", dst) is True


def test_never_synced_id_less_rename_syncs_fresh_with_no_row_at_src(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """An id-less note that was never synced has no row at src: nothing to compare.

    ``is_another_document`` is False (there is no row to protect or delete),
    and the worker syncs dst as a new document with its own id, without error.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    p, q = vault / "p.md", vault / "q.md"
    _write(p, {"title": "Never synced"}, _words("fresh", 40))
    p.rename(q)

    assert is_another_document(test_db, "p.md", q) is False
    state = _move_through_worker(test_db, fake_embedder, vault, p, q)

    assert state.errors == 0
    rows = _rows_at(test_db, "q.md")
    assert len(rows) == 1, "the never-synced note was not indexed at its destination"
    assert read_frontmatter_id(q) == rows[0], "the fresh sync did not write its own id back"


def _derived_fence(links: int) -> str:
    """A linker-shaped derived-links fence: the heading and ``links`` synthetic bullets."""
    bullets = "".join(f"- [[partner-{i}|Partner {i}]] *(shared_thread)*\n" for i in range(links))
    return (
        f"{FENCE_START_MARKER}\n## Related (auto-generated, do not edit)\n\n"
        f"{bullets}{FENCE_END_MARKER}\n"
    )


def test_derived_fence_on_the_destination_does_not_count_against_the_note(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """Deviation 1: dst's body is fence-stripped, as ``documents.content`` is.

    aud-90's case: a 20-word note (16 shingles) loses its id on disk and gains
    a derived-links fence of 20 links. Counted, the fence's shingles drop the
    Jaccard to about 0.15, below 0.2; stripped, the bodies are equal (1.0).
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    body = _words("note", 20)
    a_id, b_id = _linked_pair(test_db, fake_embedder, vault, str(uuid.uuid4()), body)
    fenced = f"{body}\n{_derived_fence(20)}"
    raw = jaccard(shingles(body), shingles(fenced))
    assert raw < move_identity.MIN_IDENTITY_JACCARD, f"fixture: the fence must bite ({raw:.3f})"
    _write(vault / "b.md", {"title": "B"}, fenced)  # id-less, the same note plus a fence

    (vault / "b.md").rename(vault / "b2.md")
    state = _move_through_worker(test_db, fake_embedder, vault, vault / "b.md", vault / "b2.md")

    assert state.errors == 0
    assert _rows_at(test_db, "b2.md") == [b_id], "the derived fence made the note look new"
    assert _incoming(test_db, a_id, b_id) == 1, "the derived fence cost the note its backlink"
