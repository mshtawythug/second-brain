"""``brain.settings.rerank``: the reranker's knobs (roadmap ticket RM-14a).

Every test passes a plain ``dict``, never ``os.environ``, so a variable
exported in the developer's shell cannot change a result. All values are
synthetic.
"""
from __future__ import annotations

import pytest

from brain.config import ConfigError
from brain.settings.rerank import (
    DEFAULT_RERANK_URL,
    LLAMACPP_RERANK_DEFAULT_MODEL,
    VOYAGE_RERANK_DEFAULT_MODEL,
    RerankSettings,
    load_rerank_settings,
)

#: A synthetic key; it must never appear in a repr or an error.
FAKE_KEY = "pa-synthetic-rerank-key-7f3e"


def test_defaults_are_off_and_local() -> None:
    settings = load_rerank_settings({})

    assert settings == RerankSettings(
        backend="none",
        depth=20,
        timeout_s=5.0,
        url="http://127.0.0.1:8081",
        model=LLAMACPP_RERANK_DEFAULT_MODEL,
        max_chars=1500,
        voyage_api_key=None,
    )
    assert DEFAULT_RERANK_URL == "http://127.0.0.1:8081"
    assert not settings.enabled


def test_model_defaults_per_backend() -> None:
    assert load_rerank_settings({"BRAIN_RERANKER": "llamacpp"}).model == "bge-reranker-v2-m3"
    voyage = load_rerank_settings({"BRAIN_RERANKER": "voyage", "VOYAGE_API_KEY": FAKE_KEY})
    assert voyage.model == VOYAGE_RERANK_DEFAULT_MODEL == "rerank-3"
    assert voyage.enabled


def test_explicit_model_wins() -> None:
    settings = load_rerank_settings(
        {"BRAIN_RERANKER": "llamacpp", "BRAIN_RERANK_MODEL": "synthetic-reranker"}
    )
    assert settings.model == "synthetic-reranker"


def test_backend_is_case_insensitive_and_refuses_unknown() -> None:
    assert load_rerank_settings({"BRAIN_RERANKER": " LlamaCpp "}).backend == "llamacpp"
    with pytest.raises(ConfigError, match="BRAIN_RERANKER"):
        load_rerank_settings({"BRAIN_RERANKER": "ollama"})


@pytest.mark.parametrize("value", ["1", "51", "x"])
def test_depth_bounds(value: str) -> None:
    with pytest.raises(ConfigError, match="BRAIN_RERANK_DEPTH"):
        load_rerank_settings({"BRAIN_RERANK_DEPTH": value})


@pytest.mark.parametrize(("value", "expected"), [("2", 2), ("50", 50)])
def test_depth_edges(value: str, expected: int) -> None:
    assert load_rerank_settings({"BRAIN_RERANK_DEPTH": value}).depth == expected


@pytest.mark.parametrize("value", ["0", "-1", "nan", "x"])
def test_timeout_must_be_positive(value: str) -> None:
    with pytest.raises(ConfigError, match="BRAIN_RERANK_TIMEOUT_S"):
        load_rerank_settings({"BRAIN_RERANK_TIMEOUT_S": value})


def test_timeout_parses() -> None:
    assert load_rerank_settings({"BRAIN_RERANK_TIMEOUT_S": "1.5"}).timeout_s == 1.5


@pytest.mark.parametrize("value", ["0", "99"])
def test_max_chars_bounds(value: str) -> None:
    with pytest.raises(ConfigError, match="BRAIN_RERANK_MAX_CHARS"):
        load_rerank_settings({"BRAIN_RERANK_MAX_CHARS": value})


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8081",
        "http://localhost:9000",
        "http://[::1]:8081",
        "https://127.0.0.1:8443/",
    ],
)
def test_loopback_urls_are_accepted(url: str) -> None:
    assert load_rerank_settings({"BRAIN_RERANK_URL": url}).url == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8081",
        "http://192.168.1.20:8081",
        "http://reranker.example.com:8081",
        "http://0.0.0.0:8081",
        "http://127.0.0.1.example.com:8081",
        "ftp://127.0.0.1:8081",
        "file:///tmp/socket",
        "127.0.0.1:8081",
        "http://user:pw@127.0.0.1:8081",
    ],
)
def test_a_non_loopback_url_is_a_config_error(url: str) -> None:
    with pytest.raises(ConfigError, match="BRAIN_RERANK_URL") as info:
        load_rerank_settings({"BRAIN_RERANK_URL": url})
    # The error names the variable, never the value (a URL can carry a credential).
    assert url not in str(info.value)


def test_url_is_checked_even_while_off() -> None:
    with pytest.raises(ConfigError, match="BRAIN_RERANK_URL"):
        load_rerank_settings({"BRAIN_RERANKER": "none", "BRAIN_RERANK_URL": "http://10.0.0.5"})


def test_voyage_needs_a_key() -> None:
    with pytest.raises(ConfigError, match="VOYAGE_API_KEY"):
        load_rerank_settings({"BRAIN_RERANKER": "voyage"})


def test_the_voyage_key_is_never_in_the_repr() -> None:
    settings = load_rerank_settings({"BRAIN_RERANKER": "voyage", "VOYAGE_API_KEY": FAKE_KEY})
    assert settings.voyage_api_key == FAKE_KEY
    assert FAKE_KEY not in repr(settings)


def test_the_key_is_not_read_for_a_local_backend() -> None:
    settings = load_rerank_settings({"BRAIN_RERANKER": "llamacpp", "VOYAGE_API_KEY": FAKE_KEY})
    assert settings.voyage_api_key is None
