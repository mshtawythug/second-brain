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
    (seconds,) = timeout.args
    assert _is_positive_number(seconds), f"the marker times nothing: timeout({seconds!r})"
    assert timeout.kwargs == {"method": BROWSER_TIMEOUT_METHOD}


#: The slowest phase measured on the CI browser command (tests/browser_timeout.py):
#: the first test's setup, which launches chromium. A timeout must leave room for a
#: shared runner several times slower than the machine that measured it.
_SLOWEST_MEASURED_PHASE_S = 1.54
_MIN_HEADROOM = 10

#: The browser job's `timeout-minutes` in .github/workflows/ci.yml is 20. A
#: per-test timeout must fire well inside it, or a hang ends as a cancelled job
#: with no stack dump: at most a quarter of it.
_MAX_TIMEOUT_S = 300


def _is_positive_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def test_the_browser_timeout_is_positive_and_bounded() -> None:
    """pytest-timeout times a test only when its timeout is above 0, so 0 or
    None would silently time nothing (measured by the phase-5 audit: the hang
    mutant with 0 stalled past 300s). The tests above catch a non-positive
    constant (``_is_positive_number``) but not one out of range. This one pins
    the range."""
    assert _is_positive_number(BROWSER_TEST_TIMEOUT_S), (
        f"BROWSER_TEST_TIMEOUT_S = {BROWSER_TEST_TIMEOUT_S!r} times nothing"
    )
    assert _SLOWEST_MEASURED_PHASE_S * _MIN_HEADROOM <= BROWSER_TEST_TIMEOUT_S, (
        f"{BROWSER_TEST_TIMEOUT_S}s leaves under {_MIN_HEADROOM}x headroom over the "
        f"slowest measured phase ({_SLOWEST_MEASURED_PHASE_S}s)"
    )
    assert BROWSER_TEST_TIMEOUT_S <= _MAX_TIMEOUT_S, (
        f"{BROWSER_TEST_TIMEOUT_S}s would let a hang run into the browser job's limit"
    )


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
