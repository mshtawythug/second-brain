"""A ``BRAIN_OCR`` exported in the shell never reaches a test (RM-32).

``tests/ocr_isolation_plugin.py``'s autouse ``_ocr_off`` (registered in
``tests/conftest.py``) sets ``BRAIN_OCR=off`` and clears the other OCR knobs for
every test. The probe checks that from inside a test; the regression test runs
it in a CHILD pytest whose environment exports OCR on with every knob. Both are
``nodb``. All values are synthetic.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from brain.ingest.ocr import resolve_ocr

pytestmark = pytest.mark.nodb

REPO = Path(__file__).resolve().parents[1]
PROBE = "tests/test_ocr_isolation.py::test_ocr_is_off_inside_every_test"

EXPORTED = {"BRAIN_OCR": "auto", "BRAIN_OCR_LANG": "deu", "BRAIN_OCR_MAX_PAGES": "7"}


def test_ocr_is_off_inside_every_test() -> None:
    assert os.environ.get("BRAIN_OCR") == "off"
    assert not {k for k in EXPORTED if k != "BRAIN_OCR"} & set(os.environ)

    setup = resolve_ocr(os.environ, which=lambda name: "/synthetic/bin/tesseract")

    assert setup.engine is None


def test_an_exported_ocr_mode_does_not_reach_a_test() -> None:
    env = {**os.environ, **EXPORTED, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", PROBE,
            "-q", "--no-cov", "-p", "no:cacheprovider", "-m", "nodb",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "1 passed" in result.stdout


def test_every_test_runs_inside_the_ocr_guard(request: pytest.FixtureRequest) -> None:
    """The plugin is wired in through ``tests/conftest.py``'s ``pytest_plugins``."""
    assert "_ocr_off" in request.fixturenames
