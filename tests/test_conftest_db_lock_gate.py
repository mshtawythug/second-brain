"""The ``nodb``/``browser`` opt-out must not hand out an unlocked test database.

The opt-out in :func:`tests.conftest._session_touches_the_database` skips both
session fixtures — the machine-wide advisory lock AND the schema reset — on the
premise that nothing in the selection opens a connection. Nothing enforced that
premise: :func:`tests.conftest.test_db` opens its OWN connection, so a marked
module that grew a database test used to ``TRUNCATE`` the shared tables with no
lock held, silently destroying a concurrent session's fixtures.

The subprocess tests below run a real nested pytest session, so they exercise the
fixture WIRING rather than the helper in isolation. They point that session at an
unreachable database on purpose: if the gate ever regresses, the nested run
cannot reach a real server to truncate, and the assertion still goes red — a
regression test for a data-loss bug must not be able to cause the data loss.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import UnlockedTestDatabaseError, _require_suite_lock

pytestmark = pytest.mark.nodb

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Syntactically valid, deliberately unreachable, and not prod-shaped.
#:
#: Port 1 refuses instantly, so a regressed gate fails on the connection rather
#: than on a ``TRUNCATE``. The ``*_test`` database name keeps the import-time
#: prod guard in ``tests/conftest.py`` satisfied.
UNREACHABLE_TEST_DB_URL = "postgresql://brain:brain@127.0.0.1:1/second_brain_test"

_MODULE_TAKING_TEST_DB = '''
{marker}

def test_takes_the_test_db_fixture(test_db):
    raise AssertionError("the fixture should never have handed out a connection")
'''


def _run_nested_pytest(tmp_path: Path, *, marked: bool) -> subprocess.CompletedProcess[str]:
    """Run one nested pytest session over a single generated test module.

    The module lives OUTSIDE ``tests/``, so ``tests/conftest.py`` is loaded
    explicitly with ``-p tests.conftest`` instead of by directory discovery.
    That is what makes the selection consist of exactly one item, which is what
    the opt-out keys on.
    """
    marker = "import pytest\n\npytestmark = pytest.mark.nodb" if marked else ""
    module = tmp_path / "test_nested_case.py"
    module.write_text(_MODULE_TAKING_TEST_DB.format(marker=marker))
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(module),
            "-p",
            "tests.conftest",
            "-p",
            "no:cacheprovider",
            "--no-cov",
            "-q",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "TEST_DATABASE_URL": UNREACHABLE_TEST_DB_URL,
            "PYTEST_ADDOPTS": "",
        },
    )


def test_a_marked_module_requesting_test_db_is_refused_by_the_lock_gate(
    tmp_path: Path,
) -> None:
    """A ``nodb`` module that takes ``test_db`` stops on the gate, not on luck."""
    # Arrange / Act
    result = _run_nested_pytest(tmp_path, marked=True)
    combined = result.stdout + result.stderr

    # Assert — the failure is the GATE, named explicitly. Asserting only
    # "nonzero exit" would pass on the connection error too, which is exactly the
    # outcome this test must be able to tell apart.
    assert result.returncode != 0, combined
    assert UnlockedTestDatabaseError.__name__ in combined, combined
    assert "skipped the machine-wide test-database lock" in combined, combined


def test_an_unmarked_module_is_not_stopped_by_the_lock_gate(tmp_path: Path) -> None:
    """Establishes what an UNGATED run looks like, so the test above can be specific.

    Same generated module, same unreachable database, marker removed. The session
    takes the opt-out's other branch, tries to acquire the lock, and fails on the
    CONNECTION — which is exactly the failure the marked module produced before
    the gate existed, and exactly what it falls back to when the gate is removed.
    That contrast is why the test above asserts on the exception NAME instead of
    on a nonzero exit, which both runs share.

    **This is not the vacuity control, and pretending otherwise would be the same
    class of false claim the gate itself was fixed for.** Mutating
    ``_require_suite_lock`` to raise unconditionally leaves this test GREEN — the
    unmarked path dies at the connection before the gate is ever consulted, and
    it cannot reach it without a live database and the lock this suite already
    holds. ``test_require_suite_lock_raises_only_when_the_lock_was_skipped`` is
    what fails on that mutation.
    """
    # Arrange / Act
    result = _run_nested_pytest(tmp_path, marked=False)
    combined = result.stdout + result.stderr

    # Assert
    assert result.returncode != 0, combined
    assert UnlockedTestDatabaseError.__name__ not in combined, combined
    assert "Connection refused" in combined or "could not connect" in combined, combined


def test_require_suite_lock_raises_only_when_the_lock_was_skipped() -> None:
    """The helper itself: silent when held, and it names the fix when not."""
    # Arrange / Act / Assert — held: returns, no exception.
    assert _require_suite_lock(True, "The test_db fixture") is None

    # Not held: raises, and the message points at the marker rather than the DB.
    with pytest.raises(UnlockedTestDatabaseError) as excinfo:
        _require_suite_lock(False, "The test_db fixture")
    message = str(excinfo.value)
    assert "The test_db fixture" in message
    assert "Remove the marker from the module" in message
