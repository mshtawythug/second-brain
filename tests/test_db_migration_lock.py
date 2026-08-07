"""Tests for the per-database schema-mutation mutex (:func:`brain.db.migration_lock`).

Regression cover for 2026-08-07. Two migration runners hit the shared test
database concurrently and interleaved their DDL. The server log shows the
signature from both sides at once::

    [29232] ERROR: relation "schema_migrations" does not exist
            STATEMENT: INSERT INTO schema_migrations (name) VALUES ($1)
    [29264] ERROR: relation "sources" already exists
            STATEMENT: CREATE TABLE sources (...)
    [29282] ERROR: column "kind" of relation "documents" already exists

One runner's INSERT failed because the table vanished under it; the other's
CREATE failed because the table reappeared. Downstream this surfaced as walls of
unrelated test failures that were blamed on application code.

The pytest suite lock (:mod:`tests.db_lock`) could not have prevented it: it
excludes other *pytest sessions*, and at least one participant here was not one
— ``brain init`` / ``brain demo`` / ``brain backup restore`` all call
``run_migrations`` and, before this mutex, took no lock at all. So the guard has
to live in the production migration path, not in conftest.

These tests use a **private lock key**, never :data:`MIGRATION_LOCK_KEY`, except
where they deliberately assert on the real key's identity — so they can never
contend with a real migration or with the suite lock held by the session
fixture while they run.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from brain import db
from brain.db import (
    MIGRATION_LOCK_KEY,
    migration_lock,
    run_migrations,
)
from brain.errors import MigrationLockTimeout
from tests.conftest import TEST_DATABASE_URL
from tests.db_lock import SUITE_LOCK_KEY

#: A key private to this module — deliberately not MIGRATION_LOCK_KEY.
PRIVATE_KEY = 0x6D69_6772

pytestmark = pytest.mark.integration


def _connect(*, autocommit: bool = True) -> psycopg.Connection:
    """A plain connection to the test database.

    ``autocommit`` defaults to ON. Advisory locks are session-scoped, so nothing
    here needs a transaction — and a second connection sitting
    *idle-in-transaction* holds relation locks that collide with the
    ``TRUNCATE`` / ``DROP SCHEMA`` a neighbouring test's fixture performs. That
    surfaced as a ``DeadlockDetected`` in fixture setup when this module ran
    alongside others: exactly the kind of cross-test phantom this whole task
    exists to eliminate, so it is not something to paper over with a retry.
    Only the poisoned-transaction test opts back out.
    """
    conn = psycopg.connect(TEST_DATABASE_URL, connect_timeout=5)
    conn.autocommit = autocommit
    return conn


@pytest.fixture()
def rival() -> Iterator[psycopg.Connection]:
    """A second, independent session standing in for a rival migration runner.

    Autocommit for the reason in :func:`_connect`: this connection stays open
    for the whole test, and an open transaction on it would block the next
    test's fixture reset.
    """
    with psycopg.connect(TEST_DATABASE_URL, connect_timeout=5) as conn:
        conn.autocommit = True
        yield conn


def _rival_can_acquire(conn: psycopg.Connection, key: int = PRIVATE_KEY) -> bool:
    """True iff ``conn``'s session can take ``key`` right now (and gives it back)."""
    row = conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()
    assert row is not None
    acquired = bool(row[0])
    if acquired:
        conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
    return acquired


# ---------------------------------------------------------------------------
# Core mutual exclusion
# ---------------------------------------------------------------------------


def test_lock_excludes_a_rival_runner_and_releases_after(
    rival: psycopg.Connection,
) -> None:
    """While the block runs, nobody else migrates; afterwards, they can."""
    # Arrange
    with _connect() as holder:
        # Act + Assert — held for the duration of the block.
        with migration_lock(holder, key=PRIVATE_KEY):
            assert _rival_can_acquire(rival) is False

        # Assert — handed back on exit.
        assert _rival_can_acquire(rival) is True


def test_lock_is_released_when_the_block_raises(rival: psycopg.Connection) -> None:
    """A migration that blows up must not wedge the database for everyone else."""
    # Arrange
    with _connect() as holder:
        # Act
        with (
            pytest.raises(RuntimeError, match="migration exploded"),
            migration_lock(holder, key=PRIVATE_KEY),
        ):
            raise RuntimeError("migration exploded")

        # Assert
        assert _rival_can_acquire(rival) is True


