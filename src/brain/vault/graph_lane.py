"""A background lane that runs the vault watcher's graph syncs off its event path.

Production, 2026-10-22: the watcher's startup ``sync_vault`` spent hours in
its graph pass (Ollama concept extraction for new documents) before the
observer and worker started, so vault edits went unwatched. The watcher now
hands its ``graph_syncer`` calls to a :class:`GraphLane`: every graph reconcile
and remove the watcher issues (startup step 7 and every live sync) is queued
and run by ONE thread with its own connection. The watcher stays live, its
worker never blocks on Ollama, and there is still exactly one graph writer per
watcher process. Plan: ``docs/plans/2026-10-22-vault-watcher-startup-blind.md``.

The lane's connection is a :class:`~brain.vault.db_reconnect.ReconnectingConnection`:
after a Postgres restart (or a failed first connect) it reconnects with capped
backoff and re-runs the request once, and ``close`` cuts the backoff short.
Plan: ``docs/plans/2026-10-22-vault-watcher-db-reconnect.md``.
"""

from __future__ import annotations

import functools
import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Any, Literal, Protocol

import psycopg

from ..errors import describe_failure
from .db_reconnect import ReconnectAbandoned, ReconnectingConnection

logger = logging.getLogger(__name__)

_Op = Literal["reconcile", "remove"]

#: How to repair graph removes that were never applied; logged with their ids.
LOST_REMOVES_REPAIR = (
    "their AGE Document vertices stay until `brain graphrag build --force` "
    "(re-extracts concepts for every document when concepts are on); aggregates and "
    "orphan entities heal at the next graph reconcile or with `brain graphrag refresh`"
)


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
    lane's own connection, never on the worker's. It is opened at the first
    request and re-opened after a loss; ``sleep`` (tests only) replaces the
    backoff sleep, which by default wakes on ``close``.
    """

    def __init__(
        self,
        syncer: GraphSyncTarget,
        conn_factory: Callable[[], psycopg.Connection[Any]],
        *,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self._syncer = syncer
        self._link = ReconnectingConnection(
            conn_factory,
            label="graph lane",
            sleep=sleep if sleep is not None else self._sleep_until_closed,
            stopping=self._is_closing,
        )
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
            while (item := self._next()) is not None:
                op, document_id = item
                try:
                    self._link.call(functools.partial(self._apply, op, document_id))
                except ReconnectAbandoned as exc:
                    self._abandon(item, exc)
                    return
                except Exception as exc:  # one bad request must not stop the lane
                    # Class only: a message can quote row values (CLAUDE.md logging rule).
                    logger.warning(
                        "vault watcher: graph %s failed for doc %s: %s",
                        op,
                        document_id,
                        describe_failure(exc),
                    )
        finally:
            self._link.close()

    def _apply(self, op: _Op, document_id: str, conn: psycopg.Connection[Any]) -> None:
        if op == "reconcile":
            self._syncer.reconcile(conn, document_id)
        else:
            self._syncer.remove(conn, document_id)

    def _abandon(self, item: tuple[_Op, str], exc: ReconnectAbandoned) -> None:
        """Shutdown came while the DB was unreachable: name every remove not applied.

        ``close`` already dropped the pending reconciles (the next startup redoes
        them). The removes, the one in flight included, are not redone by a
        startup, so they are logged here: the thread exits, ``close`` returns
        True, and the caller only reports a lane that is still running.
        """
        with self._cond:
            dropped = [item[1]] if item[0] == "remove" else []
            dropped += [d for o, d in self._pending if o == "remove"]
            self._pending.clear()
        logger.error(
            "vault watcher: graph lane has no DB connection at shutdown (%s); "
            "%d graph remove(s) not applied (%s): %s",
            describe_failure(exc),
            len(dropped),
            ", ".join(dropped) or "none",
            LOST_REMOVES_REPAIR,
        )

    def _is_closing(self) -> bool:
        with self._cond:
            return self._closing

    def _sleep_until_closed(self, delay: float) -> None:
        """The default reconnect backoff sleep: ``close`` cuts it short."""
        with self._cond:
            self._cond.wait_for(lambda: self._closing, timeout=delay)
