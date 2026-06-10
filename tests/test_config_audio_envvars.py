"""Tests for the Plan 04 `brain audio` env-var surface on :class:`Config`.

Mirrors ``test_config_enrich_envvars.py``: the ``isolated_dotenv`` fixture blocks
all on-disk ``.env`` sources so only ``os.environ`` reaches ``Config.load()``,
then each ``BRAIN_AUDIO_*`` knob is asserted for its default, a valid override,
and the eager ``ConfigError`` validation path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain import config as config_module
from brain.config import (
    DEFAULT_AUDIO_MAX_INPUT_TOKENS,
    DEFAULT_AUDIO_MAX_TURNS,
    DEFAULT_AUDIO_SCRIPT_MODEL,
    DEFAULT_AUDIO_THEME_LIMIT,
    Config,
    ConfigError,
)

_TEST_DATABASE_URL = "postgresql://brain:brain@localhost:5434/second_brain_p04_test"


@pytest.fixture()
def isolated_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Block all .env file sources so only os.environ reaches Config.load()."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        config_module, "_project_dotenv", lambda: tmp_path / "project.env"
    )
    monkeypatch.setattr(
        config_module, "_brain_home_dotenv", lambda: tmp_path / "brain_home.env"
    )
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    for key in (
        "BRAIN_AUDIO_SCRIPT_MODEL",
        "BRAIN_AUDIO_MAX_TURNS",
        "BRAIN_AUDIO_MAX_INPUT_TOKENS",
        "BRAIN_AUDIO_THEME_LIMIT",
    ):
        monkeypatch.delenv(key, raising=False)


def test_audio_defaults(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: None
) -> None:
    cfg = Config.load()
    assert cfg.audio_script_model == DEFAULT_AUDIO_SCRIPT_MODEL
    assert cfg.audio_max_turns == DEFAULT_AUDIO_MAX_TURNS
    assert cfg.audio_max_input_tokens == DEFAULT_AUDIO_MAX_INPUT_TOKENS
    assert cfg.audio_theme_limit == DEFAULT_AUDIO_THEME_LIMIT


def test_audio_valid_overrides(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: None
) -> None:
    monkeypatch.setenv("BRAIN_AUDIO_SCRIPT_MODEL", "synthetic-audio-model")
    monkeypatch.setenv("BRAIN_AUDIO_MAX_TURNS", "8")
    monkeypatch.setenv("BRAIN_AUDIO_MAX_INPUT_TOKENS", "1500")
    monkeypatch.setenv("BRAIN_AUDIO_THEME_LIMIT", "2")
    cfg = Config.load()
    assert cfg.audio_script_model == "synthetic-audio-model"
    assert cfg.audio_max_turns == 8
    assert cfg.audio_max_input_tokens == 1500
    assert cfg.audio_theme_limit == 2


def test_audio_blank_model_uses_default(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: None
) -> None:
    monkeypatch.setenv("BRAIN_AUDIO_SCRIPT_MODEL", "   ")
    cfg = Config.load()
    assert cfg.audio_script_model == DEFAULT_AUDIO_SCRIPT_MODEL


@pytest.mark.parametrize("bad", ["3", "0", "-2", "notanint", "5.5"])
def test_audio_max_turns_rejects_non_positive_even(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: None, bad: str
) -> None:
    monkeypatch.setenv("BRAIN_AUDIO_MAX_TURNS", bad)
    with pytest.raises(ConfigError, match="BRAIN_AUDIO_MAX_TURNS"):
        Config.load()


@pytest.mark.parametrize("bad", ["0", "-1", "notanint"])
def test_audio_max_input_tokens_rejects_non_positive(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: None, bad: str
) -> None:
    monkeypatch.setenv("BRAIN_AUDIO_MAX_INPUT_TOKENS", bad)
    with pytest.raises(ConfigError, match="BRAIN_AUDIO_MAX_INPUT_TOKENS"):
        Config.load()


@pytest.mark.parametrize("bad", ["0", "-3", "notanint"])
def test_audio_theme_limit_rejects_non_positive(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: None, bad: str
) -> None:
    monkeypatch.setenv("BRAIN_AUDIO_THEME_LIMIT", bad)
    with pytest.raises(ConfigError, match="BRAIN_AUDIO_THEME_LIMIT"):
        Config.load()