def test_lock_is_released_after_the_transaction_is_poisoned(
    rival: psycopg.Connection,
) -> None:
    """A failed statement inside the block still hands the lock back.

    Covers the ``InFailedSqlTransaction`` branch: once a statement errors, the
    connection refuses every further statement — including the unlock — until
    the transaction is unwound. Without the rollback-then-unlock fallback the
    lock would survive until the connection happened to close, and the next
    runner would wait out the whole timeout for nothing.
    """
    # Arrange — autocommit OFF, so the failed statement aborts a real transaction.
    with _connect(autocommit=False) as holder:
        # Act
        with (
            pytest.raises(psycopg.errors.UndefinedTable),
            migration_lock(holder, key=PRIVATE_KEY),
        ):
            holder.execute("SELECT * FROM _no_such_table_at_all_")

        # Assert
        assert _rival_can_acquire(rival) is True


def test_lock_is_reentrant_for_the_same_session(rival: psycopg.Connection) -> None:
    """Nesting is safe — and it has to be.

    ``tests/conftest.py``'s ``_reset_schema_and_migrate`` wraps the whole reset
    (including the ``DROP SCHEMA``) and then calls ``run_migrations``, which
    takes the lock again. If the inner acquire deadlocked or the inner release
    handed the lock away early, every test session would break.
    """
    # Arrange
    with _connect() as holder:
        # Act — nested acquisition of the same key on one session.
        with migration_lock(holder, key=PRIVATE_KEY):
            # noqa below: the nesting IS the behaviour under test.
            with migration_lock(holder, key=PRIVATE_KEY):  # noqa: SIM117
                assert _rival_can_acquire(rival) is False
            # Assert — the INNER exit must NOT release it; one acquire remains.
            assert _rival_can_acquire(rival) is False

        # Assert — only after the outer exit is it free.
        assert _rival_can_acquire(rival) is True


# ---------------------------------------------------------------------------
# Waiting and timing out
# ---------------------------------------------------------------------------


def test_waits_then_succeeds_when_the_holder_lets_go(
    rival: psycopg.Connection,
) -> None:
    """A contended acquire retries rather than failing on the first refusal.

    The clock and sleep are injected, so this asserts the retry behaviour
    without spending real time. The rival releases on the second poll.
    """
    # Arrange — rival holds the key first.
    assert bool(
        rival.execute("SELECT pg_try_advisory_lock(%s)", (PRIVATE_KEY,)).fetchone()[0]
    )
    polls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) == 2:  # let go partway through the wait
            rival.execute("SELECT pg_advisory_unlock(%s)", (PRIVATE_KEY,))

    with (
        _connect() as waiter,
        # Act
        migration_lock(
            waiter,
            key=PRIVATE_KEY,
            timeout_s=30.0,
            poll_s=0.01,
            sleep=fake_sleep,
            monotonic=lambda: 0.0,  # never reaches the deadline
        ),
    ):
            # Assert — it really did wait, then really did get the lock.
            assert len(polls) == 2
            assert _rival_can_acquire(rival) is False


def test_timeout_raises_named_error_naming_the_database(
    rival: psycopg.Connection,
) -> None:
    """Giving up must say which database is contended and why it stopped."""
    # Arrange — rival takes and keeps the key.
    assert bool(
        rival.execute("SELECT pg_try_advisory_lock(%s)", (PRIVATE_KEY,)).fetchone()[0]
    )
    clock = iter([0.0, 0.0, 99.0])  # start, first check, past the deadline

    with _connect() as waiter:
        # Act
        with (
            pytest.raises(MigrationLockTimeout) as excinfo,
            migration_lock(
                waiter,
                key=PRIVATE_KEY,
                timeout_s=1.0,
                sleep=lambda _s: None,
                monotonic=lambda: next(clock),
            ),
        ):
            pytest.fail("must not enter the block while another runner holds it")

        # Assert — actionable, not "could not acquire lock".
        message = str(excinfo.value)
        assert str(waiter.info.dbname) in message
        assert "interleave" in message
        assert "its own database" in message

        # Teardown
        rival.execute("SELECT pg_advisory_unlock(%s)", (PRIVATE_KEY,))


# ---------------------------------------------------------------------------
# Wiring: the production migration path actually takes it
# ---------------------------------------------------------------------------


