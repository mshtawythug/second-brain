"""Turn the document-leg stage OFF for every test, whatever the shell or a ``.env`` says.

Loaded from ``tests/conftest.py`` (``pytest_plugins``). A separate module because
conftest is far past the 800-line ceiling, following
``tests/rerank_isolation_plugin.py``.

**Why.** ``search_pipeline.run_search(stages=None)`` and
``eval.runner.run_eval(stages=None, env=None)`` read ``os.environ`` (roadmap
ticket RM-20, spec §2.3), so a ``BRAIN_DOC_LEG`` exported in the developer's
shell, or one a ``.env`` publishes, would otherwise turn the leg on for every
surface test.

**How.** ``BRAIN_DOC_LEG`` is SET to ``false`` rather than deleted, because
``Config.load()`` publishes the dotenv chain with ``os.environ.setdefault``,
which re-injects a deleted key and skips a present one. The weight is deleted.
A test that needs the leg sets it with ``monkeypatch.setenv`` and wins;
``monkeypatch`` undoes both per test. Proven by ``tests/test_docleg_isolation.py``.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _doc_leg_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``BRAIN_DOC_LEG=false`` and no weight knob, for the length of one test."""
    monkeypatch.setenv("BRAIN_DOC_LEG", "false")
    monkeypatch.delenv("BRAIN_DOC_LEG_WEIGHT", raising=False)
    yield
