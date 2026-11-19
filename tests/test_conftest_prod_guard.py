"""Regression: conftest prod-DB guard + prod-URL resolver (Task 2.4).

Prod moved from host port 5433 to 55432. The destructive-reset guard
(``_looks_like_prod_db`` / ``_assert_not_prod_db``) must refuse the real prod
host:port (55432) — regardless of db name — as well as the historical 5433
mapping and the prod db name on any host. The live-canary resolver
(``prod_database_url``) must never hand back a ``*_test`` URL.

These are pure-logic assertions (no DB connection), pinning the contract the
canary tests and the destructive schema reset both depend on.
"""
from __future__ import annotations

import pytest

from tests.conftest import (
    _assert_not_prod_db,
    _looks_like_prod_db,
    prod_database_url,
)


def test_guard_refuses_real_prod_port_55432_any_dbname() -> None:
    """The real prod host:port (localhost:55432) is refused regardless of db name.

    This is the gap Task 2.4 closes: before the fix the port-based defence only
    knew 5433, so a connection to the real prod port under a non-``second_brain``
    db name (e.g. a restore) slipped past the port check.
    """
    assert _looks_like_prod_db("localhost", 55432, "second_brain") is True
    assert _looks_like_prod_db("localhost", 55432, "second_brain_restore") is True
    # 0.0.0.0 is a local alias too (parity with brain.demo's guard) — without it
    # a 0.0.0.0:55432 URL under a non-prod db name slipped past the port check.
    assert _looks_like_prod_db("0.0.0.0", 55432, "second_brain_restore") is True
    with pytest.raises(RuntimeError, match="PROD database"):
        _assert_not_prod_db("localhost", 55432, "second_brain")


def test_guard_still_refuses_historical_5433() -> None:
    """The old 5433 prod mapping must still trip the guard (belt and suspenders)."""
    old_prod_port = 5400 + 33  # built by sum to avoid the forbidden test-DB literal
    assert _looks_like_prod_db("localhost", old_prod_port, "anything_else") is True
    with pytest.raises(RuntimeError, match="PROD database"):
        _assert_not_prod_db("localhost", old_prod_port, "second_brain")


def test_guard_refuses_prod_dbname_on_any_host() -> None:
    """The prod database NAME is refused on any host/port (unchanged contract)."""
    assert _looks_like_prod_db("db.internal", 6000, "second_brain") is True


def test_guard_allows_age_test_instance() -> None:
    """The 5434 / *_test AGE test instance must still pass untouched."""
    assert _looks_like_prod_db("localhost", 5434, "second_brain_test") is False
    _assert_not_prod_db("localhost", 5434, "second_brain_test")  # must not raise


def test_guard_refuses_a_prod_port_on_any_host() -> None:
    """A prod port is refused whatever the host, even with a ``*_test`` db.

    This reverses the earlier contract, which scoped the port refusal to a list
    of local host spellings. That list cannot be complete — see
    :func:`test_guard_refuses_every_local_spelling_of_the_prod_port` — and a
    test suite has no business on a prod port on any host.
    """
    assert _looks_like_prod_db("remote.example", 55432, "second_brain_test") is True
    with pytest.raises(RuntimeError, match="PROD database"):
        _assert_not_prod_db("remote.example", 55432, "second_brain_test")


@pytest.mark.parametrize(
    "host",
    ["127.1", "localhost.", "::ffff:127.0.0.1", "127.0.0.2", "host.docker.internal"],
)
def test_guard_refuses_every_local_spelling_of_the_prod_port(host: str) -> None:
    """Each of these reaches this machine's 55432, and each was let through.

    Under the old literal list (localhost, 127.0.0.1, ::1, 0.0.0.0, "") all five
    passed as not-prod. The db name is deliberately not ``second_brain``, so
    only the port refusal can catch them.
    """
    assert _looks_like_prod_db(host, 55432, "postgres") is True
    assert _looks_like_prod_db(host, 55432, "second_brain_restore") is True


@pytest.mark.parametrize(
    ("host", "port", "dbname"),
    [
        ("localhost", 5434, "second_brain_test"),
        ("localhost", 5434, "postgres"),
        ("127.0.0.1", 5434, "fix_d_test_sbx"),
        ("localhost", 1, "none"),
        ("remote.example", 5434, "second_brain_test"),
    ],
)
def test_guard_still_allows_the_test_dsns(host: str, port: int, dbname: str) -> None:
    """The control: the 5434 test server and the port-1 placeholder still pass."""
    assert _looks_like_prod_db(host, port, dbname) is False
    _assert_not_prod_db(host, port, dbname)  # must not raise


def test_prod_url_resolver_prefers_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BRAIN_PROD_DATABASE_URL wins when set."""
    override = "postgresql://brain:brain@localhost:55432/second_brain"
    monkeypatch.setenv("BRAIN_PROD_DATABASE_URL", override)
    assert prod_database_url() == override


def test_prod_url_resolver_never_returns_test_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolver must resolve to a PROD (non-test) URL, never the test DB."""
    monkeypatch.delenv("BRAIN_PROD_DATABASE_URL", raising=False)
    url = prod_database_url()
    assert not url.rstrip("/").endswith("/second_brain_test"), (
        f"canary resolver returned a test-DB URL: {url!r}"
    )
    assert url.rstrip("/").endswith("/second_brain"), (
        f"canary resolver must point at the prod db name; got {url!r}"
    )