def test_run_migrations_takes_the_migration_lock(
    test_db: psycopg.Connection, mocker: Any
) -> None:
    """``run_migrations`` must hold the mutex across its check-then-act.

    This is the wiring assertion that makes every caller safe at once —
    ``brain init``, ``brain demo``, ``brain backup restore`` and the pytest
    session reset all funnel through ``run_migrations``. ``wraps`` keeps the
    real lock in play, so this observes rather than replaces the behaviour.
    """
    # Arrange
    spy = mocker.patch("brain.db.migration_lock", wraps=db.migration_lock)

    # Act — a no-op re-run against an already-migrated schema.
    run_migrations(test_db)

    # Assert
    assert spy.call_count == 1
    assert spy.call_args.args[0] is test_db


def test_run_migrations_is_blocked_by_a_rival_holder(
    test_db: psycopg.Connection, rival: psycopg.Connection, mocker: Any
) -> None:
    """With the real key held elsewhere, migrating refuses instead of racing.

    This is the 2026-08-07 scenario end to end: a rival runner (a bare
    ``brain init`` against the same database) holds the mutex, and the second
    runner stops rather than interleaving its DDL. The timeout is shortened via
    the injected clock so the test does not wait a real minute.
    """
    # Arrange — rival grabs the REAL migration key on this database.
    held = rival.execute(
        "SELECT pg_try_advisory_lock(%s)", (MIGRATION_LOCK_KEY,)
    ).fetchone()
    assert held is not None and bool(held[0])
    # Shorten the wait via the module constants rather than by faking the clock:
    # `migration_lock` resolves them at call time precisely so this works, and
    # patching a brain.db constant cannot perturb psycopg's own use of `time`.
    mocker.patch.object(db, "MIGRATION_LOCK_TIMEOUT_S", 0.05)
    mocker.patch.object(db, "_MIGRATION_LOCK_POLL_S", 0.01)

    try:
        # Act + Assert
        with pytest.raises(MigrationLockTimeout):
            run_migrations(test_db)
    finally:
        # Teardown
        rival.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))


# ---------------------------------------------------------------------------
# Scope: per-database, not cluster-wide
# ---------------------------------------------------------------------------


def test_the_same_key_is_independent_in_two_databases() -> None:
    """Pins the per-database scope of Postgres advisory locks.

    Measured 2026-08-07 and easy to get wrong in the other direction: the same
    key taken in two different databases on ONE server succeeds twice. That is
    why a scratch clone
    (``TEST_DATABASE_URL=.../second_brain_test_<name>``) can run alongside the
    main suite — it is outside the lock AND outside the blast radius, because
    truncation and migration only ever touch one database.

    Asserting it here means a future "let's make the lock global" change fails
    a test instead of silently serialising every worktree behind one another.
    """
    # Arrange — same server, different database. ``postgres`` always exists.
    params = conninfo_to_dict(TEST_DATABASE_URL)
    other_url = make_conninfo(**{**params, "dbname": "postgres"})

    # Act
    with (
        psycopg.connect(TEST_DATABASE_URL, connect_timeout=5) as here,
        psycopg.connect(other_url, connect_timeout=5) as there,
    ):
        here_row = here.execute(
            "SELECT pg_try_advisory_lock(%s)", (PRIVATE_KEY,)
        ).fetchone()
        there_row = there.execute(
            "SELECT pg_try_advisory_lock(%s)", (PRIVATE_KEY,)
        ).fetchone()

        # Assert
        assert here_row is not None and bool(here_row[0]) is True
        assert there_row is not None and bool(there_row[0]) is True

        # Teardown
        here.execute("SELECT pg_advisory_unlock(%s)", (PRIVATE_KEY,))
        there.execute("SELECT pg_advisory_unlock(%s)", (PRIVATE_KEY,))


def test_migration_key_collides_with_no_other_brain_lock() -> None:
    """Three advisory locks coexist on one server; none may share a key.

    ``brain-rebuild`` (``brnr``) guards the orchestrator, the pytest suite lock
    (``brnt``) guards a whole test run, and this one (``brnm``) guards schema
    mutation. Sharing a key would make unrelated work block — or worse, one
    release hand away another's mutex.
    """
    # IMPORTED, not retyped — see the note in tests/test_db_lock.py. Retyping
    # the constant makes the collision undetectable, which is the one thing
    # this test exists to catch.
    from brain.maintenance import _REBUILD_LOCK_KEY

    assert len({MIGRATION_LOCK_KEY, SUITE_LOCK_KEY, _REBUILD_LOCK_KEY}) == 3
