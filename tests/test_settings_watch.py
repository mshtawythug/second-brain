"""``brain.settings.watch``: the saved-search knob (RM-15).

Every test passes a plain ``dict``, never ``os.environ``.
"""
from __future__ import annotations

import pytest

from brain.config import ConfigError
from brain.settings.watch import DEFAULT_MIN_COSINE, WatchSettings, load_watch_settings

pytestmark = pytest.mark.nodb


def test_default_min_cosine_is_045() -> None:
    assert load_watch_settings({}) == WatchSettings(min_cosine=0.45)
    assert DEFAULT_MIN_COSINE == 0.45


def test_min_cosine_is_read_and_bounded() -> None:
    assert load_watch_settings({"BRAIN_WATCH_MIN_COSINE": "0.6"}).min_cosine == 0.6
    assert load_watch_settings({"BRAIN_WATCH_MIN_COSINE": "0"}).min_cosine == 0.0
    assert load_watch_settings({"BRAIN_WATCH_MIN_COSINE": "1"}).min_cosine == 1.0


@pytest.mark.parametrize("raw", ["-0.1", "1.01", "nan", "high"])
def test_bad_min_cosine_is_a_config_error_naming_the_variable(raw: str) -> None:
    with pytest.raises(ConfigError, match="BRAIN_WATCH_MIN_COSINE"):
        load_watch_settings({"BRAIN_WATCH_MIN_COSINE": raw})
