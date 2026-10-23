"""Shared helpers for the graph tests that force an interleaving across connections.

Importable as ``from tests.graph_concurrency import ...``. Not a ``test_*``
module, so pytest does not collect it. Used by ``tests/test_graphrag_deadlock.py``
and ``tests/test_graphrag_alias_doc_lock.py``. Each test runs a writer on a
thread with its own connection, parks it on a real lock, and polls
``pg_stat_activity`` (through ``tests.thread_wait.wait_for``) until a backend is
waiting. No fixed sleep stands in for "long enough".
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any

import psycopg

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

#: Upper bound on any single statement in a worker. A regression that turns a
#: deadlock into an unbounded wait fails here, not by hanging CI.
STATEMENT_TIMEOUT = "20s"

#: Deadline for every Event wait and thread join, in seconds.
WAIT_S = 15.0


class Worker(threading.Thread):
    """Run ``fn`` on a thread; keep its exception for the test."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__(daemon=True)
        self._fn = fn
        self.error: BaseException | None = None
        self.finished = threading.Event()

    def run(self) -> None:
        try:
            self._fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            self.error = exc
        finally:
            self.finished.set()


def connect_worker() -> psycopg.Connection[Any]:
    """An autocommit connection with a ``statement_timeout`` backstop."""
    conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=5)
    conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
    return conn


def is_waiting_on_lock(observer: psycopg.Connection[Any], pid: int) -> bool:
    """True iff backend ``pid`` is currently waiting on a heavyweight lock."""
    row = observer.execute(
        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
    ).fetchone()
    return row is not None and row[0] == "Lock"


def join_workers(*workers: Worker) -> None:
    """Join every worker; a worker still alive after the deadline is a hang."""
    for worker in workers:
        worker.join(WAIT_S * 2)
        assert not worker.is_alive(), "a graph writer hung"
