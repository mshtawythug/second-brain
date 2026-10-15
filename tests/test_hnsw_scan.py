"""Shared HNSW scan settings: the design-D values, scoped to a block, never leaked.

:mod:`brain.hnsw_scan` is the one place the scan settings and their read / set /
restore protocol live. The search vector leg, the related-docs vector leg and
``connect``'s KNN all run their query inside :func:`brain.hnsw_scan.scoped_settings`.
These tests pin the protocol itself; each leg's own test file pins that the leg
uses it.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.hnsw_scan import (
    HNSW_EF_SEARCH,
    RELAXED_SCAN_SETTINGS,
    STRICT_SCAN_SETTINGS,
    scan_settings,
    scoped_settings,
)
from tests.hnsw_fixtures import current_scan_settings


def _load_pgvector(conn: psycopg.Connection[Any]) -> None:
    """Define the ``hnsw.*`` variables: they appear when pgvector's library loads."""
    conn.execute("SELECT '[1,2]'::vector <=> '[2,1]'::vector").fetchone()


def test_scan_settings_pin_the_design_d_values() -> None:
    """ef_search 1000 (pgvector's maximum), the named mode, sequential scans off."""
    assert HNSW_EF_SEARCH == 1000
    assert RELAXED_SCAN_SETTINGS == (
        ("hnsw.ef_search", "1000"),
        ("hnsw.iterative_scan", "relaxed_order"),
        ("enable_seqscan", "off"),
    )
    assert STRICT_SCAN_SETTINGS == (
        ("hnsw.ef_search", "1000"),
        ("hnsw.iterative_scan", "strict_order"),
        ("enable_seqscan", "off"),
    )
    assert scan_settings("relaxed_order") == RELAXED_SCAN_SETTINGS
    assert scan_settings("strict_order") == STRICT_SCAN_SETTINGS


@pytest.mark.parametrize("bad", ["off", "", "RELAXED_ORDER"])
def test_scan_settings_rejects_an_unknown_mode(bad: Any) -> None:
    """``off`` would silently restore the under-returning scan; it is refused."""
    with pytest.raises(ValueError, match="iterative_scan"):
        scan_settings(bad)


def test_scoped_settings_are_live_inside_the_block(
    test_db: psycopg.Connection[Any],
) -> None:
    _load_pgvector(test_db)
    with scoped_settings(test_db, STRICT_SCAN_SETTINGS):
        assert current_scan_settings(test_db) == ("1000", "strict_order", "off")


def test_scoped_settings_do_not_leak_from_an_autocommit_connection(
    test_db: psycopg.Connection[Any],
) -> None:
    _load_pgvector(test_db)
    before = current_scan_settings(test_db)
    assert before == ("40", "off", "on")  # pgvector's and Postgres's defaults
    with scoped_settings(test_db, RELAXED_SCAN_SETTINGS):
        pass
    assert current_scan_settings(test_db) == before


def test_scoped_settings_restore_inside_the_callers_transaction(
    test_db: psycopg.Connection[Any],
) -> None:
    """Inside a caller's transaction the block is a savepoint, and a RELEASED
    savepoint keeps local settings. The explicit restore is what puts the caller's
    own values back, and they must still be LOCAL: they end at the caller's COMMIT."""
    _load_pgvector(test_db)
    with test_db.transaction():
        test_db.execute("SET LOCAL hnsw.ef_search = 77")
        test_db.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        before = current_scan_settings(test_db)
        with scoped_settings(test_db, STRICT_SCAN_SETTINGS):
            assert current_scan_settings(test_db)[0] == "1000"
        assert current_scan_settings(test_db) == before
        assert before[:2] == ("77", "relaxed_order")
    assert current_scan_settings(test_db) == ("40", "off", "on")


def test_scoped_settings_revert_when_the_block_raises(
    test_db: psycopg.Connection[Any],
) -> None:
    """The restore never runs on an exception; the savepoint rollback reverts them."""
    _load_pgvector(test_db)

    class _Boom(Exception):
        pass

    with test_db.transaction():
        before = current_scan_settings(test_db)
        with pytest.raises(_Boom), scoped_settings(test_db, STRICT_SCAN_SETTINGS):
            raise _Boom
        assert current_scan_settings(test_db) == before
    assert current_scan_settings(test_db) == ("40", "off", "on")


def test_scoped_settings_restore_on_a_fresh_session_before_pgvector_is_loaded(
    test_db: psycopg.Connection[Any],
) -> None:
    """A new session has no ``hnsw.*`` variables until pgvector loads, so the prior
    value reads as NULL; the restore must then fall back to the session default."""
    from tests.conftest import TEST_DATABASE_URL

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as fresh, fresh.transaction():
        with scoped_settings(fresh, RELAXED_SCAN_SETTINGS):
            _load_pgvector(fresh)
        assert current_scan_settings(fresh) == ("40", "off", "on")


@pytest.mark.parametrize("count", [1, 2])
def test_scoped_settings_handles_any_number_of_settings(
    test_db: psycopg.Connection[Any], count: int
) -> None:
    _load_pgvector(test_db)
    settings = (("hnsw.ef_search", "123"), ("enable_seqscan", "off"))[:count]
    before = current_scan_settings(test_db)
    with scoped_settings(test_db, settings):
        live = current_scan_settings(test_db)
        assert live[0] == "123"
        assert live[2] == ("off" if count == 2 else "on")
    assert current_scan_settings(test_db) == before


def test_scoped_settings_rejects_an_empty_tuple(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(ValueError, match="setting"), scoped_settings(test_db, ()):
        pass
