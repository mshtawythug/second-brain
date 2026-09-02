"""Wave 4 — ``BRAIN_SNIPPET_MAX_CHARS``, the one knob the wave left behind.

Mirrors ``tests/test_config_mcp_ceilings.py``: the ``isolated_dotenv`` fixture
blocks every on-disk ``.env`` so only ``os.environ`` reaches ``Config.load()``.

Wave 4's actual subject — an Otsu cut over neighbour relevance, behind
``BRAIN_SNIPPET_ADAPTIVE`` / ``BRAIN_SNIPPET_SCORE_FLOOR`` — was built,
measured and removed; see ``brain.snippet_context``'s module docstring. This
knob survives because the measurement identified the character cap as one of
the two constraints that actually decide snippet size (47 of 55 live results,
85.5%, had a matched chunk that alone filled it). Making the binding constraint
configurable is the durable half of that finding, so it gets a real test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain import config as config_module
from brain.config import DEFAULT_SNIPPET_MAX_CHARS, Config, ConfigError
from brain.search import SNIPPET_LENGTH
from brain.snippet_context import (
    DEFAULT_SNIPPET_MAX_CHARS as HELPER_DEFAULT_SNIPPET_MAX_CHARS,
)
from tests.conftest import TEST_DATABASE_URL


@pytest.fixture()
def isolated_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Block all .env file sources so only os.environ reaches Config.load()."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        config_module, "_project_dotenv", lambda: tmp_path / "project.env"
    )
    monkeypatch.setattr(
        config_module, "_brain_home_dotenv", lambda: tmp_path / "brain_home.env"
    )
    monkeypatch.setattr(
        config_module,
        "_brain_home_root",
        lambda _config_file=None: tmp_path / "brain_home_root",
    )
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.delenv("BRAIN_SNIPPET_MAX_CHARS", raising=False)
    return tmp_path


def test_default_is_numerically_todays_constant(isolated_dotenv: Path) -> None:
    """Unset env → 1600, i.e. exactly the ``4 × SNIPPET_LENGTH`` it replaced.

    Turning an inlined constant into a knob must not move the number, or every
    snippet in every payload changes size before anyone asked it to.
    """
    cfg = Config.load()

    assert cfg.snippet_max_chars == DEFAULT_SNIPPET_MAX_CHARS
    assert cfg.snippet_max_chars == 4 * SNIPPET_LENGTH


def test_config_default_agrees_with_the_helper_default(isolated_dotenv: Path) -> None:
    """The two ``DEFAULT_SNIPPET_MAX_CHARS`` definitions must not drift.

    ``config`` holds the env default; ``snippet_context`` holds the parameter
    default for callers that pass no config at all (the ``brain.search``
    library entry point among them). If they disagree, a library caller and a
    CLI caller silently truncate at different lengths.
    """
    assert DEFAULT_SNIPPET_MAX_CHARS == HELPER_DEFAULT_SNIPPET_MAX_CHARS


def test_accepts_a_positive_int(
    isolated_dotenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_SNIPPET_MAX_CHARS", "800")
    assert Config.load().snippet_max_chars == 800


def test_blank_falls_back_to_the_default(
    isolated_dotenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_SNIPPET_MAX_CHARS", "   ")
    assert Config.load().snippet_max_chars == DEFAULT_SNIPPET_MAX_CHARS


@pytest.mark.parametrize("raw", ["0", "-1", "lots", "1.5"])
def test_rejects_non_positive_and_unparseable(
    isolated_dotenv: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """``0`` is NOT an opt-out here — it would blank every snippet in the payload.

    A search that returns results with empty snippets is indistinguishable, to
    an agent, from a search that found nothing useful. So the failure is at
    load time with the variable named, not at query time in silence.
    """
    monkeypatch.setenv("BRAIN_SNIPPET_MAX_CHARS", raw)
    with pytest.raises(ConfigError, match="BRAIN_SNIPPET_MAX_CHARS"):
        Config.load()
