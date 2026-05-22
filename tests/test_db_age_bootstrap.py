"""Tests for the Apache AGE session + init bootstrap helpers in brain.db (G0-2).

All run against the AGE test instance (``TEST_DATABASE_URL``, port 5434). The
``test_db`` fixture leaves the ``age`` extension installed (``conftest``
re-creates it on every reset) but drops the canonical ``brain_graph`` graph, so
each test starts from a clean graph state mirroring a fresh ``brain init``.
"""
import os
from unittest.mock import patch

import psycopg
import pytest

from brain.db import (
    DEFAULT_GRAPH_NAME,
    age_extension_available,
    bootstrap_age,
    connect,
    connect_age,
    load_age,
)
from brain.errors import AgeBootstrapError, BrainError
from tests.conftest import _reset_age_graph

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _graph_exists(conn: psycopg.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s", (name,)
    ).fetchone()
    return row is not None


def _graph_count(conn: psycopg.Connection, name: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", (name,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def _cypher_one(conn: psycopg.Connection) -> str:
    """Trivial fully-qualified Cypher round-trip on ``brain_graph``.

    Returns the agtype text of ``RETURN 1`` ("1"). Uses a literal graph name
    because AGE rewrites ``cypher()`` at parse time and won't accept the graph
    name or query as a bound parameter.
    """
    row = conn.execute(
        "SELECT * FROM ag_catalog.cypher('brain_graph', $$ RETURN 1 $$) "
        "AS (n ag_catalog.agtype)"
    ).fetchone()
    assert row is not None
    return str(row[0])


# --- bootstrap_age ----------------------------------------------------------


def test_bootstrap_age_creates_graph(test_db: psycopg.Connection) -> None:
    """Fresh DB (graph dropped by reset) → bootstrap creates a usable graph."""
    assert not _graph_exists(test_db, DEFAULT_GRAPH_NAME)

    created = bootstrap_age(test_db)

    assert created is True
    assert _graph_exists(test_db, DEFAULT_GRAPH_NAME)
    # The created graph is immediately usable for a Cypher round-trip.
    assert _cypher_one(test_db) == "1"


def test_bootstrap_age_is_idempotent(test_db: psycopg.Connection) -> None:
    """Re-running bootstrap is a safe no-op: second call returns False, no error.

    Guards against ``create_graph`` raising "graph already exists" — the reason
    re-running ``brain init`` must not blow up.
    """
    first = bootstrap_age(test_db)
    second = bootstrap_age(test_db)

    assert first is True
    assert second is False
    # Exactly one graph row — not duplicated.
    assert _graph_count(test_db, DEFAULT_GRAPH_NAME) == 1
    assert _cypher_one(test_db) == "1"


def test_bootstrap_age_custom_graph_name(test_db: psycopg.Connection) -> None:
    """The graph name is parameterizable (multi-tenant BRAIN_GRAPH_NAME later)."""
    name = "g0_2_custom_graph"
    try:
        created = bootstrap_age(test_db, graph_name=name)

        assert created is True
        assert _graph_exists(test_db, name)
        # Re-run with the same custom name is a no-op too.
        assert bootstrap_age(test_db, graph_name=name) is False
    finally:
        # Throwaway graph lives in ag_catalog and survives the schema reset —
        # drop it explicitly so it doesn't leak into later tests.
        if _graph_exists(test_db, name):
            test_db.execute("SELECT ag_catalog.drop_graph(%s, true)", (name,))


def test_bootstrap_age_requires_autocommit() -> None:
    """A non-autocommit connection is rejected before any DDL runs.

    AGE catalog DDL does not behave well inside an open transaction under
    psycopg v3, so the helper fails fast with a clear BrainError.
    """
    with connect(TEST_DATABASE_URL) as conn:
        assert conn.autocommit is False
        with pytest.raises(BrainError, match="autocommit"):
            bootstrap_age(conn)
        # No graph was created by the rejected call.
        conn.rollback()


# --- age_extension_available ------------------------------------------------
# The init no-AGE guard (G0 wave-boundary fix #1) probes
# ``pg_available_extensions`` to decide whether to bootstrap AGE. On the AGE
# test image the ``age`` control file is present, so the probe returns True; the
# False path (a stock pgvector image) is exercised at the CLI level in
# ``test_cli_init`` because we cannot remove a control file from the running
# server.


def test_age_extension_available_true_on_age_image(
    test_db: psycopg.Connection,
) -> None:
    """The AGE test image ships the ``age`` control file → probe returns True."""
    assert age_extension_available(test_db) is True


def test_age_extension_available_clears_txn_on_non_autocommit() -> None:
    """On a default (autocommit=False) connection the probe leaves no open txn.

    The SELECT opens an implicit transaction under psycopg's default; the probe
    must roll it back so the caller can still flip autocommit (the exact
    sequence ``brain init`` is unaffected by, but other callers rely on).
    """
    with connect(TEST_DATABASE_URL) as conn:
        assert conn.autocommit is False
        assert age_extension_available(conn) is True
        # No lingering open transaction — flipping autocommit must not raise.
        conn.autocommit = True


def test_age_extension_available_rolls_back_aborted_txn() -> None:
    """A GENUINELY aborted txn is cleared by the probe's except-path rollback.

    Arrange a real aborted transaction (a failing statement on a non-autocommit
    conn), so the probe's own ``pg_available_extensions`` SELECT raises
    ``InFailedSqlTransaction``. The except-path rollback is then OBSERVABLY
    required: only because it runs can a follow-up query on the SAME connection
    succeed afterwards — without it the connection would stay poisoned. No mock;
    the aborted state is real.
    """
    with connect(TEST_DATABASE_URL) as conn:
        assert conn.autocommit is False
        # Real failure → the transaction is now in an aborted state.
        with pytest.raises(psycopg.Error):
            conn.execute("SELECT * FROM _no_such_table_for_abort_probe")
        # The probe's SELECT raises on the aborted txn; its except path rolls
        # back, then re-raises. Without that rollback the conn stays unusable.
        with pytest.raises(psycopg.Error):
            age_extension_available(conn)
        # Proof the rollback ran: a fresh query on the SAME conn now succeeds.
        assert conn.execute("SELECT 1").fetchone() == (1,)


# --- load_age ---------------------------------------------------------------


def test_load_age_enables_cypher_on_fresh_connection(
    test_db: psycopg.Connection,
) -> None:
    """A connection that calls load_age can run Cypher; the extension is present.

    ``test_db`` bootstraps the graph (so there is something to query); a
    *separate* fresh connection then proves ``load_age`` alone makes
    ``cypher()`` callable on that session.
    """
    bootstrap_age(test_db)

    with connect(TEST_DATABASE_URL) as conn:
        conn.autocommit = True
        assert load_age(conn) is True
        assert _cypher_one(conn) == "1"


def test_load_age_does_not_leak_global_search_path(
    test_db: psycopg.Connection,
) -> None:
    """load_age must NOT prepend ag_catalog to search_path (option-b design).

    Proves a representative non-AGE query is unaffected: after load_age an
    unqualified ``CREATE TABLE`` still lands in ``public`` (it would land in
    ``ag_catalog`` if the global path had been leaked).
    """
    assert load_age(test_db) is True

    row = test_db.execute("SHOW search_path").fetchone()
    assert row is not None
    assert "ag_catalog" not in str(row[0])

    # Representative non-AGE DDL targets public, not ag_catalog.
    test_db.execute("CREATE TABLE g0_2_probe_tbl (x int)")
    schema_row = test_db.execute(
        "SELECT table_schema FROM information_schema.tables "
        "WHERE table_name = 'g0_2_probe_tbl'"
    ).fetchone()
    assert schema_row is not None
    assert schema_row[0] == "public"


def test_load_age_returns_false_when_extension_absent(
    test_db: psycopg.Connection,
) -> None:
    """Bootstrap window: extension not yet installed → load_age no-ops (False).

    Drops the ``age`` extension to simulate a pre-``brain init`` database, then
    restores it (``conftest`` would re-create it on the next reset regardless,
    but we leave the DB tidy per the Scout Law).
    """
    test_db.execute("DROP EXTENSION IF EXISTS age CASCADE")
    try:
        assert load_age(test_db) is False
    finally:
        test_db.execute("CREATE EXTENSION IF NOT EXISTS age CASCADE")


def test_load_age_clears_txn_so_autocommit_can_flip(
    test_db: psycopg.Connection,
) -> None:
    """On a default (autocommit=False) connection, load_age leaves no open txn.

    ``LOAD`` is a process-level effect that survives the rollback, so the
    session can flip to autocommit afterwards and Cypher still works — the
    exact sequence the graph backend relies on.
    """
    bootstrap_age(test_db)  # ensure brain_graph exists to query

    with connect(TEST_DATABASE_URL) as conn:
        assert conn.autocommit is False
        assert load_age(conn) is True
        # No open transaction lingers — flipping autocommit must not raise.
        conn.autocommit = True
        assert _cypher_one(conn) == "1"


# --- connect_age ------------------------------------------------------------


def test_connect_age_loads_age_and_enables_cypher(
    test_db: psycopg.Connection,
) -> None:
    """connect_age yields a connection with AGE already loaded (no repeat LOAD)."""
    bootstrap_age(test_db)

    with connect_age(TEST_DATABASE_URL) as conn:
        # No explicit load_age call here — connect_age did it for us.
        assert _cypher_one(conn) == "1"


def test_connect_age_tolerates_missing_extension(
    test_db: psycopg.Connection,
) -> None:
    """On a fresh DB (no age extension) connect_age still yields a usable conn."""
    test_db.execute("DROP EXTENSION IF EXISTS age CASCADE")
    try:
        with connect_age(TEST_DATABASE_URL) as conn:
            row = conn.execute("SELECT 1").fetchone()
            assert row == (1,)
    finally:
        test_db.execute("CREATE EXTENSION IF NOT EXISTS age CASCADE")


# --- error wrapping ---------------------------------------------------------
# The public bootstrap helpers must never let a raw ``psycopg.Error`` escape —
# a DB-level failure surfaces as :class:`AgeBootstrapError` (a BrainError
# subclass) with the original error preserved as ``__cause__``. We force a real
# ``psycopg.Error`` via an ``unittest.mock`` test double on ``conn.execute``
# (an allowed test double per CLAUDE.md, not banned monkey-patching).


def test_bootstrap_age_wraps_psycopg_error_in_brain_error(
    test_db: psycopg.Connection,
) -> None:
    """A ``psycopg.Error`` during AGE catalog DDL surfaces as AgeBootstrapError."""
    boom = psycopg.OperationalError("simulated AGE catalog failure")
    with (
        patch.object(test_db, "execute", side_effect=boom),
        pytest.raises(AgeBootstrapError) as excinfo,
    ):
        bootstrap_age(test_db)

    # It is a BrainError (so CLI/MCP can map it) and preserves the cause.
    assert isinstance(excinfo.value, BrainError)
    assert excinfo.value.__cause__ is boom


def test_load_age_wraps_psycopg_error_in_brain_error(
    test_db: psycopg.Connection,
) -> None:
    """A ``psycopg.Error`` during ``LOAD 'age'`` surfaces as AgeBootstrapError."""
    boom = psycopg.OperationalError("simulated LOAD failure")
    with (
        patch.object(test_db, "execute", side_effect=boom),
        pytest.raises(AgeBootstrapError) as excinfo,
    ):
        load_age(test_db)

    assert isinstance(excinfo.value, BrainError)
    assert excinfo.value.__cause__ is boom


def test_load_age_wraps_load_failure_after_successful_probe(
    test_db: psycopg.Connection,
) -> None:
    """Failure at ``LOAD 'age'`` (extension probe already succeeded) still wraps.

    The mock above fails on the FIRST execute (the probe); this drives the
    failure one statement later — the probe runs for real (extension present)
    and only ``LOAD`` raises — covering load_age's deeper branch.
    """
    real_execute = test_db.execute
    boom = psycopg.OperationalError("simulated LOAD failure")

    def _fail_on_load(query: object, *args: object, **kwargs: object) -> object:
        if "LOAD" in str(query):
            raise boom
        return real_execute(query, *args, **kwargs)

    with (
        patch.object(test_db, "execute", side_effect=_fail_on_load),
        pytest.raises(AgeBootstrapError) as excinfo,
    ):
        load_age(test_db)

    assert excinfo.value.__cause__ is boom


def test_bootstrap_age_wraps_create_graph_failure(
    test_db: psycopg.Connection,
) -> None:
    """Failure at ``create_graph`` (extension + graph-probe already ran) wraps.

    Exercises the deepest bootstrap_age branch: CREATE EXTENSION, LOAD and the
    ``ag_graph`` existence probe all run for real (the graph is absent after the
    per-test reset, so the guard does NOT short-circuit), and only the
    ``create_graph`` call raises.
    """
    assert not _graph_exists(test_db, DEFAULT_GRAPH_NAME)
    real_execute = test_db.execute
    boom = psycopg.OperationalError("simulated create_graph failure")

    def _fail_on_create_graph(query: object, *args: object, **kwargs: object) -> object:
        if "create_graph" in str(query):
            raise boom
        return real_execute(query, *args, **kwargs)

    with (
        patch.object(test_db, "execute", side_effect=_fail_on_create_graph),
        pytest.raises(AgeBootstrapError) as excinfo,
    ):
        bootstrap_age(test_db)

    assert excinfo.value.__cause__ is boom
    # The failed create_graph left no partial graph behind.
    assert not _graph_exists(test_db, DEFAULT_GRAPH_NAME)


def test_load_age_rolls_back_on_failure_when_not_autocommit() -> None:
    """On a non-autocommit connection, a LOAD failure rolls back, then wraps.

    Covers load_age's except-path rollback (autocommit=False): the aborted
    transaction is cleared so the connection stays usable — proven by flipping
    autocommit afterwards without error.
    """
    boom = psycopg.OperationalError("simulated LOAD failure")
    with connect(TEST_DATABASE_URL) as conn:
        assert conn.autocommit is False
        real_execute = conn.execute

        def _fail_on_load(query: object, *args: object, **kwargs: object) -> object:
            if "LOAD" in str(query):
                raise boom
            return real_execute(query, *args, **kwargs)

        with (
            patch.object(conn, "execute", side_effect=_fail_on_load),
            pytest.raises(AgeBootstrapError) as excinfo,
        ):
            load_age(conn)
        assert excinfo.value.__cause__ is boom
        # Rollback cleared the aborted txn → flipping autocommit must not raise.
        conn.autocommit = True


# --- conftest AGE reset (G0-5) ----------------------------------------------
# The per-test ``test_db`` fixture runs ``_reset_age_graph`` (via
# ``_reset_schema_and_migrate``) at the top of every DB test. AGE graph state
# lives in ``ag_catalog`` + a per-graph schema — both OUTSIDE ``public`` — so the
# ``DROP SCHEMA public CASCADE`` in the relational reset does NOT clear them.
# These tests prove the dedicated AGE reset is what keeps graph state from
# leaking across tests.


def test_reset_age_graph_drops_existing_graph(test_db: psycopg.Connection) -> None:
    """``_reset_age_graph`` drops a pre-existing ``brain_graph``.

    Self-contained proof of the reset mechanism: bootstrap the canonical graph,
    confirm it exists, run the exact conftest helper the fixture applies per
    test, then confirm the graph is gone — i.e. a graph created in one test
    cannot survive into the next.
    """
    bootstrap_age(test_db)
    assert _graph_exists(test_db, DEFAULT_GRAPH_NAME)

    _reset_age_graph(test_db)

    assert not _graph_exists(test_db, DEFAULT_GRAPH_NAME)


def test_reset_age_graph_is_safe_when_graph_absent(
    test_db: psycopg.Connection,
) -> None:
    """``_reset_age_graph`` is a no-op when ``brain_graph`` is already absent.

    The per-test reset must not raise on a clean graph state (the existence
    guard short-circuits the ``drop_graph`` call), so re-resetting is harmless.
    """
    assert not _graph_exists(test_db, DEFAULT_GRAPH_NAME)

    _reset_age_graph(test_db)  # must not raise

    assert not _graph_exists(test_db, DEFAULT_GRAPH_NAME)


def test_reset_age_graph_does_not_leak_search_path(
    test_db: psycopg.Connection,
) -> None:
    """The reset scopes ``ag_catalog`` to its AGE statements (no global leak).

    After ``_reset_age_graph`` returns, ``search_path`` must not carry
    ``ag_catalog`` — otherwise the subsequent ``run_migrations`` DDL would land
    in the wrong schema. Mirrors the load_age non-leak contract.
    """
    bootstrap_age(test_db)
    _reset_age_graph(test_db)

    row = test_db.execute("SHOW search_path").fetchone()
    assert row is not None
    assert "ag_catalog" not in str(row[0])
