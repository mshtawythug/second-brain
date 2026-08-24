"""Real-DB tests for migration ``028_search_query_tokens.sql``.

Mirrors ``tests/test_migration_024_search_duration.py`` — the nearest prior
"nullable INT ride-along on ``search_queries``" migration.

What 028 adds is two columns whose *nullability* is the contract, not an
implementation detail: every pre-028 row is honestly NULL, and a consumer that
reads NULL as ``0`` would report that retrieval was free. All rows here are
synthetic.
"""
from __future__ import annotations

import os
import time
from typing import Any

import psycopg
import pytest

from brain.db import migrations_dir

_TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

_MIGRATION_028 = migrations_dir() / "028_search_query_tokens.sql"


def _migration_sql() -> str:
    """The migration exactly as ``brain init`` will execute it."""
    return _MIGRATION_028.read_text()


#: ``information_schema.columns`` spans EVERY schema in the database, so both
#: lookups below pin the schema as well as the table. Without it the question
#: asked is "does SOME table called search_queries have this column", which is
#: not the question migration 028 answers — and the ``count(*) == 1``
#: re-runnability assertion would break for a reason unrelated to the
#: migration. This DB already carries a second schema (Apache AGE's
#: ``brain_graph``); it holds no such table today, and
#: ``test_column_lookups_are_scoped_to_the_public_schema`` is what keeps that a
#: coincidence rather than a dependency.
_PUBLIC_SEARCH_QUERIES = "table_schema = 'public' AND table_name = 'search_queries'"


