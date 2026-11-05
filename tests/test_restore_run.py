"""``restore_into``'s host leg: TOC first, then the unchanged ``pg_restore`` (RM-47).

No database and no real tool: a recording runner answers both calls. The
container leg is exercised end to end by ``test_restore_swap.py`` and, with the
real tools, by ``test_restore_real_pg_tools.py``.
"""
from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from brain.backup.pgtool import RESTORE_TIMEOUT_S, PgToolPlan
from brain.backup.restore_run import (
    TOC_MAX_CHARS,
    TOC_TIMEOUT_S,
    TOC_UNREADABLE,
    restore_into,
)
from brain.errors import BackupError
from tests.backup_fakes import RecordingRunner
from tests.test_restore_toc import PLAIN_TOC

pytestmark = pytest.mark.nodb

HOST_PLAN = PgToolPlan(
    tool="pg_restore",
    source="host",
    version="16.14",
    major=16,
    container=None,
    argv_prefix=("/opt/synthetic/bin/pg_restore",),
)
CONTAINER_PLAN = PgToolPlan(
    tool="pg_restore",
    source="container",
    version="16.14",
    major=16,
    container="synthetic-test-container",
    argv_prefix=("docker", "exec", "synthetic-test-container", "pg_restore"),
)
PARTS = {
    "host": "localhost",
    "port": "5434",
    "user": "brain",
    "password": "synthetic-password",
    "dbname": "synthetic_restore_20261101_000000",
}


def _dump(tmp_path: Path) -> Path:
    dump = tmp_path / "second_brain.dump"
    dump.write_bytes(b"PGDMP-synthetic")
    return dump


class _TimeoutRecorder(RecordingRunner):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.timeouts: list[float] = []

    def run(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.timeouts.append(timeout)
        return super().run(argv, timeout=timeout, env=env)


def test_the_listing_has_its_own_short_timeout_and_the_restore_keeps_the_long_one(
    tmp_path: Path,
) -> None:
    """sec-119b N2: a wedged listing must not hold a restore for two hours."""
    runner = _TimeoutRecorder(responses={"pg_restore -l": PLAIN_TOC})

    restore_into(HOST_PLAN, dict(PARTS), _dump(tmp_path), PARTS["dbname"], runner)

    assert TOC_TIMEOUT_S == 120
    assert runner.timeouts == [TOC_TIMEOUT_S, RESTORE_TIMEOUT_S]


def test_a_listing_that_times_out_is_a_backup_error_and_runs_no_restore(tmp_path: Path) -> None:
    runner = RecordingRunner(
        raises={"pg_restore -l": subprocess.TimeoutExpired(["pg_restore"], TOC_TIMEOUT_S)}
    )

    with pytest.raises(BackupError, match="timed out"):
        restore_into(HOST_PLAN, dict(PARTS), _dump(tmp_path), PARTS["dbname"], runner)

    assert len(runner.calls) == 1


def test_a_listing_over_the_cap_is_a_fixed_error_and_runs_no_restore(tmp_path: Path) -> None:
    """sec-119b N2: the error names no archive content."""
    oversized = "SYNTHETIC-TOC-LINE" + "x" * TOC_MAX_CHARS
    runner = RecordingRunner(responses={"pg_restore -l": oversized})

    with pytest.raises(BackupError) as excinfo:
        restore_into(HOST_PLAN, dict(PARTS), _dump(tmp_path), PARTS["dbname"], runner)

    assert str(excinfo.value) == TOC_UNREADABLE
    assert "SYNTHETIC-TOC-LINE" not in str(excinfo.value)
    assert len(runner.calls) == 1


def test_a_listing_exactly_at_the_cap_still_restores(tmp_path: Path) -> None:
    at_cap = PLAIN_TOC + ";" * (TOC_MAX_CHARS - len(PLAIN_TOC))
    runner = RecordingRunner(responses={"pg_restore -l": at_cap})

    restore_into(HOST_PLAN, dict(PARTS), _dump(tmp_path), PARTS["dbname"], runner)

    assert len(at_cap) == TOC_MAX_CHARS
    assert len(runner.calls) == 2


def test_an_undecodable_listing_is_the_fixed_error_not_a_traceback(tmp_path: Path) -> None:
    """sec-119b N3: ``text=True`` raises UnicodeDecodeError on a non-UTF-8 byte in a name."""
    runner = RecordingRunner(
        raises={
            "pg_restore -l": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        }
    )

    with pytest.raises(BackupError) as excinfo:
        restore_into(HOST_PLAN, dict(PARTS), _dump(tmp_path), PARTS["dbname"], runner)

    assert str(excinfo.value) == TOC_UNREADABLE
    assert len(runner.calls) == 1


def test_a_failed_listing_still_removes_the_dump_from_the_container(tmp_path: Path) -> None:
    runner = RecordingRunner(
        raises={
            "pg_restore -l": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        }
    )

    with pytest.raises(BackupError):
        restore_into(CONTAINER_PLAN, dict(PARTS), _dump(tmp_path), PARTS["dbname"], runner)

    assert any(call[-3:-1] == ["rm", "-f"] for call in runner.calls)
    assert not any(" -d " in call for call in runner.flat_calls)


def test_the_host_leg_reads_the_toc_then_restores_with_the_unchanged_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PGPASSWORD", raising=False)
    dump = tmp_path / "second_brain.dump"
    dump.write_bytes(b"PGDMP-synthetic")
    runner = RecordingRunner(responses={"pg_restore -l": PLAIN_TOC})

    restore_into(HOST_PLAN, dict(PARTS), dump, PARTS["dbname"], runner)

    listing, data = runner.calls
    assert listing == ["/opt/synthetic/bin/pg_restore", "-l", str(dump)]
    assert data[:8] == [
        "/opt/synthetic/bin/pg_restore",
        "-U",
        "brain",
        "-d",
        PARTS["dbname"],
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
    ]
    assert data[-5:] == ["-h", "localhost", "-p", "5434", str(dump)]
    # The password travels in the environment of the restore, never in argv.
    assert all("synthetic-password" not in part for call in runner.calls for part in call)
    assert runner.envs[1] is not None and runner.envs[1]["PGPASSWORD"] == "synthetic-password"
    assert runner.envs[0] is not None and "PGPASSWORD" not in runner.envs[0]


def test_the_listing_never_receives_the_dsn_password_whatever_the_ambient_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rev-119: the invariant is the DSN's password, not the variable's absence."""
    monkeypatch.setenv("PGPASSWORD", "synthetic-ambient")
    runner = RecordingRunner(responses={"pg_restore -l": PLAIN_TOC})

    restore_into(HOST_PLAN, dict(PARTS), _dump(tmp_path), PARTS["dbname"], runner)

    assert runner.envs[0] is not None
    assert PARTS["password"] not in runner.envs[0].values()
    assert runner.envs[1] is not None and runner.envs[1]["PGPASSWORD"] == PARTS["password"]
