"""Whole-suite exclusivity lock for the shared test database.

Every test goes through the ``test_db`` fixture, which resets state by
``TRUNCATE``-ing the shared tables (or, under ``fresh_schema``, by dropping and
re-migrating the schema). That reset is safe exactly once per database. Two
pytest processes pointed at the same ``TEST_DATABASE_URL`` therefore truncate
each other's fixtures mid-test.

The resulting failures are *non-deterministic and misleading*: on 2026-07-26 two
concurrent full-suite runs produced two entirely disjoint failure sets — one run
reported 2 failures in ``tests/test_vault_sync.py``, the next reported 11
failures across six unrelated files, several surfacing as raw ``psycopg`` errors.
Every one of them passed when re-run alone. Roughly an hour was spent hunting a
production bug that did not exist.

This module converts that failure mode into an immediate, self-explaining error:
the first suite to start takes a Postgres session-level advisory lock, and any
second suite refuses to run rather than corrupting both.

**Running suites in parallel anyway.** Postgres advisory locks are scoped to a
*database*, not to the cluster — verified 2026-07-26: the same key taken in two
different databases on one server succeeds twice, while a second taker in the
same database is refused. So concurrent runs are fine as long as each owns its
own database::

    createdb -h localhost -p 5434 -U brain second_brain_test_<name>
    TEST_DATABASE_URL=postgresql://brain:brain@localhost:5434/second_brain_test_<name> pytest

That is the supported way to parallelise (e.g. one database per git worktree).
The name must not be exactly ``second_brain`` and the port must not be a prod
port, or ``conftest``'s prod guard will — correctly — refuse to run at all.

**A mid-run Postgres crash-recovery looks identical to a code failure.** On
2026-08-06 the AGE test container crashed and self-recovered
(``database system was not properly shut down; automatic recovery in progress``
… ``redo done``, ~7s). Suites running through that window reported a
``psycopg.Connection [BAD]`` mid-statement — which reads exactly like a logic
bug — and a re-run on a *freshly created* database produced 38 setup errors with
``FATAL: the database system is in recovery mode``. Neither had anything to do
with the code under test; both cleared once the container finished recovering.

So before believing a wall of connection-level failures, check
``docker logs second-brain-age-test`` and ``pg_isready``. The tell is the
failure *layer*: a crash-recovery fails at CONNECT or mid-statement with a dead
connection, whereas a real defect fails at an ASSERTION. Re-run anything that
errored on connection rather than on an assert.

**Killing a suite mid-run leaves its database unusable.** The session fixture
resets the schema by dropping and re-migrating it; a ``SIGTERM`` landing inside
that window leaves the schema half-dropped, and the *next* run against that same
database then errors on every test until its own session fixture rebuilds the
schema. Observed 2026-07-26. So a wall of setup errors right after someone
cancelled a run is an artifact of the cancellation, not of their code — re-run
once and it clears. The lock itself is released cleanly on kill (it is
session-scoped), so this is a schema-state problem, not a lock problem.
"""
from __future__ import annotations

from typing import Any, Protocol

#: Advisory-lock key for "a pytest suite owns this database".
#:
#: Distinct from the ``brain-rebuild`` orchestrator's key (``0x62726E72``,
#: "brnr", at ``src/brain/maintenance.py``) so a rebuild and a test suite never
#: contend for the same lock — they operate on different databases and must not
#: block one another. ``0x6272_6E74`` spells "brnt" (brain-test).
SUITE_LOCK_KEY = 0x62726E74


class LockCursor(Protocol):
    """The narrow slice of a DB-API cursor this module needs."""

    def execute(self, query: str, params: Any = ..., /) -> Any: ...

    def fetchone(self) -> Any: ...


def _single_bool(cursor: LockCursor, sql: str, key: int, fn: str) -> bool:
    """Run a one-row, one-boolean advisory-lock call and return its result."""
    cursor.execute(sql, (key,))
    row = cursor.fetchone()
    if row is None:  # pragma: no cover - Postgres always returns a row here
        raise RuntimeError(f"{fn} returned no row")
    return bool(row[0])


def try_acquire_suite_lock(cursor: LockCursor, *, key: int = SUITE_LOCK_KEY) -> bool:
    """Attempt to take the suite-exclusivity lock without blocking.

    ``pg_try_advisory_lock`` returns immediately rather than waiting, so a
    second suite fails fast instead of hanging until the first one finishes.
    The lock is *session*-scoped: held for as long as the connection that took
    it stays open, and released automatically if that process dies — so a
    crashed run never leaves the database permanently locked.

    :param cursor: an open cursor on the test database.
    :param key: advisory-lock key; overridable so tests can use a private key
        and never contend with a real suite running alongside them.
    :returns: ``True`` when this session now owns the lock, ``False`` when
        another session already holds it.
    """
    return _single_bool(
        cursor, "SELECT pg_try_advisory_lock(%s)", key, "pg_try_advisory_lock"
    )


def release_suite_lock(cursor: LockCursor, *, key: int = SUITE_LOCK_KEY) -> bool:
    """Release the suite-exclusivity lock.

    Closing the connection releases it too; this exists so teardown is explicit
    and so a long-lived connection can hand the lock back deterministically.

    :returns: ``True`` if a lock was held and is now released, ``False`` if this
        session did not hold it.
    """
    return _single_bool(
        cursor, "SELECT pg_advisory_unlock(%s)", key, "pg_advisory_unlock"
    )


def concurrent_suite_message(database_url: str) -> str:
    """The operator-facing explanation shown when the lock cannot be taken.

    Names the actual cause and the actual fix. A bare "could not acquire lock"
    would send the reader looking for a deadlock in their own test.
    """
    return (
        "\n"
        "Another pytest session already owns the test database.\n"
        f"  database: {database_url}\n"
        "\n"
        "Two suites sharing one database TRUNCATE each other's fixtures between\n"
        "tests, which produces failures that move around between runs and vanish\n"
        "when re-run alone. Refusing to start rather than corrupting both.\n"
        "\n"
        "Fix: wait for the other run to finish (`pgrep -fl pytest`), or point\n"
        "this run at its own database with TEST_DATABASE_URL.\n"
    )
