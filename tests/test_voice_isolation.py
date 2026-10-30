"""A ``BRAIN_WHISPER_MODEL`` exported in the shell never reaches a test (RM-38).

``tests/voice_isolation_plugin.py``'s autouse ``_voice_off`` (registered in
``tests/conftest.py``) empties ``BRAIN_WHISPER_MODEL`` and clears the cap for every
test. The probe checks that from inside a test; the regression test runs it in a CHILD
pytest whose environment exports both knobs. Both are ``nodb``. All values are synthetic.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from brain.ingest.whisper import HINT_OFF, resolve_whisper

pytestmark = pytest.mark.nodb

REPO = Path(__file__).resolve().parents[1]
PROBE = "tests/test_voice_isolation.py::test_voice_is_off_inside_every_test"

EXPORTED = {
    "BRAIN_WHISPER_MODEL": "/synthetic/models/ggml-base.en.bin",
    "BRAIN_WHISPER_MAX_MINUTES": "7",
}


def test_voice_is_off_inside_every_test() -> None:
    assert os.environ.get("BRAIN_WHISPER_MODEL") == ""
    assert "BRAIN_WHISPER_MAX_MINUTES" not in os.environ

    setup = resolve_whisper(
        os.environ, which=lambda name: "/synthetic/bin/whisper-cli", is_file=lambda p: True
    )

    assert setup.unavailable == HINT_OFF


def test_an_exported_model_does_not_reach_a_test() -> None:
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


def test_every_test_runs_inside_the_voice_guard(request: pytest.FixtureRequest) -> None:
    """The plugin is wired in through ``tests/conftest.py``'s ``pytest_plugins``."""
    assert "_voice_off" in request.fixturenames
