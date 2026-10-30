"""Turn brief delivery OFF for every test, whatever the shell or a ``.env`` says (RM-16).

Loaded from ``tests/conftest.py`` (``pytest_plugins``), a separate module for the reason
``tests/rerank_isolation_plugin.py`` gives.

**Why.** ``brief_publish.build_brief`` reads ``os.environ`` when it is given no
``delivery_env`` (the ``brain brief --wiki`` path), so a ``BRAIN_BRIEF_DELIVERY=email`` exported
in the developer's shell, or published by a ``.env``, would make a CLI brief test try to send
mail through the real ``gws``. Delivery is an outward action; the suite must never take it.

**How.** ``BRAIN_BRIEF_DELIVERY`` is SET to ``none`` rather than deleted (``Config.load()``
re-injects a deleted key from a ``.env`` with ``os.environ.setdefault``). The other two knobs are
deleted. A test that needs delivery passes ``delivery_env`` and a fake channel, or sets the
variable with ``monkeypatch.setenv``, which wins; ``monkeypatch`` undoes both per test. Proven by
``tests/test_delivery_isolation.py``.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

#: The delivery knobs besides ``BRAIN_BRIEF_DELIVERY`` (``brain.settings.brief_delivery``).
DELIVERY_KNOBS = ("BRAIN_BRIEF_SLACK_WEBHOOK_URL", "BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS")


@pytest.fixture(autouse=True)
def _delivery_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``BRAIN_BRIEF_DELIVERY=none`` and no other delivery knob, for the length of one test."""
    monkeypatch.setenv("BRAIN_BRIEF_DELIVERY", "none")
    for knob in DELIVERY_KNOBS:
        monkeypatch.delenv(knob, raising=False)
    yield
