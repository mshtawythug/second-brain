"""C22 regression: a test must not be able to drop the suite's own database.

2026-08-07. ``tests/backup_fakes.drop_restore_artifacts`` reclaims a parked
database with ``DROP DATABASE {live} WITH (FORCE)``. ``FORCE`` exists to
terminate other backends — and when ``live`` is the database the pytest session
is running on, the backends it terminates are the session's own. Observed twice,
~45 minutes apart: a backend exited with code 2, the postmaster crash-restarted
the entire instance, ``second_brain_test`` was left ``datconnlimit = -2``
(unusable without a manual DROP+CREATE), and ~670 downstream tests errored in
setup — indicting `test_vault_watcher.py`, which had nothing to do with it.

The structural point this pins: the repo's DB-safety guards protected
PRODUCTION from the tests. Nothing protected the TEST database from a test, so a
full suite could destroy its own substrate.

Most of these need no database — the guard is pure comparison — so they are
cheap and cannot themselves be the thing that breaks the DB. The one exception
is :func:`test_drop_restore_artifacts_actually_calls_the_guard`, which proves
the guard is WIRED; see its docstring for why it is safe even if the wiring it
tests is broken.
"""
from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from tests.backup_fakes import (
    LiveSuiteDatabaseDrop,
    _assert_not_the_running_suite_database,
    drop_restore_artifacts,
    dsn_for_database,
)
from tests.conftest import TEST_DATABASE_URL

_SESSION_DB = str(
    psycopg.conninfo.conninfo_to_dict(TEST_DATABASE_URL).get("dbname", "")
)


def test_guard_refuses_the_database_the_suite_is_running_on() -> None:
    """THE regression. Passing the session's own database must raise."""
    with pytest.raises(LiveSuiteDatabaseDrop) as excinfo:
        _assert_not_the_running_suite_database(_SESSION_DB)

    message = str(excinfo.value)
    assert _SESSION_DB in message
    # The message must name the mechanism, not just refuse — whoever hits this
    # needs to know FORCE is what makes it fatal.
    assert "FORCE" in message
    assert "throwaway" in message


def test_guard_allows_a_throwaway_database() -> None:
    """POSITIVE CONTROL: the guard must not refuse everything.

    Without this, a guard that raised unconditionally would pass the test above
    while making every restore test impossible — a guard is only meaningful if
    it also lets the safe case through.
    """
    _assert_not_the_running_suite_database(f"{_SESSION_DB}_throwaway_sandbox")
    _assert_not_the_running_suite_database("some_unrelated_database")


def test_guard_ignores_an_empty_name() -> None:
    """An unparseable DSN must not be read as 'matches the session database'.

    ``conninfo_to_dict`` yields ``""`` for a DSN without a dbname. Treating that
    as a match would make the guard fire on inputs it knows nothing about —
    refusing safe work for a reason it cannot justify.
    """
    _assert_not_the_running_suite_database("")


