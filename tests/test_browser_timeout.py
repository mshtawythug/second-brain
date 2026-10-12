"""Unit tests for tests/browser_timeout.py: which collected tests get timed."""
from __future__ import annotations

from typing import Any

import pytest

from tests.browser_timeout import (
    BROWSER_TEST_TIMEOUT_S,
    BROWSER_TIMEOUT_METHOD,
    pytest_collection_modifyitems,
)

pytestmark = pytest.mark.nodb


def _mark(name: str, *args: Any) -> Any:
    """A marker, looked up by NAME rather than written as ``pytest.mark.<name>``:
    written that way for ``browser``, tests/test_ci_workflow.py would read
    this module as a browser suite that CI must run."""
    return getattr(pytest.mark, name)(*args).mark


class _Item:
    """A collected item, as far as the hook reads one: its markers by name."""

    def __init__(self, *marks: Any) -> None:
        self.marks = list(marks)

    def get_closest_marker(self, name: str) -> Any:
        return next((mark for mark in self.marks if mark.name == name), None)

    def add_marker(self, marker: Any) -> None:
        self.marks.append(marker.mark)


def test_a_browser_test_without_a_timeout_gets_the_browser_timeout() -> None:
    item = _Item(_mark("browser"))

    pytest_collection_modifyitems([item])

    timeout = item.get_closest_marker("timeout")
    assert timeout is not None and timeout.args == (BROWSER_TEST_TIMEOUT_S,)
    assert timeout.kwargs == {"method": BROWSER_TIMEOUT_METHOD}


def test_the_browser_timeout_ends_the_process_rather_than_raising() -> None:
    """``thread``, not ``signal``: a raised timeout leaves Playwright's
    dispatcher hung (measured; tests/browser_timeout.py)."""
    assert BROWSER_TIMEOUT_METHOD == "thread"


def test_a_browser_test_keeps_its_own_timeout() -> None:
    item = _Item(_mark("browser"), _mark("timeout", 5))

    pytest_collection_modifyitems([item])

    assert [mark.args for mark in item.marks if mark.name == "timeout"] == [(5,)]


def test_a_test_that_is_not_a_browser_test_is_not_timed() -> None:
    item = _Item(_mark("nodb"))

    pytest_collection_modifyitems([item])

    assert item.get_closest_marker("timeout") is None


def test_the_hook_is_loaded_in_every_session(pytestconfig: pytest.Config) -> None:
    """tests/conftest.py loads the module as a plugin. Unloaded, the hook above
    is never called, and every unit test here still passes."""
    assert pytestconfig.pluginmanager.has_plugin("tests.browser_timeout")
