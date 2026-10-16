"""Regression tests: a test's dotenv values must not reach the next test's ``os.environ``.

Bug (2026-10-16): ``test_config_dotenv_chain.py``'s
``test_found_but_incomplete_dotenv_reports_loaded_not_missing`` writes
``BRAIN_EMBEDDER=none`` into a test ``.env`` and calls ``Config.load()``, which
``os.environ.setdefault``-s every dotenv key before it raises on the missing
``DATABASE_URL``. ``monkeypatch`` restores only the keys it was told about, so
the value outlived the test, and seven ``test_cli_doctor.py`` tests failed
whenever the chain module ran first. CI runs alphabetically, so CI never saw it.

Publishing dotenv values into the process env is intended production
behaviour (see ``docs/specs/2026-10-16-dotenv-isolation-leak-design.md`` §3),
so the fix is the session-wide guard in ``tests/env_isolation.py``. These tests
pin the guard's mechanism, prove it holds across a real test boundary in a
fixed order, and check that the real suite actually runs under it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from brain.config import Config
from tests.conftest import isolate_config_dotenv
from tests.env_isolation import restored_environ

pytest_plugins = ("pytester",)

#: Never set by anything else; unique so a leak is unambiguous.
_SENTINEL = "BRAIN_TEST_ENV_ISOLATION_SENTINEL"
_CHANGED = "BRAIN_TEST_ENV_ISOLATION_CHANGED"
_REMOVED = "BRAIN_TEST_ENV_ISOLATION_REMOVED"

# Two inner tests, run in file order by an inner pytest session: the first
# publishes a key through the real ``Config.load_minimal`` path (the same
# mechanism as the original leak), the second asserts it did not survive.
_BOUNDARY_PAIR = f'''
import os

from brain.config import Config
from tests.conftest import isolate_config_dotenv


def test_a_publishes_a_dotenv_key(monkeypatch, tmp_path):
    project = isolate_config_dotenv(monkeypatch, tmp_path)
    project.write_text("{_SENTINEL}=leaked\\n", encoding="utf-8")
    Config.load_minimal()
    assert os.environ["{_SENTINEL}"] == "leaked"


def test_b_does_not_inherit_it():
    assert os.environ.get("{_SENTINEL}") is None
'''


def test_restored_environ_undoes_what_config_load_publishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Added keys are removed; changed and deleted keys get their old values back."""
    project = isolate_config_dotenv(monkeypatch, tmp_path)
    project.write_text(f"{_SENTINEL}=published\n", encoding="utf-8")
    monkeypatch.setenv(_CHANGED, "before")
    monkeypatch.setenv(_REMOVED, "present")

    with restored_environ():
        Config.load_minimal()
        # The leak path is live: without this the test would pass vacuously.
        assert os.environ[_SENTINEL] == "published"
        os.environ[_CHANGED] = "after"
        del os.environ[_REMOVED]

    assert os.environ.get(_SENTINEL) is None
    assert os.environ.get(_CHANGED) == "before"
    assert os.environ.get(_REMOVED) == "present"


def test_published_key_does_not_cross_a_test_boundary(
    pytester: pytest.Pytester,
) -> None:
    """In a fixed a-then-b order, the guard stops test_a's key from reaching test_b."""
    assert os.environ.get(_SENTINEL) is None
    pytester.makepyfile(test_boundary_pair=_BOUNDARY_PAIR)

    result = pytester.runpytest("-p", "tests.env_isolation", "-p", "no:cacheprovider")

    result.assert_outcomes(passed=2)


def test_boundary_pair_fails_without_the_guard(pytester: pytest.Pytester) -> None:
    """Negative control: the same pair, unguarded, reproduces the leak.

    Proves the inner assertion can fail, and that the leak mechanism the guard
    defends against still exists. The key this leaks into this process is
    removed again by this test's own guard.
    """
    assert os.environ.get(_SENTINEL) is None
    pytester.makepyfile(test_boundary_pair=_BOUNDARY_PAIR)

    result = pytester.runpytest("-p", "no:cacheprovider")

    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*FAILED*test_b_does_not_inherit_it*"])


def test_every_test_runs_inside_the_environ_guard(request: pytest.FixtureRequest) -> None:
    """The guard is wired into THIS suite, not only loadable on request.

    The boundary test loads the plugin itself with ``-p``, so it stays green
    even if ``tests/conftest.py`` stops registering it. This one does not.
    """
    assert "_restore_os_environ" in request.fixturenames
