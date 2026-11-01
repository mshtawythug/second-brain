"""``restore_into``'s host leg: TOC first, then the unchanged ``pg_restore`` (RM-47).

No database and no real tool: a recording runner answers both calls. The
container leg is exercised end to end by ``test_restore_swap.py`` and, with the
real tools, by ``test_restore_real_pg_tools.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.backup.pgtool import PgToolPlan
from brain.backup.restore_run import restore_into
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
PARTS = {
    "host": "localhost",
    "port": "5434",
    "user": "brain",
    "password": "synthetic-password",
    "dbname": "synthetic_restore_20261101_000000",
}


def test_the_host_leg_reads_the_toc_then_restores_with_the_unchanged_flags(
    tmp_path: Path,
) -> None:
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
