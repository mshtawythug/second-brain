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
from pathlib import Path

import pytest

from brain.config import Config
from tests.conftest import (
    TEST_DATABASE_URL,
    _assert_not_prod_db,
    _looks_like_prod_db,
)

# Built by concatenation so THIS file never contains the contiguous literal it
# forbids (otherwise the scan below would flag itself).
_STOCK_PORT_TEST_DB_LITERAL = "5433" + "/second_brain_test"


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


def test_no_test_file_hardcodes_stock_pg_port_for_test_db() -> None:
    """No test module may default the test DB to the stock-pgvector port (5433).

    The GraphRAG suite must run against the Apache-AGE test instance
    (docker-compose.age-test.yml, port 5434). The prod container on 5433 is
    stock pgvector with no ``age`` extension, so any test whose
    ``TEST_DATABASE_URL`` fallback hardcodes ``5433/second_brain_test`` would
    silently connect to the no-AGE instance whenever the env var is unset
    (CI, a fresh shell) — breaking every AGE test and splitting the suite
    across two databases. Each module's fallback (and conftest's) must use
    5434. This scan fails fast if the 5433 test-DB literal is reintroduced
    anywhere under ``tests/``.

    Note: ``5433/second_brain`` *without* ``_test`` (the live eval/canary
    harness ``LIVE_DB_URL``) is intentional read-only access to the prod
    corpus and is NOT matched by this guard.
    """
    tests_dir = Path(__file__).parent
    this_file = Path(__file__).resolve()
    offenders: list[str] = []
    for py_file in sorted(tests_dir.rglob("*.py")):
        if py_file.resolve() == this_file:
            continue  # this guard builds the needle dynamically; skip itself
        if _STOCK_PORT_TEST_DB_LITERAL in py_file.read_text(encoding="utf-8"):
            offenders.append(str(py_file.relative_to(tests_dir)))
    assert not offenders, (
        f"These test modules hardcode the stock-pgvector port for the test DB "
        f"({_STOCK_PORT_TEST_DB_LITERAL!r}); they must use 5434 (the Apache-AGE "
        f"test instance): {offenders}"
    )


# --- conftest prod-DB destructive-reset guard (DB-safety) -------------------
# conftest._assert_not_prod_db is the hard guard that aborts the destructive
# DROP SCHEMA reset if it would ever target the prod container. These pin its
# contract (prod port on localhost, or the prod db name on any host, is
# refused; the AGE test instance passes).


def test_prod_db_guard_refuses_prod_port_on_localhost() -> None:
    """The guard aborts on the prod container (localhost + prod port)."""
    prod_port = 5400 + 33  # avoid the contiguous "5433/second_brain_test" literal
    assert _looks_like_prod_db("localhost", prod_port, "second_brain") is True
    with pytest.raises(RuntimeError, match="PROD database"):
        _assert_not_prod_db("localhost", prod_port, "second_brain")


def test_prod_db_guard_refuses_prod_dbname_on_any_host() -> None:
    """Belt-and-suspenders: the prod database NAME is refused on any host/port."""
    assert _looks_like_prod_db("db.internal", 6000, "second_brain") is True
    with pytest.raises(RuntimeError, match="PROD database"):
        _assert_not_prod_db("db.internal", 6000, "second_brain")


def test_prod_db_guard_allows_age_test_instance() -> None:
    """The AGE test instance (5434 / *_test) passes the guard untouched."""
    assert _looks_like_prod_db("localhost", 5434, "second_brain_test") is False
    _assert_not_prod_db("localhost", 5434, "second_brain_test")  # must not raise


def test_prod_db_guard_scopes_prod_port_refusal_to_local_hosts() -> None:
    """The prod-port refusal is scoped to local hosts (the prod container).

    A remote host on the same port with a ``*_test`` db is not the prod box,
    so it is allowed — proving the guard isn't an overbroad port blocklist.
    """
    prod_port = 5400 + 33
    assert _looks_like_prod_db("remote.example", prod_port, "second_brain_test") is False
    _assert_not_prod_db("remote.example", prod_port, "second_brain_test")  # no raise
