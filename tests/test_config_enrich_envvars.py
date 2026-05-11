"""Tests for the Wave Q1-D enrichment env-var surface on :class:`Config`."""
from __future__ import annotations

import pytest

from brain.config import (
    DEFAULT_ENRICH_MAX_INPUT_TOKENS,
    DEFAULT_ENRICH_MIN_TOKENS,
    DEFAULT_ENRICH_MODEL,
    DEFAULT_ENRICH_TIMEOUT_SECONDS,
    Config,
    ConfigError,
)

_TEST_DATABASE_URL = "postgresql://brain:brain@localhost:5433/second_brain_test"


def test_enrich_defaults_apply_when_envvars_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRAIN_ENRICH_MODEL", raising=False)
    monkeypatch.delenv("BRAIN_ENRICH_MIN_TOKENS", raising=False)
    monkeypatch.delenv("BRAIN_ENRICH_MAX_INPUT_TOKENS", raising=False)
    monkeypatch.delenv("BRAIN_ENRICH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    cfg = Config.load()
    assert cfg.enrich_model == DEFAULT_ENRICH_MODEL
    assert cfg.enrich_min_tokens == DEFAULT_ENRICH_MIN_TOKENS
    assert cfg.enrich_max_input_tokens == DEFAULT_ENRICH_MAX_INPUT_TOKENS
    assert cfg.enrich_timeout_seconds == DEFAULT_ENRICH_TIMEOUT_SECONDS


def test_enrich_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_ENRICH_MODEL", "mistral:7b")
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    assert Config.load().enrich_model == "mistral:7b"


def test_enrich_min_tokens_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_ENRICH_MIN_TOKENS", "10")
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    assert Config.load().enrich_min_tokens == 10


def test_enrich_min_tokens_zero_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_ENRICH_MIN_TOKENS", "0")
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    assert Config.load().enrich_min_tokens == 0


def test_enrich_min_tokens_negative_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_ENRICH_MIN_TOKENS", "-5")
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    with pytest.raises(ConfigError):
        Config.load()


def test_enrich_min_tokens_non_numeric_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_ENRICH_MIN_TOKENS", "x")
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    with pytest.raises(ConfigError):
        Config.load()


def test_enrich_max_input_tokens_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_ENRICH_MAX_INPUT_TOKENS", "8000")
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    assert Config.load().enrich_max_input_tokens == 8000


def test_enrich_max_input_tokens_zero_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_ENRICH_MAX_INPUT_TOKENS", "0")
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    with pytest.raises(ConfigError):
        Config.load()


def test_enrich_max_input_tokens_negative_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_ENRICH_MAX_INPUT_TOKENS", "-1")
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    with pytest.raises(ConfigError):
        Config.load()


def test_enrich_timeout_seconds_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_ENRICH_TIMEOUT_SECONDS", "30.5")
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    assert Config.load().enrich_timeout_seconds == 30.5


def test_enrich_timeout_seconds_non_numeric_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_ENRICH_TIMEOUT_SECONDS", "abc")
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    with pytest.raises(ConfigError):
        Config.load()


def test_enrich_timeout_seconds_zero_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_ENRICH_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    with pytest.raises(ConfigError):
        Config.load()
