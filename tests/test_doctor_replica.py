"""``brain doctor``'s ``replica`` row (roadmap ticket RM-47).

The row reads ``$BRAIN_HOME/state/restored-from.json``, the marker a successful
``brain restore`` writes. Absent marker, absent row (AC2). All values synthetic.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from brain.backup.replica_marker import MARKER_UNREADABLE, marker_path
from brain.doctor_replica import replica_doctor_check

pytestmark = pytest.mark.nodb

ARCHIVE_NAME = "brain-backup-20261030-181500.tar.gz"
GUIDE = "docs/guides/second-machine.md"
#: rev-119 #2: prose, because a bare ``$BRAIN_HOME`` expands to nothing when it is unset.
MARKER_PROSE = "remove restored-from.json from $BRAIN_HOME/state (~/.brain/state by default)"


def _put(home: Path, raw: str) -> None:
    path = marker_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")


def _valid(**overrides: object) -> str:
    payload: dict[str, object] = {
        "schema": 1,
        "created_at": "2026-10-30T18:15:00+00:00",
        "archive": ARCHIVE_NAME,
        "restored_at": "2026-10-31T09:00:00+00:00",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _all_text(check: object) -> str:
    lines = getattr(check, "lines", ())
    return "\n".join(
        [str(getattr(check, "detail", "")), str(getattr(check, "remedy", ""))]
        + [line.text for line in lines]
    )


def test_no_marker_means_no_row(tmp_path: Path) -> None:
    assert replica_doctor_check(brain_home=tmp_path) == []


def test_a_marker_names_the_backup_time_and_archive(tmp_path: Path) -> None:
    _put(tmp_path, _valid())

    [check] = replica_doctor_check(brain_home=tmp_path)

    assert check.check == "replica"
    assert check.status == "ok"
    assert check.detail.startswith(
        "read replica, restored from a backup taken at 2026-10-30 18:15 UTC"
    )
    assert ARCHIVE_NAME in check.detail
    assert "writes here are lost at the next restore" in check.detail


def test_the_ok_row_points_a_recovered_primary_at_the_guide(tmp_path: Path) -> None:
    """RM-47.D1: a primary restored for disaster recovery is not a replica."""
    _put(tmp_path, _valid())

    [check] = replica_doctor_check(brain_home=tmp_path)

    text = _all_text(check)
    assert MARKER_PROSE in text
    assert GUIDE in text
    assert not any(line.err for line in check.lines)


def test_a_non_utc_backup_time_is_shown_in_utc(tmp_path: Path) -> None:
    _put(tmp_path, _valid(created_at="2026-10-30T11:15:00-07:00"))

    [check] = replica_doctor_check(brain_home=tmp_path)

    assert "taken at 2026-10-30 18:15 UTC" in check.detail


def test_an_unreadable_marker_warns_without_echoing_it(tmp_path: Path) -> None:
    _put(tmp_path, '{"archive": "SYNTHETIC-SECRET-BYTES", "created_at": "garbage"}')

    [check] = replica_doctor_check(brain_home=tmp_path)

    assert check.status == "warn"
    assert check.detail == MARKER_UNREADABLE
    text = _all_text(check)
    assert "SYNTHETIC-SECRET-BYTES" not in text
    assert str(tmp_path) not in text
    assert MARKER_PROSE in text
    assert GUIDE in text
    assert not check.lines[0].err  # a WARN, never a FAIL


def test_an_escape_in_the_archive_name_never_reaches_the_terminal(tmp_path: Path) -> None:
    _put(tmp_path, _valid(archive="evil\x1b[2Jname.tar.gz"))

    [check] = replica_doctor_check(brain_home=tmp_path)

    assert "\x1b" not in _all_text(check)
    assert "evil[2Jname.tar.gz" in check.detail


def test_the_default_home_is_brain_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    _put(tmp_path, _valid())

    [check] = replica_doctor_check()

    assert check.status == "ok"


def test_the_module_imports_cold_without_the_backup_package() -> None:
    """The backup package (psycopg, restore) is imported inside the probe only."""
    code = (
        "import sys, brain.doctor_replica; "
        "sys.exit(1 if 'brain.backup' in sys.modules else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr.decode()


def test_an_out_of_range_backup_time_warns_instead_of_crashing(tmp_path: Path) -> None:
    """sec-119 L1: ``astimezone(UTC)`` on this used to raise OverflowError in doctor."""
    _put(tmp_path, _valid(created_at="0001-01-01T00:00:00+14:00"))

    [check] = replica_doctor_check(brain_home=tmp_path)

    assert check.status == "warn"
    assert check.detail == MARKER_UNREADABLE


def test_deeply_nested_json_warns_instead_of_crashing(tmp_path: Path) -> None:
    """sec-119 L2: RecursionError on 3.11 escaped the read."""
    _put(tmp_path, "[" * 4000)

    [check] = replica_doctor_check(brain_home=tmp_path)

    assert check.status == "warn"


def test_any_unexpected_error_in_the_read_becomes_the_fixed_warning(tmp_path: Path) -> None:
    """Defence in depth: the replica row never takes ``brain doctor`` down."""
    _put(tmp_path, _valid())
    with patch(
        "brain.backup.replica_marker.read_marker",
        side_effect=RuntimeError("SYNTHETIC-UNEXPECTED"),
    ):
        [check] = replica_doctor_check(brain_home=tmp_path)

    assert check.status == "warn"
    assert check.detail == MARKER_UNREADABLE
    assert "SYNTHETIC-UNEXPECTED" not in _all_text(check)


def test_a_valid_marker_is_still_ok_after_the_hardening(tmp_path: Path) -> None:
    _put(tmp_path, _valid(created_at="2026-10-30T11:15:00-07:00"))

    [check] = replica_doctor_check(brain_home=tmp_path)

    assert check.status == "ok"
