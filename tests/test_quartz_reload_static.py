"""Static smoke test for the Quartz browser polling reload script (reload.js).

Pins the ``INTERVAL_MS`` constant that drives edit-to-UI latency. No JS
toolchain is required — assertion is a direct substring check against the
source file, following the pattern in ``tests/test_quartz_parser_cache_static.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RELOAD_JS = REPO_ROOT / "quartz_overrides" / "quartz" / "static" / "reload.js"


# ---------------------------------------------------------------------------
# Shared fixture — read file once per module
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def reload_js_source() -> str:
    """Read ``reload.js`` once per module."""
    assert RELOAD_JS.is_file(), f"missing reload.js at {RELOAD_JS}"
    return RELOAD_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reload_js_file_exists() -> None:
    """``reload.js`` exists at the expected overlay path."""
    assert RELOAD_JS.is_file(), (
        f"reload.js not found at {RELOAD_JS} — was the overlay file committed?"
    )


def test_interval_ms_is_1000(reload_js_source: str) -> None:
    """``INTERVAL_MS`` is set to 1000ms in reload.js.

    Pin the polling interval that drives edit-to-UI latency. 1000ms is per
    Task 6 of the 2026-05-09 closeout plan; if this fails, latency math is off.
    """
    assert "var INTERVAL_MS = 1000" in reload_js_source, (
        "expected `var INTERVAL_MS = 1000` in reload.js — "
        "1000ms per Task 6 of the 2026-05-09 closeout plan; "
        "a higher value inflates perceived edit-to-UI latency"
    )


def test_reload_accepts_fastpath_build_ids(reload_js_source: str) -> None:
    """Partial builds write ``fastpath-...`` ids that must reload open tabs."""
    assert "fastpath-\\d+-[0-9a-f]{8}" in reload_js_source, (
        "reload.js must accept fastpath build ids written by build-partial"
    )
