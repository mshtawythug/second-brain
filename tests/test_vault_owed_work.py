"""The owed-work journal and its replay (RM-26, ``brain.vault.owed_work``).

Plan: ``docs/plans/2026-10-30-rm-26-owed-watcher-work.md``. The watcher-level
criteria live in ``tests/test_vault_watch_owed_work.py``; this module pins the
leaf: the journal's file shape and permissions, the replay's targeted prune and
its refusals, and the delete's id capture (H-4a).
"""

from __future__ import annotations

import json
import logging
import os
import stat
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.db import connect_raw
from brain.vault.owed_work import (
    JOURNAL_NAME,
    DeleteCapture,
    OwedJournal,
    default_journal_path,
    delete_vault_rows,
    replay_owed,
    vault_relative_posix,
)
from tests.conftest import TEST_DATABASE_URL


class _Recorder:
    """A ``GraphSyncTarget`` double that records removes."""

    def __init__(self) -> None:
        self.removed: list[str] = []

    def reconcile(self, conn: Any, document_id: str) -> None:
        raise AssertionError("replay never reconciles")

    def remove(self, conn: Any, document_id: str) -> None:
        self.removed.append(document_id)


def _seed(conn: psycopg.Connection[Any], vault_path: str, *, kind: str = "vault") -> str:
    row = conn.execute(
        """
        INSERT INTO documents (title, content, content_hash, content_type, kind, vault_path)
        VALUES (%s, %s, %s, 'note', %s, %s) RETURNING id::text
        """,
        (f"Synthetic {vault_path}", "body\n", f"hash-{uuid.uuid4()}", kind, vault_path),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _row_ids(conn: psycopg.Connection[Any], vault_path: str) -> list[str]:
    rows = conn.execute(
        "SELECT id::text FROM documents WHERE vault_path = %s", (vault_path,)
    ).fetchall()
    return [str(r[0]) for r in rows]


def _write_journal(path: Path, *lines: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _delete(path: str) -> str:
    return json.dumps({"action": "delete", "path": path})


def _remove(doc_id: str) -> str:
    return json.dumps({"action": "graph_remove", "id": doc_id})


# --------------------------------------------------------------------------- #
# The journal file
# --------------------------------------------------------------------------- #


def test_journal_is_created_owner_only(tmp_path: Path) -> None:
    """Security: the journal is 0600 and its state directory 0700."""
    vault = tmp_path / "vault"
    journal = tmp_path / "home" / "state" / JOURNAL_NAME

    OwedJournal(journal, vault).owe_deletes([vault / "a.md"])

    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.parent.stat().st_mode) == 0o700


def test_an_existing_journal_is_narrowed_to_owner_only(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    journal = tmp_path / JOURNAL_NAME
    journal.write_text("")
    journal.chmod(0o644)

    OwedJournal(journal, vault).owe_graph_removes([str(uuid.uuid4())])

    assert stat.S_IMODE(journal.stat().st_mode) == 0o600


def test_entries_are_vault_relative_json_lines(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    journal = tmp_path / JOURNAL_NAME
    doc_id = str(uuid.uuid4())
    owed = OwedJournal(journal, vault)

    owed.owe_deletes([vault / "a.md", vault / "sub" / "b.md", tmp_path / "outside.md"])
    owed.owe_graph_removes([doc_id])

    entries = [json.loads(line) for line in journal.read_text().splitlines()]
    assert entries == [
        {"action": "delete", "path": "a.md"},
        {"action": "delete", "path": "sub/b.md"},
        {"action": "graph_remove", "id": doc_id},
    ]


def test_each_entry_is_one_write_then_an_fsync(tmp_path: Path, mocker: Any) -> None:
    """Concurrent appenders (observer, worker, main) cannot interleave a line."""
    vault = tmp_path / "vault"
    write = mocker.spy(os, "write")
    fsync = mocker.spy(os, "fsync")

    OwedJournal(tmp_path / JOURNAL_NAME, vault).owe_deletes([vault / "a.md", vault / "b.md"])

    payloads = [call.args[1] for call in write.call_args_list]
    assert len(payloads) == 2 and all(p.endswith(b"\n") and p.count(b"\n") == 1 for p in payloads)
    assert fsync.call_count == 2


def test_a_failed_append_is_logged_by_class_and_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "home"
    blocker.write_text("a file where the state directory should be")
    owed = OwedJournal(blocker / "state" / JOURNAL_NAME, tmp_path / "vault")

    with caplog.at_level(logging.WARNING, logger="brain.vault.owed_work"):
        owed.owe_deletes([tmp_path / "vault" / "private-name.md"])

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "could not journal owed delete" in logged
    assert "private-name" not in logged, f"the warning carried the path: {logged!r}"


def test_default_journal_path_is_under_brain_home_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    assert default_journal_path() == tmp_path / "state" / JOURNAL_NAME


def test_vault_relative_posix_refuses_a_path_outside_the_vault(tmp_path: Path) -> None:
    assert vault_relative_posix(tmp_path / "elsewhere.md", tmp_path / "vault") is None


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #


def test_replay_deletes_an_owed_row_and_rotates_the_journal(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    doc_id = _seed(test_db, "gone.md")
    journal = tmp_path / JOURNAL_NAME
    _write_journal(journal, _delete("gone.md"))
    lane = _Recorder()

    report = replay_owed(test_db, vault, lane, journal=journal)

    assert _row_ids(test_db, "gone.md") == []
    assert lane.removed == [doc_id]
    assert (report.rows_deleted, report.completed) == (1, True)
    assert not journal.exists() and (tmp_path / f"{JOURNAL_NAME}.1").exists()


def test_replay_never_deletes_a_path_whose_file_is_back(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Criterion 5: a reappeared file (or a restored backup) keeps its row."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "back.md").write_text("back again\n")
    doc_id = _seed(test_db, "back.md")
    journal = tmp_path / JOURNAL_NAME
    _write_journal(journal, _delete("back.md"))
    lane = _Recorder()

    report = replay_owed(test_db, vault, lane, journal=journal)

    assert _row_ids(test_db, "back.md") == [doc_id]
    assert lane.removed == []
    assert (report.rows_deleted, report.kept) == (0, 1)


def test_replay_never_deletes_an_ingested_row(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Same tier rule as the watcher's live delete: only vault rows."""
    vault = tmp_path / "vault"
    vault.mkdir()
    doc_id = _seed(test_db, "_ingested/krisp/call.md", kind="ingested")
    journal = tmp_path / JOURNAL_NAME
    _write_journal(journal, _delete("_ingested/krisp/call.md"))

    replay_owed(test_db, vault, _Recorder(), journal=journal)

    assert _row_ids(test_db, "_ingested/krisp/call.md") == [doc_id]


def test_a_corrupt_line_is_skipped_by_class_and_the_rest_replays(
    test_db: psycopg.Connection[Any], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Criterion 6."""
    vault = tmp_path / "vault"
    vault.mkdir()
    doc_id = _seed(test_db, "after.md")
    journal = tmp_path / JOURNAL_NAME
    _write_journal(journal, '{"action": "delete", "path": "secret-tit', _delete("after.md"))

    with caplog.at_level(logging.WARNING, logger="brain.vault.owed_work"):
        report = replay_owed(test_db, vault, _Recorder(), journal=journal)

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "skipping owed-work journal line 1 (JSONDecodeError" in logged, logged
    assert "secret-tit" not in logged, f"the warning quoted the line: {logged!r}"
    assert _row_ids(test_db, "after.md") == [] and doc_id
    assert (report.skipped, report.rows_deleted) == (1, 1)


@pytest.mark.parametrize(
    "line",
    [
        '["delete", "a.md"]',
        json.dumps({"action": "upsert", "path": "a.md"}),
        json.dumps({"action": "delete", "path": "/etc/a.md"}),
        json.dumps({"action": "delete", "path": "../outside.md"}),
        json.dumps({"action": "delete", "path": "notes.txt"}),
        json.dumps({"action": "delete", "path": 7}),
        json.dumps({"action": "graph_remove", "id": "not-a-uuid"}),
        json.dumps({"action": "graph_remove", "id": None}),
    ],
)
def test_an_entry_this_module_did_not_write_is_refused(
    test_db: psycopg.Connection[Any], tmp_path: Path, line: str
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    journal = tmp_path / JOURNAL_NAME
    _write_journal(journal, line)
    lane = _Recorder()

    report = replay_owed(test_db, vault, lane, journal=journal)

    assert (report.skipped, report.rows_deleted, lane.removed) == (1, 0, [])


def test_owed_ids_are_removed_only_for_deleted_documents(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    live = _seed(test_db, "live.md")
    (vault / "live.md").write_text("live\n")
    gone = str(uuid.uuid4())
    journal = tmp_path / JOURNAL_NAME
    _write_journal(journal, _remove(live), _remove(gone), _remove(gone.upper()))
    lane = _Recorder()

    report = replay_owed(test_db, vault, lane, journal=journal)

    assert lane.removed == [gone], "a live document's graph was removed, or a dup replayed"
    assert report.graph_removes == 1


def test_owed_ids_are_dropped_without_a_graph_lane(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _seed(test_db, "gone.md")
    journal = tmp_path / JOURNAL_NAME
    _write_journal(journal, _delete("gone.md"), _remove(str(uuid.uuid4())))

    report = replay_owed(test_db, vault, None, journal=journal)

    assert _row_ids(test_db, "gone.md") == []
    assert (report.graph_removes, report.completed) == (0, True)


def test_no_journal_is_a_no_op(test_db: psycopg.Connection[Any], tmp_path: Path) -> None:
    report = replay_owed(test_db, tmp_path, _Recorder(), journal=tmp_path / JOURNAL_NAME)

    assert report.completed and report.rows_deleted == 0


def test_an_unreadable_journal_is_kept(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    journal = tmp_path / JOURNAL_NAME
    journal.mkdir()  # a directory: read_text raises IsADirectoryError

    with caplog.at_level(logging.WARNING, logger="brain.vault.owed_work"):
        report = replay_owed(None, tmp_path, None, journal=journal)  # type: ignore[arg-type]

    assert not report.completed and journal.is_dir()
    assert "could not read the owed-work journal" in caplog.text


def test_a_db_error_keeps_the_journal_for_the_next_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    journal = tmp_path / JOURNAL_NAME
    _write_journal(journal, _delete("gone.md"))
    dead = connect_raw(TEST_DATABASE_URL)
    dead.close()

    with caplog.at_level(logging.WARNING, logger="brain.vault.owed_work"):
        report = replay_owed(dead, vault, _Recorder(), journal=journal)

    assert not report.completed
    assert journal.exists(), "a replay that did not finish rotated the journal away"
    assert "owed-work replay stopped (OperationalError" in caplog.text


def test_a_rotation_failure_does_not_fail_the_replay(
    test_db: psycopg.Connection[Any], tmp_path: Path, mocker: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    journal = tmp_path / JOURNAL_NAME
    _write_journal(journal, _delete("gone.md"))
    mocker.patch("brain.vault.owed_work.os.replace", side_effect=PermissionError(13, "denied"))

    with caplog.at_level(logging.WARNING, logger="brain.vault.owed_work"):
        report = replay_owed(test_db, vault, _Recorder(), journal=journal)

    assert report.completed
    assert "could not rotate the owed-work journal (PermissionError" in caplog.text


# --------------------------------------------------------------------------- #
# The delete's id capture (H-4a)
# --------------------------------------------------------------------------- #


def test_a_first_run_captures_and_hands_the_id_once(
    test_db: psycopg.Connection[Any],
) -> None:
    doc_id = _seed(test_db, "note.md")
    capture = DeleteCapture()
    graph = _Recorder()

    deleted = delete_vault_rows(test_db, "note.md", graph_syncer=graph, capture=capture)

    assert deleted == [doc_id] and graph.removed == [doc_id]
    assert capture.ids == [doc_id] and capture.unhanded() == []


def test_a_rerun_after_a_committed_delete_hands_the_captured_id(
    test_db: psycopg.Connection[Any],
) -> None:
    """H-4a: the row is already gone, so only the capture knows its id."""
    doc_id = _seed(test_db, "note.md")
    capture = DeleteCapture(ids=[doc_id])  # the first run read it, then lost its reply
    test_db.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
    graph = _Recorder()

    deleted = delete_vault_rows(test_db, "note.md", graph_syncer=graph, capture=capture)

    assert deleted == [] and graph.removed == [doc_id]


def test_a_rerun_does_not_hand_an_id_twice_or_remove_a_live_document(
    test_db: psycopg.Connection[Any],
) -> None:
    live = _seed(test_db, "elsewhere.md")
    handed = str(uuid.uuid4())
    capture = DeleteCapture(ids=[handed, live], handed={handed})
    graph = _Recorder()

    delete_vault_rows(test_db, "note.md", graph_syncer=graph, capture=capture)

    assert graph.removed == []


def test_without_a_graph_syncer_the_id_stays_owed(test_db: psycopg.Connection[Any]) -> None:
    doc_id = _seed(test_db, "note.md")
    capture = DeleteCapture()

    delete_vault_rows(test_db, "note.md", capture=capture)

    assert capture.unhanded() == [doc_id]
