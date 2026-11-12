"""Turn the engagement stage OFF for every test, whatever the shell or a ``.env`` says.

Loaded from ``tests/conftest.py`` (``pytest_plugins``). A separate module because
conftest is far past the 800-line ceiling, following
``tests/docleg_isolation_plugin.py``.

**Why.** ``search_pipeline.run_search(stages=None)`` and
``eval.runner.run_eval(stages=None, env=None)`` read ``os.environ`` (roadmap
ticket RM-22b, spec §2.3), so a ``BRAIN_ENGAGEMENT_ALPHA`` exported in the
developer's shell, or one a ``.env`` publishes, would otherwise turn the stage
on for every surface test, and reorder results by whatever ``interactions``
rows the test happened to write.

**How.** Both knobs are SET to ``0`` rather than deleted, because
``Config.load()`` publishes the dotenv chain with ``os.environ.setdefault``,
which re-injects a deleted key and skips a present one. The stage is on when
either knob is above zero, so both must be pinned. A test that needs the stage
sets a knob with ``monkeypatch.setenv`` and wins; ``monkeypatch`` undoes both per
test. Proven by ``tests/test_engagement_isolation.py``.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _engagement_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``BRAIN_ENGAGEMENT_ALPHA=0`` and ``BRAIN_ENGAGEMENT_BETA=0``, for one test."""
    monkeypatch.setenv("BRAIN_ENGAGEMENT_ALPHA", "0")
    monkeypatch.setenv("BRAIN_ENGAGEMENT_BETA", "0")
    yield
