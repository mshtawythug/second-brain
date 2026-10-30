"""Turn voice memos OFF for every test, whatever the shell or a ``.env`` says (RM-38).

Loaded from ``tests/conftest.py`` (``pytest_plugins``), following
``tests/ocr_isolation_plugin.py``: conftest is past the 800-line ceiling.

**Why.** ``brain.ingest.voice.extract_voice(whisper=None)`` and ``brain doctor``'s
``voice memos`` row read ``os.environ`` and look for ``whisper-cli`` and the model. On
a machine that has both, a test that dispatched an ``.m4a`` would run the real
``afinfo``, ``afconvert`` and ``whisper-cli``.

**How.** ``BRAIN_WHISPER_MODEL`` is SET to the empty string (which the settings leaf
reads as unset, so voice memos are off) rather than deleted, because ``Config.load()``
publishes the dotenv chain with ``os.environ.setdefault``. ``BRAIN_WHISPER_MAX_MINUTES``
is deleted. A test that needs voice passes a ``WhisperSetup`` (or sets the knob with
``monkeypatch.setenv``); ``monkeypatch`` undoes both per test. Proven by
``tests/test_voice_isolation.py``.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _voice_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``BRAIN_WHISPER_MODEL`` empty and no cap knob, for the length of one test."""
    monkeypatch.setenv("BRAIN_WHISPER_MODEL", "")
    monkeypatch.delenv("BRAIN_WHISPER_MAX_MINUTES", raising=False)
    yield