def _column(conn: psycopg.Connection[Any], name: str) -> tuple[str, str] | None:
    row = conn.execute(
        f"""
        SELECT data_type, is_nullable
        FROM information_schema.columns
        WHERE {_PUBLIC_SEARCH_QUERIES} AND column_name = %s
        """,
        (name,),
    ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def _column_count(conn: psycopg.Connection[Any], name: str) -> int:
    """How many columns of this name the *public* ``search_queries`` has."""
    row = conn.execute(
        f"""
        SELECT count(*)
        FROM information_schema.columns
        WHERE {_PUBLIC_SEARCH_QUERIES} AND column_name = %s
        """,
        (name,),
    ).fetchone()
    assert row is not None  # count(*) always yields one row
    return int(row[0])


def test_payload_tokens_column_exists_and_is_nullable(
    test_db: psycopg.Connection[Any],
) -> None:
    """The MEASURED half: nullable INT, so "not measured" is representable."""
    column = _column(test_db, "payload_tokens")

    assert column is not None, "migration 028 must add payload_tokens"
    assert column[0] == "integer"
    assert column[1] == "YES", "payload_tokens must stay nullable"


def test_baseline_tokens_column_exists_and_is_nullable(
    test_db: psycopg.Connection[Any],
) -> None:
    """The COUNTERFACTUAL half: nullable, and NULL is its normal state."""
    column = _column(test_db, "baseline_tokens")

    assert column is not None, "migration 028 must add baseline_tokens"
    assert column[0] == "integer"
    assert column[1] == "YES", "baseline_tokens must stay nullable"


def test_pre_028_rows_read_back_as_null(
    test_db: psycopg.Connection[Any],
) -> None:
    """A row written without token columns keeps NULL — 0 would be a lie."""
    # Arrange / Act — the shape of every row written before this migration.
    test_db.execute(
        "INSERT INTO search_queries (query, result_count, source) "
        "VALUES (%s, %s, %s)",
        ("synthetic pre-028 query", 3, "cli"),
    )

    # Assert
    row = test_db.execute(
        "SELECT payload_tokens, baseline_tokens FROM search_queries "
        "WHERE query = %s",
        ("synthetic pre-028 query",),
    ).fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None


def test_no_check_constraint_mirrors_the_python_gate(
    test_db: psycopg.Connection[Any],
) -> None:
    """The migration's stated design: no SQL mirror of the Python bound.

    Migration 024's header records why — a CHECK duplicating a Python rule
    drifts, and fixing only the SQL half looks correct while not being. The
    gate lives in ``brain.gaps._validate_token_columns`` and is tested there.
    A future edit that "hardens" the schema with a CHECK would create exactly
    the second, silently-diverging gate that decision rejected.
    """
    rows = test_db.execute(
        """
        SELECT pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        WHERE c.conrelid = 'search_queries'::regclass AND c.contype = 'c'
        """
    ).fetchall()
    definitions = " ".join(str(r[0]) for r in rows)

    assert "payload_tokens" not in definitions
    assert "baseline_tokens" not in definitions


@pytest.mark.fresh_schema
def test_migration_is_rerunnable(test_db: psycopg.Connection[Any]) -> None:
    """Applying 028 again is a clean no-op (``ADD COLUMN IF NOT EXISTS``)."""
    # Arrange
    sql = _migration_sql()

    # Act — the session fixture already applied it once; apply it twice more.
    test_db.execute(sql)
    test_db.execute(sql)

    # Assert — each column still present exactly once.
    for name in ("payload_tokens", "baseline_tokens"):
        assert _column_count(test_db, name) == 1, name


@pytest.mark.fresh_schema
def test_column_lookups_are_scoped_to_the_public_schema(
    test_db: psycopg.Connection[Any],
) -> None:
    """A ``search_queries`` in another schema cannot answer for ours.

    Both lookups in this file filter on ``table_schema`` as well as
    ``table_name``. Drop that filter and this test reddens twice over: the
    re-runnability count sees two ``payload_tokens`` columns where the
    migration added one, and ``_column`` reports whichever row Postgres
    happened to yield first. The decoy below therefore disagrees on every axis
    the other tests assert on — wrong type, wrong nullability — so a lookup
    that reaches it cannot accidentally still pass.
    """
    # Arrange — a second schema holding a same-named, differently-shaped table.
    test_db.execute("CREATE SCHEMA decoy_schema")
    test_db.execute(
        "CREATE TABLE decoy_schema.search_queries ("
        "  payload_tokens  BOOLEAN NOT NULL,"
        "  baseline_tokens BOOLEAN NOT NULL"
        ")"
    )
    try:
        # Act / Assert — the shape reported is still the migrated one...
        assert _column(test_db, "payload_tokens") == ("integer", "YES")
        assert _column(test_db, "baseline_tokens") == ("integer", "YES")

        # ...and the count the re-runnability test asserts on stays 1, not 2.
        for name in ("payload_tokens", "baseline_tokens"):
            assert _column_count(test_db, name) == 1, name
    finally:
        # Synthetic and local to this test; the ``fresh_schema`` marker
        # re-migrates afterwards regardless, but leaving a decoy behind for
        # even one test would be a mystery guest for the next one.
        test_db.execute("DROP SCHEMA decoy_schema CASCADE")


def test_the_migration_fails_fast_under_a_conflicting_lock(
    test_db: psycopg.Connection[Any],
) -> None:
    """Contended, 028 ABORTS rather than queueing every reader behind it.

    ``ADD COLUMN IF NOT EXISTS`` still takes ACCESS EXCLUSIVE before it notices
    the column already exists, so this exercises the real lock behaviour
    against the already-migrated schema and changes nothing.

    **The two error classes ARE the guard.** ``LockNotAvailable`` (SQLSTATE
    55P03) means the migration's own ``SET LOCAL lock_timeout`` fired — it gave
    up on its own terms. Remove that line and the same ALTER waits instead,
    until this test's ``statement_timeout`` kills it, which arrives as
    ``QueryCanceled`` (57014) — the "stalls until something else intervenes"
    behaviour, caught in the act. A test asserting only "it raised" would pass
    for the wrong reason; these are disjoint psycopg classes, so this one
    cannot.

    What makes the abort SAFE rather than merely fast is structural, not
    incidental: ``run_migrations`` executes the file and then writes the
    ``schema_migrations`` row in a SEPARATE statement (``db.py``, the
    ``INSERT`` directly after ``conn.execute(sql_file.read_text())``), so a
    file that aborts cannot be recorded as applied. Re-running ``brain init``
    after the blocking transaction ends applies it cleanly.
    """
    # Arrange — a reader holding ACCESS SHARE on search_queries, exactly as
    # brain-mcp or the vault watcher would mid-transaction.
    with psycopg.connect(_TEST_DATABASE_URL) as blocker:
        blocker.execute("SELECT count(*) FROM search_queries")
        # A ceiling well above the migration's own 3s, so a migration that does
        # NOT give up is killed by this instead, under a distinguishable class.
        #
        # ``SET``, deliberately NOT ``SET LOCAL``. ``test_db`` is an AUTOCOMMIT
        # connection (which is what makes this faithful — ``brain init`` sets
        # autocommit before ``run_migrations`` too), so there is no open
        # transaction for a LOCAL setting to attach to: Postgres would accept
        # it with a warning and apply nothing, and the mutant below would hang
        # forever instead of reddening. Measured, not reasoned — the first
        # draft of this test used SET LOCAL and ran past ten minutes.
        #
        # The migration's own ``SET LOCAL`` is fine for the opposite reason: it
        # sits inside the file's ``BEGIN``/``COMMIT``, which under autocommit is
        # a real transaction, and being LOCAL is what stops it leaking into 029.
        test_db.execute("SET statement_timeout = '15s'")
        try:
            # Act / Assert
            started = time.monotonic()
            with pytest.raises(psycopg.errors.LockNotAvailable):
                test_db.execute(_migration_sql())
            elapsed = time.monotonic() - started
        finally:
            # The file's own BEGIN left an aborted transaction on this
            # connection; clear it (and the session setting) before the
            # fixture hands it back.
            test_db.execute("ROLLBACK")
            test_db.execute("RESET statement_timeout")
        blocker.rollback()

    # Generous bound (the migration asks for 3s): this is here so that raising
    # the timeout to something that is no longer "fast" cannot pass silently.
    assert elapsed < 10, f"gave up, but took {elapsed:.1f}s"
