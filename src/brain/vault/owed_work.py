"""Work the vault watcher owes across a shutdown or a lost connection (RM-26).

The watcher loses three kinds of work today (backlog H-2 and H-4):

* **H-2.** A delete (or a move's source) delivered after the shutdown drain
  closed the watcher is dropped. The next startup ``sync_vault`` reconciles
  upserts, but prunes a deleted file's row only with ``--prune``.
* **H-4a.** A delete whose ``DELETE`` committed just before the connection died
  is re-run on a fresh connection, finds no row, and never removes the
  document's AGE ``Document`` vertex.
* **H-4b.** A delete not applied at shutdown (no DB) is redone only by
  ``brain vault sync --prune``; graph removes the lane could not apply are only
  logged.

This leaf keeps an append-only journal of that work and replays it at the next
start, and it owns the delete of one vault row by path, so the watcher's delete
can capture the row's id BEFORE deleting (:class:`DeleteCapture`). The journal is
a FILE, not a table (RM-26.D1): the watcher owes work precisely when the
database is unreachable. It holds vault-relative paths and document ids, never
content, and is created with mode 0600. Plan:
``docs/plans/2026-10-30-rm-26-owed-watcher-work.md``.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import psycopg

from ..errors import BrainError, describe_failure

if TYPE_CHECKING:
    from .graph_lane import GraphSyncTarget

logger = logging.getLogger(__name__)

#: The journal's file name under ``$BRAIN_HOME/state``.
JOURNAL_NAME = "watcher-owed.jsonl"
#: Suffix a replayed journal is rotated to (one generation kept).
ROTATED_SUFFIX = ".1"

_FILE_MODE = 0o600
_DIR_MODE = 0o700

_SELECT_IDS = "SELECT id::text FROM documents WHERE kind = 'vault' AND vault_path = %s"
_DELETE_ROWS = "DELETE FROM documents WHERE kind = 'vault' AND vault_path = %s RETURNING id::text"
_LIVE_IDS = "SELECT id::text FROM documents WHERE id = ANY(%s::uuid[])"


class InvalidOwedEntry(BrainError):
    """A journal line parsed as JSON but is not an entry this module wrote."""


def default_journal_path() -> Path:
    """``$BRAIN_HOME/state/watcher-owed.jsonl``.

    ``_brain_home_root`` is imported lazily, as in ``log_rotation``, so importing
    the watcher never depends on the config layer.
    """
    from ..config import _brain_home_root

    return _brain_home_root() / "state" / JOURNAL_NAME


def vault_relative_posix(abs_path: Path, vault_path: Path) -> str | None:
    """Return ``abs_path`` relative to the vault as a POSIX string, or None.

    Tries the resolved vault root first, then the lexical one, without touching
    ``abs_path`` on disk: a delete or move source is already gone. Returns
    ``None`` when the path is outside the vault.
    """
    try:
        return abs_path.relative_to(vault_path.resolve()).as_posix()
    except ValueError:
        try:
            return abs_path.relative_to(vault_path).as_posix()
        except ValueError:
            return None


@dataclass
class DeleteCapture:
    """What one delete job saw before deleting, kept across its reconnect re-run.

    ``ids`` are the vault row ids at the path when a run first looked; ``handed``
    are the ids already given to the graph syncer. An id captured and never
    handed is a graph remove still owed.
    """

    ids: list[str] = field(default_factory=list)
    handed: set[str] = field(default_factory=set)

    def unhanded(self) -> list[str]:
        return [doc_id for doc_id in self.ids if doc_id not in self.handed]


def delete_vault_rows(
    conn: psycopg.Connection[Any],
    relative: str,
    *,
    graph_syncer: GraphSyncTarget | None = None,
    capture: DeleteCapture | None = None,
) -> list[str]:
    """Delete the vault-tier row at ``relative`` and hand its id to the graph.

    With ``capture``, the row's id is read BEFORE the delete, on the first run
    that finds one. A re-run whose ``DELETE`` returns nothing (the first run's
    committed and its reply was lost with the connection) still hands every
    captured id to the graph, if it was never handed and has no ``documents``
    row: the "deleted documents only" ruling of ``brain graphrag remove``.

    Returns the ids this call deleted. More than one row means the
    ``documents_vault_path_idx`` uniqueness invariant broke: every row is still
    deleted, and the break is logged at ERROR.
    """
    if capture is not None and not capture.ids:
        capture.ids = [str(row[0]) for row in conn.execute(_SELECT_IDS, (relative,)).fetchall()]
    deleted = [str(row[0]) for row in conn.execute(_DELETE_ROWS, (relative,)).fetchall()]
    if len(deleted) > 1:
        logger.error(
            "vault watcher: deleted %d rows for vault_path=%s — "
            "schema uniqueness invariant broken (expected 0 or 1)",
            len(deleted),
            relative,
        )
    if graph_syncer is None:
        return deleted
    # Wave G1-c: best-effort, never raises; the delete above already committed.
    for doc_id in deleted + _ghost_ids(conn, capture, deleted):
        graph_syncer.remove(conn, doc_id)
        if capture is not None:
            capture.handed.add(doc_id)
    return deleted


def _ghost_ids(
    conn: psycopg.Connection[Any], capture: DeleteCapture | None, deleted: list[str]
) -> list[str]:
    if capture is None:
        return []
    candidates = [doc_id for doc_id in capture.unhanded() if doc_id not in deleted]
    return _without_live_rows(conn, candidates)


def _without_live_rows(conn: psycopg.Connection[Any], ids: list[str]) -> list[str]:
    if not ids:
        return []
    live = {str(row[0]) for row in conn.execute(_LIVE_IDS, (ids,)).fetchall()}
    return [doc_id for doc_id in ids if doc_id not in live]


class OwedJournal:
    """Append owed work to the journal: one ``write`` and one ``fsync`` per line.

    Appends come from the observer thread (a closed watcher's dropped event),
    the worker (unapplied jobs) and the main thread (the lane's leftover
    removes). One ``os.write`` on an ``O_APPEND`` descriptor keeps their lines
    whole. A failed append is logged by class and swallowed: the watcher must
    never die because it could not write down what it owes.
    """

    def __init__(self, path: Path, vault_root: Path) -> None:
        self.path = path
        self._vault_root = vault_root

    def owe_deletes(self, abs_paths: Iterable[Path]) -> None:
        """Owe a delete of each path's vault row (a move owes its SOURCE)."""
        for abs_path in abs_paths:
            relative = vault_relative_posix(abs_path, self._vault_root)
            if relative is not None:
                self._append({"action": "delete", "path": relative})

    def owe_graph_removes(self, document_ids: Iterable[str]) -> None:
        """Owe a graph remove of each document id."""
        for doc_id in document_ids:
            self._append({"action": "graph_remove", "id": doc_id})

    def _append(self, entry: dict[str, str]) -> None:
        line = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, _FILE_MODE)
            try:
                os.fchmod(fd, _FILE_MODE)
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            logger.warning(
                "vault watcher: could not journal owed %s work (%s); it is lost",
                entry["action"],
                describe_failure(exc),
            )
            return
        logger.info("vault watcher: owed one %s to the next start", entry["action"])


