"""A ``BRAIN_BRIEF_DELIVERY`` exported in the shell never reaches a test (RM-16).

``tests/delivery_isolation_plugin.py``'s autouse ``_delivery_off`` (registered in
``tests/conftest.py``) sets ``BRAIN_BRIEF_DELIVERY=none`` and clears the other delivery knobs.
The probe checks that from inside a test, through the real settings loader; the regression test
runs it in a CHILD pytest whose environment exports email delivery and every knob. Both are
``nodb``. All values are synthetic.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from brain.settings.brief_delivery import load_brief_delivery_settings

pytestmark = pytest.mark.nodb

REPO = Path(__file__).resolve().parents[1]
PROBE = "tests/test_delivery_isolation.py::test_delivery_is_off_inside_every_test"

EXPORTED = {
    "BRAIN_BRIEF_DELIVERY": "email",
    "BRAIN_USER_EMAIL": "owner@example.invalid",
    "BRAIN_BRIEF_SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T0/B0/isolation-probe",
    "BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS": "true",
}


def test_delivery_is_off_inside_every_test() -> None:
    assert os.environ.get("BRAIN_BRIEF_DELIVERY") == "none"
    assert "BRAIN_BRIEF_SLACK_WEBHOOK_URL" not in os.environ
    assert "BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS" not in os.environ

    settings = load_brief_delivery_settings(os.environ)

    assert settings.enabled is False


def test_an_exported_delivery_does_not_reach_a_test() -> None:
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


def test_every_test_runs_inside_the_delivery_guard(request: pytest.FixtureRequest) -> None:
    assert "_delivery_off" in request.fixturenames
