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
    _reset_age_graph(recorder)

    # Assert — gone, and it took the AGE path to get there.
    assert _age_graph_exists(age_conn, DEFAULT_GRAPH_NAME) is False
    assert recorder.issued("LOAD 'age'") == 1
    assert recorder.issued("drop_graph") == 1


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
