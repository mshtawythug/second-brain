"""Turn the cross-encoder reranker OFF for every test, whatever the shell or a ``.env`` says.

Loaded from ``tests/conftest.py`` (``pytest_plugins``). It is a separate module
because conftest is already far past the 800-line ceiling, following
``tests/env_isolation.py``.

**Why.** ``search_pipeline.run_search(stages=None)`` and
``eval.runner.run_eval(stages=None, env=None)`` read ``os.environ`` (roadmap
ticket RM-14a, spec §2.3), so a ``BRAIN_RERANKER`` exported in the developer's
shell, or one a ``.env`` publishes, would otherwise turn the reranker on for
every surface test. rev-76 reproduced three failures that way.

**How.** ``BRAIN_RERANKER`` is SET to ``none`` rather than deleted:
``Config.load()`` publishes the dotenv chain with ``os.environ.setdefault``,
which re-injects a deleted key and skips a present one. The other knobs are
deleted. A test that needs the reranker sets it with ``monkeypatch.setenv`` and
wins; ``monkeypatch`` undoes both per test. Proven by
``tests/test_rerank_isolation.py``, which also checks this plugin is registered.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

#: The reranker's knobs besides ``BRAIN_RERANKER`` (``brain.settings.rerank``).
RERANK_KNOBS = (
    "BRAIN_RERANK_DEPTH",
    "BRAIN_RERANK_TIMEOUT_S",
    "BRAIN_RERANK_URL",
    "BRAIN_RERANK_MODEL",
    "BRAIN_RERANK_MAX_CHARS",
    "BRAIN_RERANK_GGUF",
)


@pytest.fixture(autouse=True)
def _reranker_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``BRAIN_RERANKER=none`` and no other rerank knob, for the length of one test."""
    monkeypatch.setenv("BRAIN_RERANKER", "none")
    for knob in RERANK_KNOBS:
        monkeypatch.delenv(knob, raising=False)
    yield
