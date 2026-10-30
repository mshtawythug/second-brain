"""Tests for the voice-memo knobs, :mod:`brain.settings.voice` (RM-38).

Every ``env`` is a plain dict: the leaf reads only what it is given. A refusal names the
variable and never quotes the value.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.config import ConfigError
from brain.settings.voice import (
    DEFAULT_MAX_MINUTES,
    MAX_MAX_MINUTES,
    VoiceSettings,
    load_voice_settings,
)

ECHO_CANARY = "zz-value-that-must-not-be-echoed"


def test_unset_means_off_with_the_default_cap() -> None:
    settings = load_voice_settings({})

    assert settings == VoiceSettings(model=None, max_minutes=DEFAULT_MAX_MINUTES)
    assert DEFAULT_MAX_MINUTES == 60


def test_blank_model_is_off() -> None:
    assert load_voice_settings({"BRAIN_WHISPER_MODEL": "   "}).model is None


def test_an_absolute_model_path_is_kept() -> None:
    settings = load_voice_settings({"BRAIN_WHISPER_MODEL": "/opt/models/ggml-base.en.bin"})

    assert settings.model == Path("/opt/models/ggml-base.en.bin")


def test_a_tilde_model_path_is_expanded() -> None:
    settings = load_voice_settings({"BRAIN_WHISPER_MODEL": "~/models/ggml-base.en.bin"})

    assert settings.model == Path("~/models/ggml-base.en.bin").expanduser()
    assert settings.model is not None and settings.model.is_absolute()


def test_a_relative_model_path_is_refused_without_quoting_it() -> None:
    with pytest.raises(ConfigError, match="BRAIN_WHISPER_MODEL") as info:
        load_voice_settings({"BRAIN_WHISPER_MODEL": f"models/{ECHO_CANARY}.bin"})

    assert ECHO_CANARY not in str(info.value)


@pytest.mark.parametrize("raw", ["1", "90", str(MAX_MAX_MINUTES)])
def test_max_minutes_in_range(raw: str) -> None:
    assert load_voice_settings({"BRAIN_WHISPER_MAX_MINUTES": raw}).max_minutes == int(raw)


@pytest.mark.parametrize("raw", ["0", "-5", str(MAX_MAX_MINUTES + 1), ECHO_CANARY, "1.5"])
def test_max_minutes_out_of_range_or_junk_is_refused(raw: str) -> None:
    with pytest.raises(ConfigError, match="BRAIN_WHISPER_MAX_MINUTES") as info:
        load_voice_settings({"BRAIN_WHISPER_MAX_MINUTES": raw})

    assert ECHO_CANARY not in str(info.value)


def test_max_seconds_follows_max_minutes() -> None:
    assert load_voice_settings({"BRAIN_WHISPER_MAX_MINUTES": "2"}).max_seconds == 120.0