@dataclass(frozen=True)
class ReplayReport:
    """Counts only: what one :func:`replay_owed` did.

    ``graph_removes`` counts owed ids; a deleted row's own graph remove is part of
    ``rows_deleted``. ``kept`` counts owed paths whose file is back on disk.
    """

    rows_deleted: int = 0
    graph_removes: int = 0
    kept: int = 0
    skipped: int = 0
    completed: bool = True


def replay_owed(
    conn: psycopg.Connection[Any],
    vault_root: Path,
    graph_lane: GraphSyncTarget | None,
    *,
    journal: Path,
) -> ReplayReport:
    """Apply the journal's owed work, then rotate it to ``<journal>.1``.

    Runs at startup, after the startup sync and before the observer starts.

    * An owed path whose file exists again is NOT deleted (a reappeared file,
      or a journal replayed after the owner restored an older backup). Otherwise
      exactly that path's vault row is deleted, a targeted prune, never a full
      one, and its id goes to the graph lane.
    * An owed id goes to the graph lane only if it has no ``documents`` row.
    * A corrupt or invalid line is skipped with a class-only WARNING.

    A ``psycopg.Error`` keeps the journal whole for the next start (every step is
    idempotent) and returns ``completed=False``; the watcher still starts.
    """
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ReplayReport()
    except OSError as exc:
        logger.warning(
            "vault watcher: could not read the owed-work journal (%s); kept for the next start",
            describe_failure(exc),
        )
        return ReplayReport(completed=False)
    paths, ids, skipped = _parse(lines)
    deleted = kept = removes = 0
    try:
        for relative in paths:
            if (vault_root / relative).exists():
                kept += 1
                continue
            deleted += len(delete_vault_rows(conn, relative, graph_syncer=graph_lane))
        if graph_lane is not None:
            for doc_id in _without_live_rows(conn, ids):
                graph_lane.remove(conn, doc_id)
                removes += 1
    except psycopg.Error as exc:
        logger.warning(
            "vault watcher: owed-work replay stopped (%s); the journal is kept for the next start",
            describe_failure(exc),
        )
        return ReplayReport(deleted, removes, kept, skipped, completed=False)
    try:
        os.replace(journal, journal.with_name(journal.name + ROTATED_SUFFIX))
    except OSError as exc:  # applied; a second replay of it is a no-op
        logger.warning(
            "vault watcher: could not rotate the owed-work journal (%s)", describe_failure(exc)
        )
    logger.info(
        "vault watcher: replayed owed work: %d row(s) deleted, %d graph remove(s), "
        "%d path(s) back on disk, %d line(s) skipped",
        deleted,
        removes,
        kept,
        skipped,
    )
    return ReplayReport(deleted, removes, kept, skipped)


def _parse(lines: list[str]) -> tuple[list[str], list[str], int]:
    """Split the journal into owed paths and ids, deduplicated, in order."""
    paths: list[str] = []
    ids: list[str] = []
    skipped = 0
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            action, value = _entry(json.loads(line))
        except (ValueError, InvalidOwedEntry) as exc:
            skipped += 1
            logger.warning(
                "vault watcher: skipping owed-work journal line %d (%s)",
                number,
                describe_failure(exc),
            )
            continue
        target = paths if action == "delete" else ids
        if value not in target:
            target.append(value)
    return paths, ids, skipped


def _entry(raw: object) -> tuple[str, str]:
    if not isinstance(raw, dict):
        raise InvalidOwedEntry("not an object")
    action = raw.get("action")
    if action == "delete":
        return action, _valid_path(raw.get("path"))
    if action == "graph_remove":
        return action, _valid_id(raw.get("id"))
    raise InvalidOwedEntry("unknown action")


def _valid_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidOwedEntry("path is not a string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".md":
        raise InvalidOwedEntry("path is not a vault-relative markdown path")
    return path.as_posix()


def _valid_id(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidOwedEntry("id is not a string")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise InvalidOwedEntry("id is not a UUID") from exc
