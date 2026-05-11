"""Regression: ensure the test session can never connect to the prod DB.

On 2026-05-04 a full-suite run leaked 15 test fixtures into the real
``second_brain`` database. The vector was tests using
``CliRunner().invoke(app, ...)`` without first patching ``DATABASE_URL``
to the test DB — those tests fell through to ``brain.config.Config.load()``
which reads ``DATABASE_URL`` from ``.env`` (= prod).

The fix is a session-scoped autouse fixture in ``tests/conftest.py`` that
forces ``os.environ["DATABASE_URL"] = TEST_DATABASE_URL`` for the entire
session. This file pins the contract.
"""

from __future__ import annotations

import os

from brain.config import Config
from tests.conftest import TEST_DATABASE_URL


def test_database_url_env_var_points_at_test_db() -> None:
    """``os.environ["DATABASE_URL"]`` must be the test DB during pytest.

    If this fails, the autouse fixture in conftest didn't run. Any
    ``Config.load()`` call from inside a CLI test would then talk to prod.
    """
    assert os.environ.get("DATABASE_URL") == TEST_DATABASE_URL, (
        f"DATABASE_URL is {os.environ.get('DATABASE_URL')!r}; "
        f"expected {TEST_DATABASE_URL!r}. Did the autouse fixture in "
        "tests/conftest.py run?"
    )


def test_config_load_resolves_to_test_database_url() -> None:
    """``Config.load()`` reads from os.environ — must yield the test DB.

    Even though ``Config.load()`` reads ``.env`` files internally via
    ``dotenv_values`` + ``os.environ.setdefault``, it never overwrites an
    existing env var, so the autouse fixture's assignment wins.
    """
    cfg = Config.load()
    assert cfg.database_url == TEST_DATABASE_URL, (
        f"Config.load().database_url is {cfg.database_url!r}; "
        f"expected {TEST_DATABASE_URL!r}."
    )


def test_database_url_does_not_match_prod_naming() -> None:
    """Defense-in-depth: the test DB URL should never end in ``second_brain``.

    A common .env mistake is setting ``TEST_DATABASE_URL`` to the prod
    URL by accident. Catching that here is cheaper than losing data.
    """
    assert not TEST_DATABASE_URL.rstrip("/").endswith("/second_brain"), (
        f"TEST_DATABASE_URL must not point at prod database name; "
        f"got {TEST_DATABASE_URL!r}."
    )
