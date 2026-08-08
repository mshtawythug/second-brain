"""Tests for the per-test Apache AGE reset in :mod:`tests.conftest`.

Regression cover for C17 (2026-08-07). ``_reset_age_graph`` runs once per DB
test from ``_truncate_reset``. It used to issue ``CREATE EXTENSION IF NOT
EXISTS age CASCADE`` and ``LOAD 'age'`` **unconditionally**, so every test
backend loaded AGE and built a per-backend label cache — including the large
majority of tests that never touch the graph. AGE invalidates that cache badly
when graphs are dropped and recreated across sessions, and the result is::

    ERROR:  label (relation) cache corrupted
    FATAL:  terminating connection because protocol synchronization was lost

The connection dies, so the failure lands in the NEXT test's setup and indicts
an innocent bystander. 140 such events sit in the test container's log,
clustered on 2026-07-26, 2026-08-06 and 2026-08-07 — the first two being the
dates ``tests/db_lock.py`` cites for the mysterious-disjoint-failure incidents.

**On what these tests do and do not claim.** The corruption itself is
probabilistic — it depends on which backend cached what and when another
session dropped a graph — so there is no honest way to assert "the corruption
no longer happens" in a unit test. What IS deterministic, and what these tests
pin, is the *cause*: a reset that has no graph to drop must not touch AGE at
all. Bounding the cause is the substitute for reproducing the symptom, and the
distinction is deliberate.

Measured A/B over a fixed 206-test slice, same database, pre-fix body replayed
against post-fix body:

===================  ======  =====
statistic            before  after
===================  ======  =====
resets                  141    141
statements issued       729    378
``LOAD 'age'``          141     24
``CREATE EXTENSION``    141      0
``drop_graph``           24     24
===================  ======  =====

``drop_graph`` is unchanged, which is the point: this removes waste, not
cleanup.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from brain.db import DEFAULT_GRAPH_NAME, bootstrap_age
from tests.conftest import (
    TEST_DATABASE_URL,
    _age_graph_exists,
    _open_age_connection,
    _reset_age_graph,
    _truncate_reset,
)

pytestmark = pytest.mark.integration


class RecordingConnection:
    """Delegating proxy that records every statement executed through it.

    A pure test double passed in as an argument — production code is never
    reopened or patched. Everything it does not implement falls through to the
    real connection via ``__getattr__``.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        self.statements: list[str] = []

    def execute(self, sql: Any, params: Any = None) -> Any:
        self.statements.append(str(sql))
        if params is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def issued(self, needle: str) -> int:
        """How many recorded statements contain ``needle`` (case-insensitive)."""
        low = needle.lower()
        return sum(1 for s in self.statements if low in s.lower())


class _PoisonedConnection:
    """A connection that dies the way an AGE-corrupted backend dies.

    ``LOAD`` succeeds and the ``drop_graph`` raises, because that is the real
    sequence: the library loads fine and the label cache only detonates once the
    graph is touched.
    """

    def __init__(self) -> None:
        self.closed = False

    def execute(self, sql: Any, params: Any = None) -> Any:
        if "drop_graph" in str(sql):
            raise psycopg.errors.InternalError("label (relation) cache corrupted")
        return None

    def close(self) -> None:
        self.closed = True


