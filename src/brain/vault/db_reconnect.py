"""Bounded reconnect for the vault watcher's long-lived DB connections.

The watcher's worker and its graph lane each hold one connection for the life
of the process. Before this module, a Postgres restart left both on a dead
connection, and every later job failed until the watcher was restarted. Plan
and rulings: ``docs/plans/2026-10-22-vault-watcher-db-reconnect.md``.

* **Lost means closed (R1).** After a job runs, raising OR returning, the
  connection is checked: psycopg marks it ``closed`` once the server is gone.
  An exception class would be the wrong test: ``DeadlockDetected`` and
  ``LockNotAvailable`` are ``OperationalError`` subclasses raised on a healthy
  connection. And ``GraphSyncer`` swallows its errors, so a loss can come back
  as a normal return.
* **One re-run, in place (R2).** The job that met the dead connection runs once
  more on a fresh one. Every watcher job is idempotent; a second failure is the
  caller's ordinary error.
* **Capped backoff, woken by shutdown (R3).** The first attempt is immediate.
  Each failed attempt then sleeps 1s, 2s, 4s, and so on up to 30s. Once
  ``stopping()`` is true, a failed attempt raises :class:`ReconnectAbandoned`
  instead of sleeping, so shutdown is never held up by the loop.
* **Transitions only in the log (R7),** by exception class, never message.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

import psycopg

from ..errors import BrainError, describe_failure

logger = logging.getLogger(__name__)

#: First backoff sleep after a failed connect; doubles each attempt up to the cap.
BACKOFF_INITIAL_S = 1.0
#: Longest backoff sleep between connect attempts.
BACKOFF_MAX_S = 30.0

_T = TypeVar("_T")


class ReconnectAbandoned(BrainError):
    """Shutdown was requested while the connection could not be re-opened."""


def backoff_delays() -> Iterator[float]:
    """1, 2, 4, ... seconds, capped. Reads the module constants when called.

    Doubles the previous delay rather than computing ``initial * 2**n``: past
    about 1,024 attempts (8.5 hours down at the cap) that product overflows a
    float, and the loop raised ``OverflowError`` instead of waiting (C10).
    """
    delay, cap = min(BACKOFF_INITIAL_S, BACKOFF_MAX_S), BACKOFF_MAX_S
    while True:
        yield delay
        delay = min(delay * 2, cap)


class ReconnectingConnection:
    """One thread's DB connection, re-opened with capped backoff when it is lost.

    ``sleep(delay)`` waits between connect attempts and should return early on
    shutdown; ``stopping()`` says whether shutdown was requested. Both are
    injected so tests never sleep. Every connection this opens is autocommit.
    ``conn`` is an already-open first connection; without one, the first
    :meth:`call` connects.
    """

    def __init__(
        self,
        conn_factory: Callable[[], psycopg.Connection[Any]],
        *,
        label: str,
        sleep: Callable[[float], object],
        stopping: Callable[[], bool],
        conn: psycopg.Connection[Any] | None = None,
    ) -> None:
        self._factory = conn_factory
        self._label = label
        self._sleep = sleep
        self._stopping = stopping
        self._conn = conn

    def call(self, fn: Callable[[psycopg.Connection[Any]], _T]) -> _T:
        """Run ``fn`` on a live connection; re-run it once if the connection was lost."""
        conn = self._live(cause=None)
        try:
            result = fn(conn)
        except Exception as exc:
            if not conn.closed:
                raise  # a healthy connection: an ordinary job error
            conn = self._live(cause=exc)
        else:
            if not conn.closed:
                return result
            conn = self._live(cause=None)  # the callee swallowed the loss
        return fn(conn)

    def close(self) -> None:
        """Close the current connection, if any. Safe to call twice."""
        if self._conn is not None:
            self._conn.close()  # psycopg's close is idempotent and does not raise

    def _live(self, *, cause: BaseException | None) -> psycopg.Connection[Any]:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        if self._conn is not None:
            self._conn.close()
            logger.warning(
                "vault watcher: %s DB connection lost (%s); reconnecting",
                self._label,
                describe_failure(cause) if cause is not None else "closed",
            )
            lost = True
        else:
            lost = False
        self._conn = self._connect(announced=lost)
        return self._conn

    def _connect(self, *, announced: bool) -> psycopg.Connection[Any]:
        delays = backoff_delays()
        failed = 0
        while True:
            try:
                conn = self._factory()
            except psycopg.Error as exc:
                failed += 1
                logger.debug(
                    "vault watcher: %s DB connect attempt %d failed: %s",
                    self._label,
                    failed,
                    describe_failure(exc),
                )
                if self._stopping():
                    raise ReconnectAbandoned(
                        f"{self._label}: no DB connection at shutdown after "
                        f"{failed} attempt(s) ({describe_failure(exc)})"
                    ) from exc
                if not announced:
                    logger.warning(
                        "vault watcher: %s cannot connect to the DB (%s); retrying",
                        self._label,
                        describe_failure(exc),
                    )
                    announced = True
                self._sleep(next(delays))
                continue
            conn.autocommit = True
            if announced:
                logger.info(
                    "vault watcher: %s DB connection restored after %d failed attempt(s)",
                    self._label,
                    failed,
                )
            return conn
