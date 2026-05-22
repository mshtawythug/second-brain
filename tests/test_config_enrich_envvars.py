"""Tests for the Wave Q1-D enrichment env-var surface on :class:`Config`."""
from __future__ import annotations

from pathlib import Path

import pytest

from brain import config as config_module
from brain.config import (
    DEFAULT_ENRICH_MAX_INPUT_TOKENS,
    DEFAULT_ENRICH_MIN_TOKENS,
    DEFAULT_ENRICH_MODEL,
    DEFAULT_ENRICH_TIMEOUT_SECONDS,
    Config,
    ConfigError,
)

_TEST_DATABASE_URL = "postgresql://brain:brain@localhost:5434/second_brain_test"


@pytest.fixture()
def isolated_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Block all .env file sources so only os.environ values reach Config.load().

    Without this, T1.0's merged-dict + setdefault algorithm would re-inject
    keys from the developer's project .env (e.g. BRAIN_ENRICH_MODEL=gemma3:27b)
    after monkeypatch.delenv removes them from os.environ.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: tmp_path / "project.env")
    monkeypatch.setattr(
        config_module, "_brain_home_dotenv", lambda: tmp_path / "brain_home.env"
    )
    monkeypatch.delenv("BRAIN_HOME", raising=False)


def test_enrich_defaults_apply_when_envvars_match_defaults(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dotenv: None,
) -> None:
    """``Config`` parses each enrich env var into its documented default value.

    Originally this test used ``monkeypatch.delenv`` to "remove" each
    ``BRAIN_ENRICH_*`` var and then asserted that ``Config.load()`` fell
    back to ``DEFAULT_ENRICH_*``. That assertion was a lie: ``Config.load()``
    calls ``load_dotenv(..., override=False)`` internally, which silently
    re-reads the user's on-disk ``.env`` and re-populates any var the test
    just deleted (only ``override=False`` honors values already in the
    process env — but ``delenv`` removed them, so the ``.env`` line wins).

    The test only "passed" by accident — i.e. when the developer's local
    ``.env`` happened not to carry the relevant key. The moment someone
    set ``BRAIN_ENRICH_MODEL=gemma3:27b`` in ``.env`` (as happened during
    Wave Q2-SUMMARY-WIKI verification, 2026-05-11), the test deterministically
    failed with ``AssertionError: assert 'gemma3:27b' == 'llama3.1:8b'``.

    Fix: instead of trying to assert "no env var set" (which we can't
    guarantee while ``load_dotenv`` runs inside ``Config.load()``), assert
    the equivalent contract — when each env var is explicitly set to the
    documented default string, ``Config`` parses it into the typed default.
    This covers the two things that can actually break: (1) the parser
    drifts from the documented default constant, (2) the env-var name
    drifts from the one ``Config.load`` reads.
    """
    monkeypatch.setenv("BRAIN_ENRICH_MODEL", DEFAULT_ENRICH_MODEL)
    monkeypatch.setenv("BRAIN_ENRICH_MIN_TOKENS", str(DEFAULT_ENRICH_MIN_TOKENS))
    monkeypatch.setenv(
        "BRAIN_ENRICH_MAX_INPUT_TOKENS", str(DEFAULT_ENRICH_MAX_INPUT_TOKENS)
    )
    monkeypatch.setenv(
        "BRAIN_ENRICH_TIMEOUT_SECONDS", str(DEFAULT_ENRICH_TIMEOUT_SECONDS)
    )
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