def test_guard_resolves_the_session_database_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The comparison must not be captured at import.

    An import-time snapshot is the exact seam-that-cannot-be-overridden shape
    this codebase has hit repeatedly (a clock injected via a signature default;
    an env var read at module load). Here it would mean a suite pointed at a
    scratch database still compared against whatever was configured when the
    module was first imported — so the guard would protect the wrong database.
    """
    import tests.conftest as conftest_module

    # DERIVED, not a literal. A hardcoded `postgresql://...second_brain_test...`
    # here is exactly what `test_database_url_isolation` forbids, and it failed
    # that scan — a guard module that violates the repo's own DSN-pinning guard.
    # Nothing connects to this database; it exists only as a name to compare.
    scratch_db = f"{_SESSION_DB}_scratch"
    monkeypatch.setattr(
        conftest_module,
        "TEST_DATABASE_URL",
        dsn_for_database(TEST_DATABASE_URL, scratch_db),
    )

    # The NEW session database is now refused...
    with pytest.raises(LiveSuiteDatabaseDrop):
        _assert_not_the_running_suite_database(scratch_db)
    # ...and the previously-refused one is allowed, proving the value is re-read.
    _assert_not_the_running_suite_database(_SESSION_DB)


#: DERIVED from the session database, never hardcoded — the sibling restore
#: modules carry the same rule. Parallel worktree suites each get their own
#: ``second_brain_test_*``; a literal here would have two of them creating and
#: dropping the same two databases, so one suite's teardown would delete the
#: database the other is asserting on — a cross-suite flake in the module whose
#: whole job is proving a safety guard.
_WIRING_DB = f"{_SESSION_DB}_c22wire"
_WIRING_DSN = dsn_for_database(TEST_DATABASE_URL, _WIRING_DB)
_WIRING_PARKED = f"{_WIRING_DB}_replaced_20260101_000000"


def _drop_wiring_databases() -> None:
    """Drop ONLY this module's two throwaways, never anything else.

    Named explicitly rather than by pattern, and each name re-checked before
    issuing ``WITH (FORCE)`` — this is the one raw ``DROP DATABASE`` in the
    suite not already behind :func:`drop_restore_artifacts`' guards, so it
    carries its own.

    Deliberately NOT :func:`_assert_not_the_running_suite_database`, even
    though that is the obvious choice. It resolves
    ``tests.conftest.TEST_DATABASE_URL`` at CALL time — which is the property
    that makes it correct everywhere else, and wrong here: this function also
    runs in a teardown where the test has monkeypatched exactly that attribute
    to ``_WIRING_DSN``. It would compare each name against the *pretend*
    session database, match, and refuse the very cleanup it exists to perform,
    leaking both throwaways onto the shared server. Compare against
    ``_SESSION_DB`` instead — captured at import from the real, unpatched
    value, which is what "the suite's own database" actually means here.
    """
    # TRIPWIRE, not runtime protection: both names derive from `_WIRING_DB`, so
    # today these cannot fire. They exist to fail loudly the moment someone
    # hardcodes a name here — the very edit that would aim a raw
    # `DROP DATABASE ... WITH (FORCE)` at something real. Stated explicitly
    # because an assertion that cannot fail is otherwise indistinguishable
    # from the inert-safety defects this module exists to prevent.
    for name in (_WIRING_DB, _WIRING_PARKED):
        assert name != _SESSION_DB, "refusing to drop the suite's own database"
        assert name.startswith(f"{_SESSION_DB}_c22wire"), f"unexpected target: {name}"
    maintenance = dsn_for_database(TEST_DATABASE_URL, "postgres")
    with psycopg.connect(maintenance) as conn:
        conn.autocommit = True
        for name in (_WIRING_DB, _WIRING_PARKED):
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture
def parked_database_for_a_pretend_session() -> Iterator[None]:
    """Create ``<_WIRING_DB>_replaced_<ts>``; remove it and any rename afterwards."""
    _drop_wiring_databases()
    with psycopg.connect(dsn_for_database(TEST_DATABASE_URL, "postgres")) as conn:
        conn.autocommit = True
        conn.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(_WIRING_PARKED))
        )
    try:
        yield
    finally:
        _drop_wiring_databases()


def test_drop_restore_artifacts_actually_calls_the_guard(
    monkeypatch: pytest.MonkeyPatch,
    parked_database_for_a_pretend_session: None,
) -> None:
    """WIRING. A guard nothing calls is the defect it was written to prevent.

    Every other test here exercises :func:`_assert_not_the_running_suite_database`
    directly. That proves the comparison is correct and proves nothing about
    whether :func:`drop_restore_artifacts` — the only caller, and the function
    that actually issues ``DROP DATABASE ... WITH (FORCE)`` — still reaches it.
    Delete the call and all four of those tests stay green while the hazard is
    fully restored. This test fails instead.

    **Why this is safe even if the wiring is broken.** It does not point the
    guard at the real session database. It monkeypatches ``TEST_DATABASE_URL``
    so the guard treats ``_WIRING_DB`` as "the session database", and every
    database :func:`drop_restore_artifacts` can reach from that DSN is one this
    fixture created. With the guard removed the call would drop/rename only
    those throwaways; ``second_brain_test`` is not named anywhere in the path.
    """
    import tests.conftest as conftest_module

    monkeypatch.setattr(conftest_module, "TEST_DATABASE_URL", _WIRING_DSN)

    with pytest.raises(LiveSuiteDatabaseDrop) as excinfo:
        drop_restore_artifacts(_WIRING_DSN)

    assert _WIRING_DB in str(excinfo.value)
    # And it refused BEFORE destroying anything: the parked database survives.
    with psycopg.connect(dsn_for_database(TEST_DATABASE_URL, "postgres")) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (_WIRING_PARKED,)
        ).fetchone()
    assert row is not None, "the guard fired only after the destructive work"


def test_guard_also_covers_the_no_parked_database_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leftovers sweep must be guarded too, not just the parked branch.

    :func:`drop_restore_artifacts` has two destructive branches. The one that
    reclaims a parked database is the obvious one. The other — the sweep that
    drops everything matching ``{live}_restore_%`` — is reached even when no
    parked database exists. With the guard sitting inside the parked branch, a
    caller passing the suite's own DSN on a clean server would sail past it and
    into that unconditional drop, and the sibling wiring test would not notice:
    it always creates a parked database first, so it only ever exercises the
    guarded branch.

    Historically the sweep pattern also MATCHED the sandbox database itself,
    then named ``{live}_restore_sandbox``, so the miss deleted the session's
    sandbox mid-run. The sandbox is now ``{live}_sbx``
    (:data:`tests.backup_fakes.SANDBOX_SUFFIX`) and no longer collides; this
    test asserts the guard, which is what actually prevents the drop.

    Takes NO fixture, so no ``_replaced_`` database exists — which is exactly
    the state that used to slip through.
    """
    import tests.conftest as conftest_module

    monkeypatch.setattr(conftest_module, "TEST_DATABASE_URL", _WIRING_DSN)

    with pytest.raises(LiveSuiteDatabaseDrop):
        drop_restore_artifacts(_WIRING_DSN)
