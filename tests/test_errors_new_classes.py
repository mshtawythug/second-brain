"""Tests for the Wave-0 pre-landed exception classes (Task 0B, plan 2026-07-25).

These ten classes are scaffolding: nothing raises them yet. They are landed in
Wave 0 so that ``errors.py`` has exactly one writer for the whole release and
five parallel Wave-1/2 worktrees never collide on it. The assertions here are
the contract every later wave codes against — the class exists, it inherits
:class:`BrainError`, and (for the two payload-carrying restore errors) the
documented attribute is reachable with a keyword-only argument and an empty
default.
"""
from __future__ import annotations

import pytest

from brain.errors import (
    AgentIdInvalid,
    BackupError,
    BrainError,
    HookInstallError,
    PgToolUnavailable,
    RestoreAborted,
    RestoreIncompatible,
    SecretGuardError,
    SensitivityError,
    SettingsFormatError,
    VaultPathEscape,
)

# The ten classes pre-landed by Task 0B, in plan order.
_PRE_LANDED: tuple[type[BrainError], ...] = (
    HookInstallError,
    SettingsFormatError,
    AgentIdInvalid,
    BackupError,
    PgToolUnavailable,
    RestoreIncompatible,
    RestoreAborted,
    SecretGuardError,
    SensitivityError,
    VaultPathEscape,
)


@pytest.mark.parametrize("cls", _PRE_LANDED, ids=lambda c: c.__name__)
def test_pre_landed_exception_inherits_brain_error(cls: type[BrainError]) -> None:
    assert issubclass(cls, BrainError)


@pytest.mark.parametrize("cls", _PRE_LANDED, ids=lambda c: c.__name__)
def test_pre_landed_exception_has_docstring(cls: type[BrainError]) -> None:
    # Repo rule: every exception carries a docstring explaining when it fires.
    assert cls.__doc__ is not None and cls.__doc__.strip()


@pytest.mark.parametrize(
    "cls",
    (PgToolUnavailable, RestoreIncompatible, RestoreAborted),
    ids=lambda c: c.__name__,
)
def test_backup_family_inherits_backup_error(cls: type[BrainError]) -> None:
    assert issubclass(cls, BackupError)


def test_restore_incompatible_issues_defaults_to_empty_tuple() -> None:
    # Setup / exercise.
    error = RestoreIncompatible("archive was written by a different embedder")

    # Verify: the payload is reachable and empty until Task 1A populates it.
    assert error.issues == ()
    assert str(error) == "archive was written by a different embedder"


def test_restore_incompatible_carries_issues_as_keyword_only_tuple() -> None:
    # Setup: Wave 1 passes brain.backup.PreflightIssue instances; the container
    # is typed ``tuple[object, ...]`` here so errors.py never imports that
    # not-yet-existing module. Any sequence is accepted and frozen to a tuple.
    issues = ["embedder mismatch", "dim mismatch"]

    # Exercise.
    error = RestoreIncompatible("preflight failed", issues=issues)

    # Verify.
    assert error.issues == ("embedder mismatch", "dim mismatch")
    assert isinstance(error.issues, tuple)


def test_restore_incompatible_issues_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        RestoreIncompatible("preflight failed", ["positional"])  # type: ignore[misc]


def test_restore_aborted_recovery_sql_defaults_to_empty_string() -> None:
    error = RestoreAborted("restore aborted before the swap")

    assert error.recovery_sql == ""
    assert str(error) == "restore aborted before the swap"


def test_restore_aborted_carries_recovery_sql_as_keyword_only() -> None:
    sql = "ALTER DATABASE brain_qa_replaced_20260726 RENAME TO brain_qa;"

    error = RestoreAborted("restore aborted mid-swap", recovery_sql=sql)

    assert error.recovery_sql == sql


def test_restore_aborted_recovery_sql_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        RestoreAborted("restore aborted", "SELECT 1;")  # type: ignore[misc]


def test_backup_family_is_catchable_as_brain_error() -> None:
    # The CLI / MCP layers catch BrainError once; every pre-landed class must
    # flow through that single handler.
    with pytest.raises(BrainError):
        raise PgToolUnavailable("pg_dump 14.23 is older than server 16.14")
