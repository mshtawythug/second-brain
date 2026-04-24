"""Tests for brain.config — env loading."""
import pytest

from brain.config import Config, ConfigError


def test_loads_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    monkeypatch.setenv("VOYAGE_API_KEY", "vk-test")
    cfg = Config.load()
    assert cfg.database_url == "postgresql://x:y@h:5432/d"
    assert cfg.voyage_api_key == "vk-test"


def test_missing_database_url_raises(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("VOYAGE_API_KEY", "vk-test")
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        Config.load()


def test_missing_voyage_key_raises(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="VOYAGE_API_KEY"):
        Config.load()
