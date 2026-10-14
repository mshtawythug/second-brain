"""A per-test timeout for the BROWSER harness, so a hang FAILS the run.

Loaded from ``tests/conftest.py`` (``pytest_plugins``). It lives in its own
module because conftest is already far past the 800-line ceiling.

**Why the browser tests, and only they.** A page that loops forever, such as
graph.js's ``fitText`` with its binary search mutated to ``hi = mid``, never
answers a Playwright call that has no timeout of its own, such as
``page.evaluate``. Most tests that met that mutant failed at
``wait_for_selector``'s own 30s timeout. One did not. MEASURED 2026-10-12: the
CI browser command was still running after 45 minutes, stalled in
tests/test_ui_browser_inspector_blocks.py, and that module alone was still
running after 300s. With this plugin the same module ends with exit 1 after
60.7s. The database tests are not timed: a timeout there would be a new
failure mode for ~8,800 tests, to fix a hang that only a browser can produce.

A browser test that sets its own ``@pytest.mark.timeout`` keeps it.
"""
from __future__ import annotations

import pytest

#: Seconds before a browser test is failed as hung. MEASURED 2026-10-12 on the
#: CI browser command (196 tests, macOS, ``--durations``): no setup, call or
#: teardown phase took longer than 1.54s, and that phase was the first test's
#: setup, which launches chromium. 60s is about 40 times that, enough for a
#: slow shared runner, and still kills a hang in a minute instead of at the
#: job's own limit.
BROWSER_TEST_TIMEOUT_S = 60


#: ``thread``, NOT the default ``signal``. MEASURED 2026-10-12 under the hang
#: mutant on tests/test_ui_browser_inspector_blocks.py. ``signal`` raises in
#: the main thread, and the stack dump shows that thread waiting in
#: Playwright's greenlet dispatcher (``greenlet_main``), not in the test. The
#: test is reported FAILED, and then the run hangs anyway: with ``-x`` it was
#: still running 330s later. ``thread`` dumps every thread's stack and ends the
#: process with exit 1, 60.7s after the run began. So the run FAILS, and runs
#: no further tests. The dump does not name the test, because the test's frame
#: is in another greenlet. The progress line shows which module it was in, and
#: ``-v`` shows the test.
BROWSER_TIMEOUT_METHOD = "thread"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give every ``browser``-marked test without its own timeout this one."""
    for item in items:
        if item.get_closest_marker("browser") and not item.get_closest_marker("timeout"):
            item.add_marker(
                pytest.mark.timeout(BROWSER_TEST_TIMEOUT_S, method=BROWSER_TIMEOUT_METHOD)
            )
