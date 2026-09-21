"""The restore sandbox's name must fit Postgres' identifier limit — and fail loudly.

Two independent regressions are covered here, because the bug that prompted
them had two halves and either half alone still breaks a worktree:

1. **The budget was too tight.** The sandbox suffix was ``_restore_sandbox``
   (16 bytes), which left suite database names 22. This repo's convention is
   ``second_brain_test_<worktree-slug>`` and the fixed prefix alone is 18, so
   any slug past 4 characters overflowed — ``second_brain_test_wikiui`` (24)
   did, and four sibling databases on the shared test server were over the line
   with it.
2. **The failure took down collection.** Both restore modules resolve their DSN
   at module scope, so the raise landed during COLLECTION: pytest reported
   ``Interrupted: 2 errors during collection`` and exited 2 for the whole repo.
   8,445 tests, ``-m browser`` among them, were unreachable because deselection
   never got to run.

No database is created or dropped here. The tests are pure arithmetic, one
read-only ``SHOW max_identifier_length``, and a subprocess whose database name
is deliberately over budget — which raises before any DDL, by construction (see
:func:`tests.backup_fakes.restore_sandbox_dsn`).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from brain.backup.create import TIMESTAMP_FORMAT
from tests.backup_fakes import (
    MAX_SUITE_DB_NAME_BYTES,
    PG_IDENTIFIER_MAX_BYTES,
    RESTORE_DERIVED_SUFFIX_BYTES,
    SANDBOX_SUFFIX,
    SuiteDatabaseNameTooLong,
    sandbox_database_name,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The name that actually broke this worktree. 24 bytes.
WORKTREE_DB = "second_brain_test_wikiui"


def test_the_worktree_suffixed_name_that_broke_collection_now_fits() -> None:
    """``second_brain_test_wikiui`` (24) must derive a sandbox, not an error."""
    assert len(WORKTREE_DB) == 24
    assert sandbox_database_name(WORKTREE_DB) == f"{WORKTREE_DB}{SANDBOX_SUFFIX}"


def test_the_derived_parked_name_fits_postgres_for_the_longest_legal_suite_db() -> None:
    """The budget is the real thing: sandbox + what a restore appends, in bytes.

    Asserts the END of the chain rather than the constant, so an arithmetic
    slip in :data:`MAX_SUITE_DB_NAME_BYTES` cannot pass by agreeing with itself.
    """
    longest = "s" * MAX_SUITE_DB_NAME_BYTES
    parked = f"{sandbox_database_name(longest)}_replaced_20260725_181500"
    assert len(parked.encode("utf-8")) == PG_IDENTIFIER_MAX_BYTES


def test_the_identifier_limit_is_pinned_to_postgres_reality() -> None:
    """``PG_IDENTIFIER_MAX_BYTES`` must be 63, and not merely self-consistent.

    Every other assertion in this file derives its expected value FROM this
    constant, so all of them stay green if it is edited — verified by mutating
    63 to 64, which left the whole file passing. That makes them a consistency
    check on the arithmetic, not a check that the arithmetic is calibrated to
    Postgres. This is the one place the number is nailed down.

    Two independent pins, because each has a hole the other covers: the literal
    catches an edit even with no server around, and the server probe catches
    the literal being confidently wrong.
    """
    import psycopg

    from tests.conftest import TEST_DATABASE_URL

    # NAMEDATALEN (64) - 1. A literal on purpose: comparing the constant to
    # itself is what the mutation showed to be worthless.
    assert PG_IDENTIFIER_MAX_BYTES == 63

    # Read-only. No DDL, no database created or dropped.
    with psycopg.connect(TEST_DATABASE_URL, connect_timeout=5) as conn:
        reported = conn.execute("SHOW max_identifier_length").fetchone()
    assert reported is not None
    assert int(reported[0]) == PG_IDENTIFIER_MAX_BYTES


def test_one_byte_past_the_budget_is_refused() -> None:
    """Prove the guard can fail — a budget nothing trips is not a guard."""
    too_long = "s" * (MAX_SUITE_DB_NAME_BYTES + 1)
    with pytest.raises(SuiteDatabaseNameTooLong) as excinfo:
        sandbox_database_name(too_long)
    message = str(excinfo.value)
    # Actionable at the point of use: it must name the limit and the offender,
    # and point at the slug rather than at the convention.
    assert str(MAX_SUITE_DB_NAME_BYTES) in message
    assert "slug" in message.lower()


def test_the_reserved_suffix_matches_what_production_actually_appends() -> None:
    """Ties the reservation to ``brain.backup.restore``, not to a copied 25.

    ``_restore_database`` derives ``{live}_replaced_{stamp}`` where ``stamp`` is
    ``TIMESTAMP_FORMAT`` with its dash swapped for an underscore. If that format
    ever grows, this fails here instead of surfacing as a truncated database
    name nobody can trace.
    """
    from datetime import datetime

    stamp = datetime(2026, 7, 25, 18, 15, 0).strftime(TIMESTAMP_FORMAT)
    assert len(f"_replaced_{stamp.replace('-', '_')}") == RESTORE_DERIVED_SUFFIX_BYTES


def test_the_sandbox_suffix_cannot_collide_with_the_leftovers_sweep() -> None:
    """``drop_restore_artifacts`` drops every ``{live}_restore_%``.

    The old ``_restore_sandbox`` name matched that pattern, so passing the suite
    DSN deleted the session's own sandbox. Keep the suffix out of the pattern.
    """
    assert not SANDBOX_SUFFIX.startswith("_restore")


def test_an_over_budget_name_skips_the_module_instead_of_killing_collection() -> None:
    """The half that made this repo-wide: a module-scope raise aborts collection.

    Runs a real nested pytest so the assertion is about pytest's own behaviour
    (exit code, "errors during collection") rather than about our try/except
    reading correctly. The name is deliberately over budget, which raises before
    any ``CREATE DATABASE`` — this test never reaches a server.
    """
    over_budget = "second_brain_test_" + "x" * 30
    assert len(over_budget) > MAX_SUITE_DB_NAME_BYTES

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_restore_gate.py",
            "tests/test_restore_swap.py",
            # A third, unrelated, DB-free module. It is the POINT of the test:
            # the regression was that the two above aborted collection for
            # everything else, so the assertion has to be that a bystander is
            # still collected. Selecting only the skipping pair would exit 5
            # ("no tests collected") and prove nothing about blast radius.
            "tests/test_chunker.py",
            "--collect-only",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        env={
            **__import__("os").environ,
            "TEST_DATABASE_URL": (
                f"postgresql://brain:brain@localhost:5434/{over_budget}"
            ),
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = completed.stdout + completed.stderr
    # Exit 2 is the regression: "Interrupted: N errors during collection".
    assert completed.returncode == 0, output
    assert "errors during collection" not in output, output
    # Both restore modules skipped, by name — and skipped for OUR reason, not
    # some unrelated one that would make this pass for the wrong cause.
    assert "SKIPPED [1] tests/test_restore_gate.py" in output, output
    assert "SKIPPED [1] tests/test_restore_swap.py" in output, output
    assert output.count("is too long: the restore sandbox") == 2, output
    # ...and the bystander was still collected, which is the whole claim.
    assert "tests/test_chunker.py::" in output, output
