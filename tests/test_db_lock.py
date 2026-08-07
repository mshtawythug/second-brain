"""Tests for the whole-suite test-database exclusivity lock.

Regression cover for 2026-07-26: two concurrent pytest sessions sharing one
test database truncated each other's fixtures, yielding two disjoint and
entirely misleading failure sets. See :mod:`tests.db_lock`.

These tests use a **private lock key**, never :data:`db_lock.SUITE_LOCK_KEY`, so
they can never contend with the real suite lock held by the session fixture
while they themselves are running.
"""
from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from tests.conftest import TEST_DATABASE_URL
from tests.db_lock import (
    SUITE_LOCK_KEY,
    concurrent_suite_message,
    release_suite_lock,
    try_acquire_suite_lock,
)

#: A key private to this module — deliberately not SUITE_LOCK_KEY.
PRIVATE_KEY = 0x7465737420

pytestmark = pytest.mark.integration


@pytest.fixture()
def other_session() -> Iterator[psycopg.Connection]:
    """A second, independent connection standing in for a rival pytest run."""
    with psycopg.connect(TEST_DATABASE_URL, connect_timeout=5) as conn:
        yield conn


def test_first_session_acquires_the_lock(other_session: psycopg.Connection) -> None:
    """The first suite to ask gets the lock."""
    # Act
    with other_session.cursor() as cur:
        acquired = try_acquire_suite_lock(cur, key=PRIVATE_KEY)

        # Assert
        assert acquired is True

        # Teardown
        release_suite_lock(cur, key=PRIVATE_KEY)


def test_second_session_is_refused_while_first_holds_it() -> None:
    """A rival session is refused rather than blocked — this is the whole point.

    ``pg_try_advisory_lock`` must not wait: a blocking acquire would hang the
    second suite for the ~27 minutes the first one takes, which is no better
    than the corruption it replaces.
    """
    # Arrange — holder takes the lock and keeps its session open.
    with psycopg.connect(TEST_DATABASE_URL, connect_timeout=5) as holder:
        with holder.cursor() as cur:
            assert try_acquire_suite_lock(cur, key=PRIVATE_KEY) is True

        # Act — a genuinely separate session attempts the same key.
        with (
            psycopg.connect(TEST_DATABASE_URL, connect_timeout=5) as rival,
            rival.cursor() as rival_cur,
        ):
            refused = try_acquire_suite_lock(rival_cur, key=PRIVATE_KEY)

        # Assert
        assert refused is False

        # Teardown
        with holder.cursor() as cur:
            release_suite_lock(cur, key=PRIVATE_KEY)


def test_lock_is_reacquirable_after_release() -> None:
    """Releasing hands the database back to the next suite."""
    # Arrange
    with (
        psycopg.connect(TEST_DATABASE_URL, connect_timeout=5) as holder,
        holder.cursor() as cur,
    ):
        assert try_acquire_suite_lock(cur, key=PRIVATE_KEY) is True
        assert release_suite_lock(cur, key=PRIVATE_KEY) is True

    # Act — a fresh session should now succeed.
    with (
        psycopg.connect(TEST_DATABASE_URL, connect_timeout=5) as nxt,
        nxt.cursor() as cur,
    ):
        reacquired = try_acquire_suite_lock(cur, key=PRIVATE_KEY)

        # Assert
        assert reacquired is True

        # Teardown
        release_suite_lock(cur, key=PRIVATE_KEY)


def test_lock_is_released_when_the_holding_session_dies() -> None:
    """A crashed run must not leave the database locked forever.

    Postgres session-level advisory locks are released on disconnect, which is
    what makes this safe to use unattended. Closing the connection stands in
    for the process dying.
    """
    # Arrange — take the lock, then drop the connection entirely.
    holder = psycopg.connect(TEST_DATABASE_URL, connect_timeout=5)
    with holder.cursor() as cur:
        assert try_acquire_suite_lock(cur, key=PRIVATE_KEY) is True
    holder.close()

    # Act
    with (
        psycopg.connect(TEST_DATABASE_URL, connect_timeout=5) as successor,
        successor.cursor() as cur,
    ):
        acquired = try_acquire_suite_lock(cur, key=PRIVATE_KEY)

        # Assert
        assert acquired is True

        # Teardown
        release_suite_lock(cur, key=PRIVATE_KEY)


def test_suite_key_differs_from_the_rebuild_orchestrator_key() -> None:
    """The suite lock must not collide with ``brain-rebuild``'s lock.

    They guard different databases; sharing a key would make a rebuild and a
    test run block each other for no reason.
    """
    # IMPORTED, not retyped. A collision test that hardcodes the value it is
    # checking against cannot detect the collision it exists for: changing
    # ``maintenance._REBUILD_LOCK_KEY`` would leave this green.
    from brain.maintenance import _REBUILD_LOCK_KEY

    assert _REBUILD_LOCK_KEY != SUITE_LOCK_KEY


def test_refusal_message_names_the_cause_and_the_fix() -> None:
    """The operator must not have to guess why the suite refused to start."""
    # Act
    message = concurrent_suite_message("postgresql://user@localhost:5434/example_test")

    # Assert
    assert "postgresql://user@localhost:5434/example_test" in message
    assert "TRUNCATE" in message
    assert "TEST_DATABASE_URL" in message
    assert "pgrep" in message