class _RaisingConnection:
    """A connection whose every statement raises a given, non-AGE error."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def execute(self, sql: Any, params: Any = None) -> Any:
        raise self._error

    def close(self) -> None:
        return None


class _AgeConnSpy:
    """Factory + recorder for the throwaway connection ``_reset_age_graph`` opens.

    Callable so it can be passed straight in as ``open_age_conn``; it hands back
    a :class:`RecordingConnection` over a real connection, so the AGE work still
    genuinely happens and the statements are still observable.
    """

    def __init__(self) -> None:
        self.opened: list[RecordingConnection] = []

    def __call__(self) -> Any:
        recorder = RecordingConnection(_open_age_connection())
        self.opened.append(recorder)
        return recorder

    def issued(self, needle: str) -> int:
        """How many statements across every opened connection match ``needle``."""
        return sum(r.issued(needle) for r in self.opened)


@pytest.fixture()
def age_conn_spy() -> _AgeConnSpy:
    """Observe the AGE connection without patching anything — pure injection."""
    return _AgeConnSpy()


@pytest.fixture()
def age_conn() -> Iterator[psycopg.Connection]:
    """Autocommit connection to the test DB with no ``brain_graph`` present.

    Autocommit because AGE catalog DDL wants explicit commits under psycopg v3,
    matching how ``conftest`` drives the reset.
    """
    with psycopg.connect(TEST_DATABASE_URL, connect_timeout=5) as conn:
        conn.autocommit = True
        _reset_age_graph(conn)  # ensure a clean starting point
        yield conn
        _reset_age_graph(conn)  # leave nothing behind for the next test


def test_reset_touches_age_not_at_all_when_there_is_no_graph(
    age_conn: psycopg.Connection,
) -> None:
    """The whole point: no graph => no ``LOAD``, no ``CREATE EXTENSION``.

    Every one of those on a backend that will never use AGE was pure exposure:
    it populates a label cache that another session's ``drop_graph`` can then
    invalidate into a connection-killing error.
    """
    # Arrange
    recorder = RecordingConnection(age_conn)
    assert _age_graph_exists(age_conn) is False

    # Act
    _reset_age_graph(recorder)

    # Assert — the cause is gone, stated as three separate invariants so a
    # failure names which one regressed.
    assert recorder.issued("LOAD 'age'") == 0
    assert recorder.issued("CREATE EXTENSION") == 0
    assert recorder.issued("drop_graph") == 0
    # And it stayed cheap: a probe or two, not a DDL sequence.
    assert len(recorder.statements) <= 2


def test_per_test_reset_issues_no_age_load_for_a_graph_free_test(
    age_conn: psycopg.Connection,
) -> None:
    """The bound the fixture actually has to honour, at the real entry point.

    ``_truncate_reset`` is what every non-``fresh_schema`` test runs. Pinning
    the invariant here rather than only on ``_reset_age_graph`` means an
    accidental re-introduction of unconditional AGE work anywhere in the
    per-test path fails a test.
    """
    # Arrange
    recorder = RecordingConnection(age_conn)

    # Act
    _truncate_reset(recorder)

    # Assert
    assert recorder.issued("LOAD 'age'") == 0
    assert recorder.issued("CREATE EXTENSION") == 0
    # It still did its real job.
    assert recorder.issued("TRUNCATE") >= 1


def test_reset_still_drops_a_graph_that_does_exist(
    age_conn: psycopg.Connection,
    age_conn_spy: _AgeConnSpy,
) -> None:
    """Cleanup semantics are unchanged — this removes waste, not correctness.

    Without this, "don't touch AGE" could be satisfied by never cleaning up,
    which would leak graph state across tests: a worse bug than the one being
    fixed.
    """
    # Arrange — create the canonical graph the way production does.
    bootstrap_age(age_conn)
    assert _age_graph_exists(age_conn, DEFAULT_GRAPH_NAME) is True
    recorder = RecordingConnection(age_conn)

    # Act
    _reset_age_graph(recorder, open_age_conn=age_conn_spy)

    # Assert — gone, and it took the AGE path to get there.
    assert _age_graph_exists(age_conn, DEFAULT_GRAPH_NAME) is False
    assert age_conn_spy.issued("LOAD 'age'") == 1
    assert age_conn_spy.issued("drop_graph") == 1


def test_the_callers_connection_never_loads_age_even_when_a_graph_exists(
    age_conn: psycopg.Connection,
    age_conn_spy: _AgeConnSpy,
) -> None:
    """C17 bounded how OFTEN AGE loads; this bounds WHICH backend pays.

    The early-out cannot help the ~17% of resets that do have a graph to drop,
    and on those ``conn`` is the connection about to be handed to the test. AGE
    poisons the backend that ran ``drop_graph`` at its *next* statement, so
    loading AGE there hands the test a connection primed to die on its first
    query — which is exactly how
    ``test_cli_agent_flag::test_search_and_ingest_agree_on_the_env_var`` failed
    on 2026-08-07: both CLI invocations returned 0, then the verification
    ``SELECT`` came back on a dead connection.

    Reproduced at ~1 run in 12 (2 files, 20 tests) before this; 0 in 40 after.
    """
    # Arrange
    bootstrap_age(age_conn)
    assert _age_graph_exists(age_conn, DEFAULT_GRAPH_NAME) is True
    recorder = RecordingConnection(age_conn)

    # Act
    _reset_age_graph(recorder, open_age_conn=age_conn_spy)

    # Assert — the caller's connection is untouched by AGE...
    assert recorder.issued("LOAD 'age'") == 0
    assert recorder.issued("drop_graph") == 0
    assert recorder.issued("search_path") == 0
    # ...and is still alive, which is the property the failing tests needed.
    assert age_conn.execute("SELECT 1").fetchone() == (1,)


def test_a_poisoned_age_connection_is_retried_on_a_fresh_one(
    age_conn: psycopg.Connection,
) -> None:
    """A corrupted attempt must degrade to a retry, not to a dead test.

    The corruption also leaves the graph undropped, so without the retry the
    next reset takes the AGE path again and can be poisoned again — the
    self-sustaining loop behind two consecutive failures in one file.

    The first attempt is made to fail the way Postgres actually fails it
    (``InternalError`` carrying AGE's message); the second gets a real
    connection, so the assertion is that the graph really is gone.
    """
    # Arrange
    bootstrap_age(age_conn)
    assert _age_graph_exists(age_conn, DEFAULT_GRAPH_NAME) is True
    attempts: list[str] = []

    def flaky_first_attempt() -> Any:
        if not attempts:
            attempts.append("poisoned")
            return _PoisonedConnection()
        attempts.append("fresh")
        return _open_age_connection()

    # Act
    _reset_age_graph(age_conn, open_age_conn=flaky_first_attempt)

    # Assert
    assert attempts == ["poisoned", "fresh"]
    assert _age_graph_exists(age_conn, DEFAULT_GRAPH_NAME) is False


def test_an_unrecoverable_error_is_not_retried_into_silence(
    age_conn: psycopg.Connection,
) -> None:
    """Only the corruption is absorbed — a real failure must still surface.

    Retrying is a narrow concession to one upstream bug. If it swallowed
    everything, a graph that genuinely refuses to drop would leak into the next
    test as a mystery instead of failing here.
    """
    # Arrange
    bootstrap_age(age_conn)
    boom = psycopg.ProgrammingError("permission denied for schema ag_catalog")

    def always_raises() -> Any:
        return _RaisingConnection(boom)

    # Act / Assert
    with pytest.raises(psycopg.ProgrammingError):
        _reset_age_graph(age_conn, open_age_conn=always_raises)


def test_graph_existence_probe_needs_neither_extension_nor_load(
    age_conn: psycopg.Connection,
) -> None:
    """The probe must be answerable without doing the thing it guards.

    ``_age_graph_exists`` reads ``ag_catalog.ag_graph`` — an ordinary table —
    behind a ``to_regclass`` NULL check, so it works on a database that has
    never seen AGE instead of erroring on a missing schema. If this ever
    required ``LOAD 'age'``, the guard would reintroduce exactly the churn it
    exists to avoid.
    """
    # Arrange
    recorder = RecordingConnection(age_conn)

    # Act
    result = _age_graph_exists(recorder)

    # Assert
    assert result is False
    assert recorder.issued("LOAD") == 0
    assert recorder.issued("CREATE EXTENSION") == 0
