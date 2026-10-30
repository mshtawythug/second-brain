"""Turn OCR OFF for every test, whatever the shell or a ``.env`` says (RM-32).

Loaded from ``tests/conftest.py`` (``pytest_plugins``), following
``tests/rerank_isolation_plugin.py``: conftest is past the 800-line ceiling.

**Why.** ``brain.ingest.pdf.extract_pdf(ocr=None)`` and ``brain doctor``'s
``ocr`` row read ``os.environ`` and look for ``tesseract`` on ``PATH``. On a
machine that has the binary, the default ``BRAIN_OCR=auto`` would shell out to
it from any test that ingests an image-only PDF or builds the doctor checks.

**How.** ``BRAIN_OCR`` is SET to ``off`` rather than deleted, because
``Config.load()`` publishes the dotenv chain with ``os.environ.setdefault``. The
other knobs are deleted. A test that needs OCR sets it with
``monkeypatch.setenv`` or passes an ``OcrSetup``; ``monkeypatch`` undoes both
per test. Proven by ``tests/test_ocr_isolation.py``.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

#: OCR's knobs besides ``BRAIN_OCR`` (``brain.settings.ocr``).
OCR_KNOBS = ("BRAIN_OCR_LANG", "BRAIN_OCR_MAX_PAGES")


@pytest.fixture(autouse=True)
def _ocr_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``BRAIN_OCR=off`` and no other OCR knob, for the length of one test."""
    monkeypatch.setenv("BRAIN_OCR", "off")
    for knob in OCR_KNOBS:
        monkeypatch.delenv(knob, raising=False)
    yield
