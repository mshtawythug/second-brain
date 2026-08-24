"""`brain init` must explain a lock timeout, not hand back a traceback.

Migration ``028`` sets ``SET LOCAL lock_timeout = '3s'`` so a contended
``ALTER`` aborts instead of queueing every reader behind it. That trade is only
worth making if the abort is legible: the raw
``LockNotAvailable: canceling statement due to lock timeout`` names neither the
holder, nor the fact that nothing was applied, nor the remedy — so it swaps a
mysterious stall for a mysterious crash.

This is the UNIT half of that guard: it proves the mapping exists. The
integration half — a real contended ``brain init`` against a held
``AccessShareLock`` — lives with the migration's own lock test.
"""
import os

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def test_init_maps_lock_timeout_to_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three facts an operator needs, and exit 1 — not a traceback.

    MUTATION TEST: delete the ``except psycopg.errors.LockNotAvailable`` block
    in ``brain.cli.init`` and this reddens at the ``result.exception``
    assertion. Verified, and worth stating precisely because the obvious guess
    is wrong: the **exit code stays 1** under the mutation, since ``CliRunner``
    reports 1 for an unhandled exception too. So the exit-code assertion alone
    would pass while the handler was gone — it is the exception-type check that
    distinguishes "mapped cleanly" from "escaped as a traceback". It is the
    only test covering that handler.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    def _raise_lock_timeout(_conn: object) -> list[str]:
        raise psycopg.errors.LockNotAvailable(
            "canceling statement due to lock timeout"
        )

    monkeypatch.setattr("brain.cli.run_migrations", _raise_lock_timeout)

    result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        "the handler must convert the psycopg error into a clean exit, "
        f"not re-raise it: {result.exception!r}"
    )

    out = result.output
    # Fact 1 — what is holding the lock. Naming the usual suspects is the
    # difference between "something" and a thing the operator can go stop.
    assert "brain-mcp" in out and "vault sync --watch" in out, out
    # Fact 2 — the FAILING migration was not applied and not recorded. Scoped
    # to the failing one, not "nothing": earlier pending migrations commit and
    # record individually, so a run that got partway through keeps that
    # progress. Without this fact the operator cannot know whether re-running
    # is safe.
    assert "failing migration was not applied and not recorded" in out, out
    assert "safe to " in out, out
    # Fact 3 — the remedy.
    assert "brain-down" in out, out
