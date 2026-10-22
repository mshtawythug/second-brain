"""A background lane that runs the vault watcher's graph syncs off its event path.

Production, 2026-10-22: the watcher's startup ``sync_vault`` spent hours in
its graph pass (Ollama concept extraction for new documents) before the
observer and worker started, so vault edits went unwatched. The watcher now
hands its ``graph_syncer`` calls to a :class:`GraphLane`: every graph reconcile
and remove the watcher issues (startup step 7 and every live sync) is queued
and run by ONE thread with its own connection. The watcher stays live, its
worker never blocks on Ollama, and there is still exactly one graph writer per
watcher process. Plan: ``docs/plans/2026-10-22-vault-watcher-startup-blind.md``.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Any, Literal, Protocol

import psycopg

from ..errors import describe_failure

logger = logging.getLogger(__name__)

_Op = Literal["reconcile", "remove"]


class GraphSyncTarget(Protocol):
    """What the vault sync and the watcher call; ``GraphSyncer`` satisfies it."""

    def reconcile(self, conn: psycopg.Connection[Any], document_id: str) -> None: ...

    def remove(self, conn: psycopg.Connection[Any], document_id: str) -> None: ...


class GraphLane:
    """Queue graph requests; run them in FIFO order on one thread, own connection.

    * ``reconcile`` for a document whose LAST pending request is already a
      reconcile is dropped: the pending one reads the latest DB state when it
      runs. A reconcile requested while that document's reconcile is IN FLIGHT
      is queued again, so an edit landing mid-reconcile is reconciled after it.
    * ``remove`` is never deduped and keeps its place relative to reconciles.
    * ``close`` drops pending reconciles (the next startup sync reconciles every
      walked document again) and ignores later requests. Pending removes are
      drained only if the in-flight reconcile finishes within the timeout; a
      later startup does NOT redo them, so any left undrained are reported by
      :meth:`undrained_removes` for the caller to log.

    The caller's connection is ignored on purpose: graph work runs on the
    lane's own connection, never on the worker's.
    """

    def __init__(
        self,
        syncer: GraphSyncTarget,
        conn_factory: Callable[[], psycopg.Connection[Any]],
    ) -> None:
        self._syncer = syncer
        self._conn_factory = conn_factory
        self._pending: deque[tuple[_Op, str]] = deque()
        self._cond = threading.Condition()
        self._closing = False
        self._thread = threading.Thread(
            target=self._run, name="brain-vault-watcher-graph", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def reconcile(self, conn: psycopg.Connection[Any], document_id: str) -> None:
        self._submit("reconcile", document_id)

    def remove(self, conn: psycopg.Connection[Any], document_id: str) -> None:
        self._submit("remove", document_id)

    def _submit(self, op: _Op, document_id: str) -> None:
        with self._cond:
            if self._closing:
                return
            if op == "reconcile":
                last = next((o for o, d in reversed(self._pending) if d == document_id), None)
                if last == "reconcile":
                    return  # the pending reconcile will read the latest state
            self._pending.append((op, document_id))
            self._cond.notify()

    def close(self, timeout: float) -> bool:
        """Stop accepting work, drop pending reconciles, then join (draining removes).

        Returns True if the lane thread exited within ``timeout``. An in-flight
        reconcile is allowed to finish; if it outlives ``timeout`` the daemon
        thread is left to finish (or to die with the process, which rolls its
        transaction back), and the removes still queued behind it are NOT
        applied: :meth:`undrained_removes` names them.
        """
        with self._cond:
            self._closing = True
            self._pending = deque(item for item in self._pending if item[0] == "remove")
            self._cond.notify()
        if self._thread.is_alive():
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def undrained_removes(self) -> list[str]:
        """Document ids of removes still queued (not applied), in queue order."""
        with self._cond:
            return [d for o, d in self._pending if o == "remove"]

    def _next(self) -> tuple[_Op, str] | None:
        with self._cond:
            while not self._pending and not self._closing:
                self._cond.wait()
            return self._pending.popleft() if self._pending else None

    def _run(self) -> None:
        try:
            conn = self._conn_factory()
        except psycopg.Error as exc:
            # Without a connection the lane can do nothing: close it (before
            # logging, so the log line proves it) so later submits are no-ops,
            # and name the removes dropped with it: a later startup won't redo them.
            with self._cond:
                self._closing = True
                dropped = [d for o, d in self._pending if o == "remove"]
                self._pending.clear()
            logger.error(
                "vault watcher: graph lane could not connect: %s; graph sync is off until "
                "the watcher restarts; %d graph remove(s) not applied (%s)",
                describe_failure(exc),
                len(dropped),
                ", ".join(dropped) or "none",
            )
            return
        try:
            conn.autocommit = True
            while (item := self._next()) is not None:
                op, document_id = item
                try:
                    if op == "reconcile":
                        self._syncer.reconcile(conn, document_id)
                    else:
                        self._syncer.remove(conn, document_id)
                except Exception as exc:  # one bad request must not stop the lane
                    # Class only: a message can quote row values (CLAUDE.md logging rule).
                    logger.warning(
                        "vault watcher: graph %s failed for doc %s: %s",
                        op,
                        document_id,
                        describe_failure(exc),
                    )
        finally:
            conn.close()  # psycopg's close is idempotent and does not raise
