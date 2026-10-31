"""``tests/browser_isolation_plugin.py`` is loaded and does what it says (RM-41)."""
from __future__ import annotations

from pathlib import Path

from brain import cli_ingest_web
from tests import conftest
from tests.browser_isolation_plugin import NOWHERE


def test_the_plugin_is_registered_in_conftest() -> None:
    assert "tests.browser_isolation_plugin" in conftest.pytest_plugins


def test_the_default_home_is_a_path_that_does_not_exist_and_is_not_the_real_home() -> None:
    home = cli_ingest_web.home_dir()

    assert home.name == NOWHERE
    assert not home.exists()
    assert home != Path.home()


def test_the_fetch_resolver_answers_nothing() -> None:
    assert list(cli_ingest_web.fetch_resolver("example.com", 443)) == []
