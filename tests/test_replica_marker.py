"""The ``restored-from.json`` marker a restore leaves behind (roadmap ticket RM-47).

Every guard is paired: a "refuses" case and a "keeps ordinary behaviour" case,
because an over-tight fix that rejects ordinary data is the regression earlier
tickets kept shipping. All values are synthetic.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from brain.backup.replica_marker import (
    ARCHIVE_NAME_MAX,
    MARKER_MAX_BYTES,
    MARKER_NAME,
    ReplicaMarkerError,
    RestoredFrom,
    marker_path,
    read_marker,
    write_marker,
)
from brain.errors import BackupError
from tests.test_backup_manifest import _manifest

pytestmark = pytest.mark.nodb

TAKEN = datetime(2026, 10, 30, 18, 15, 0, tzinfo=UTC)
RESTORED = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
ARCHIVE_NAME = "brain-backup-20261030-181500.tar.gz"


def _write(home: Path, archive: Path | None = None) -> Path:
    archive = archive if archive is not None else home.parent / "copies" / ARCHIVE_NAME
    return write_marker(home, _manifest(created_at=TAKEN), archive, now=RESTORED)


def _payload(home: Path) -> dict[str, object]:
    return json.loads(marker_path(home).read_text(encoding="utf-8"))


def _put(home: Path, payload: object) -> Path:
    path = marker_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _valid_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema": 1,
        "created_at": TAKEN.isoformat(),
        "archive": ARCHIVE_NAME,
        "restored_at": RESTORED.isoformat(),
    }
    base.update(overrides)
    return base


# --- write ---------------------------------------------------------------------------


def test_marker_path_is_under_the_state_directory(tmp_path: Path) -> None:
    assert marker_path(tmp_path) == tmp_path / "state" / MARKER_NAME
    assert MARKER_NAME == "restored-from.json"


def test_write_records_the_manifest_created_at_and_the_archive_name(tmp_path: Path) -> None:
    home = tmp_path / "home"

    written = _write(home)

    assert written == marker_path(home)
    payload = _payload(home)
    assert datetime.fromisoformat(str(payload["created_at"])) == TAKEN
    assert datetime.fromisoformat(str(payload["restored_at"])) == RESTORED
    assert payload["archive"] == ARCHIVE_NAME
    assert payload["schema"] == 1


def test_write_stores_the_basename_never_the_full_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    nested = tmp_path / "synthetic-user" / "Downloads" / "copies" / ARCHIVE_NAME

    _write(home, nested)

    raw = marker_path(home).read_text(encoding="utf-8")
    assert "synthetic-user" not in raw
    assert "Downloads" not in raw
    assert _payload(home)["archive"] == ARCHIVE_NAME


def test_write_strips_control_escape_and_bidi_characters(tmp_path: Path) -> None:
    home = tmp_path / "home"
    hostile = tmp_path / "brain-\x1b[31mbackup‮-\x07x.tar.gz"

    _write(home, hostile)

    assert _payload(home)["archive"] == "brain-[31mbackup-x.tar.gz"


def test_write_keeps_spaces_and_non_ascii_letters(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ordinary = tmp_path / "brain backup café 2026.tar.gz"

    _write(home, ordinary)

    assert _payload(home)["archive"] == "brain backup café 2026.tar.gz"


def test_write_caps_an_overlong_name(tmp_path: Path) -> None:
    home = tmp_path / "home"
    # A real filesystem caps a name at 255 bytes, so build the path object only.
    long_name = Path("b" * (ARCHIVE_NAME_MAX + 40) + ".tar.gz")

    write_marker(home, _manifest(created_at=TAKEN), long_name, now=RESTORED)

    assert len(str(_payload(home)["archive"])) == ARCHIVE_NAME_MAX


def test_write_keeps_a_name_of_exactly_the_cap(tmp_path: Path) -> None:
    home = tmp_path / "home"
    exact = Path("c" * ARCHIVE_NAME_MAX)

    write_marker(home, _manifest(created_at=TAKEN), exact, now=RESTORED)

    assert _payload(home)["archive"] == "c" * ARCHIVE_NAME_MAX


def test_write_creates_a_private_state_dir_and_marker(tmp_path: Path) -> None:
    home = tmp_path / "home"

    _write(home)

    assert stat.S_IMODE((home / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE(marker_path(home).stat().st_mode) == 0o600


def test_write_refuses_a_symlinked_state_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / "state").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(ReplicaMarkerError) as excinfo:
        _write(home)

    assert list(elsewhere.iterdir()) == []
    assert str(tmp_path) not in str(excinfo.value)


def test_write_reuses_an_existing_state_dir_and_leaves_its_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    neighbour = state / "voice-failures.json"
    neighbour.write_text("{}", encoding="utf-8")

    _write(home)

    assert neighbour.read_text(encoding="utf-8") == "{}"
    assert marker_path(home).is_file()


def test_write_replaces_a_symlinked_marker_without_following_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    target = tmp_path / "victim.txt"
    target.write_text("untouched", encoding="utf-8")
    marker_path(home).symlink_to(target)

    _write(home)

    assert target.read_text(encoding="utf-8") == "untouched"
    assert not marker_path(home).is_symlink()
    assert _payload(home)["archive"] == ARCHIVE_NAME


def test_write_overwrites_an_earlier_marker(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _put(home, _valid_payload(archive="older.tar.gz"))

    _write(home)

    assert _payload(home)["archive"] == ARCHIVE_NAME


def test_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    home = tmp_path / "home"

    _write(home)

    assert [p.name for p in (home / "state").iterdir()] == [MARKER_NAME]


def test_write_into_a_read_only_state_dir_fails_and_leaves_the_old_marker(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    old = _put(home, _valid_payload(archive="older.tar.gz"))
    before = old.read_bytes()
    (home / "state").chmod(0o500)
    try:
        with pytest.raises(ReplicaMarkerError):
            _write(home)
    finally:
        (home / "state").chmod(0o700)

    assert old.read_bytes() == before
    assert [p.name for p in (home / "state").iterdir()] == [MARKER_NAME]


def test_write_failure_is_a_fixed_message_without_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "state").write_text("a file, not a directory", encoding="utf-8")

    with pytest.raises(ReplicaMarkerError) as excinfo:
        _write(home)

    assert isinstance(excinfo.value, BackupError)
    assert str(excinfo.value) == "could not write the restored-from marker"


# --- read ----------------------------------------------------------------------------


def test_read_round_trips_a_written_marker(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(home)

    assert read_marker(home) == RestoredFrom(
        created_at=TAKEN, archive_name=ARCHIVE_NAME, restored_at=RESTORED
    )


def test_read_without_a_marker_is_none(tmp_path: Path) -> None:
    assert read_marker(tmp_path / "never-restored") is None
    (tmp_path / "state").mkdir()
    assert read_marker(tmp_path) is None


def test_read_keeps_a_non_utc_offset(tmp_path: Path) -> None:
    offset = TAKEN.astimezone(timezone(timedelta(hours=-7)))
    _put(tmp_path, _valid_payload(created_at=offset.isoformat()))

    marker = read_marker(tmp_path)

    assert marker is not None
    assert marker.created_at == TAKEN


def test_read_refuses_a_symlinked_marker(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text(json.dumps(_valid_payload()), encoding="utf-8")
    (tmp_path / "state").mkdir()
    marker_path(tmp_path).symlink_to(real)

    with pytest.raises(ReplicaMarkerError):
        read_marker(tmp_path)


def test_read_refuses_a_symlinked_state_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    other_home = tmp_path / "other-home"
    _put(other_home, _valid_payload())
    (home / "state").symlink_to(other_home / "state", target_is_directory=True)

    with pytest.raises(ReplicaMarkerError):
        read_marker(home)


def test_read_refuses_an_oversized_marker(tmp_path: Path) -> None:
    _put(tmp_path, _valid_payload(padding="x" * MARKER_MAX_BYTES))

    with pytest.raises(ReplicaMarkerError):
        read_marker(tmp_path)


def test_read_refuses_an_oversized_marker_even_when_its_head_parses(tmp_path: Path) -> None:
    """Trailing whitespace keeps a truncated read valid JSON, so only the cap refuses it."""
    path = marker_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_valid_payload()) + " " * MARKER_MAX_BYTES, encoding="utf-8")

    with pytest.raises(ReplicaMarkerError):
        read_marker(tmp_path)


def test_read_accepts_a_marker_at_exactly_the_cap(tmp_path: Path) -> None:
    body = json.dumps(_valid_payload())
    path = marker_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(body + " " * (MARKER_MAX_BYTES - len(body)), encoding="utf-8")

    assert read_marker(tmp_path) is not None


def test_read_ignores_unknown_extra_keys(tmp_path: Path) -> None:
    _put(tmp_path, _valid_payload(future_field="kept for later"))

    marker = read_marker(tmp_path)

    assert marker is not None
    assert marker.archive_name == ARCHIVE_NAME


def test_read_refuses_a_directory_in_place_of_the_marker(tmp_path: Path) -> None:
    marker_path(tmp_path).mkdir(parents=True)

    with pytest.raises(ReplicaMarkerError):
        read_marker(tmp_path)


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all {",
        "[1, 2, 3]",
        json.dumps({k: v for k, v in _valid_payload().items() if k != "created_at"}),
        json.dumps({k: v for k, v in _valid_payload().items() if k != "archive"}),
        json.dumps(_valid_payload(created_at=12345)),
        json.dumps(_valid_payload(archive=["a"])),
        json.dumps(_valid_payload(created_at="yesterday-ish")),
        json.dumps(_valid_payload(created_at="2026-10-30T18:15:00")),  # naive
        json.dumps(_valid_payload(restored_at="2026-10-31T09:00:00")),  # naive
    ],
)
def test_read_refuses_malformed_content_with_a_fixed_message(
    tmp_path: Path, raw: str
) -> None:
    path = marker_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(ReplicaMarkerError) as excinfo:
        read_marker(tmp_path)

    message = str(excinfo.value)
    assert message == "restored-from marker is unreadable"
    assert str(tmp_path) not in message


def test_read_refuses_bytes_that_are_not_utf8(tmp_path: Path) -> None:
    path = marker_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe{}")

    with pytest.raises(ReplicaMarkerError):
        read_marker(tmp_path)


def test_read_resanitises_a_hand_edited_name(tmp_path: Path) -> None:
    _put(tmp_path, _valid_payload(archive="evil\x1b]0;title\x07.tar.gz"))

    marker = read_marker(tmp_path)

    assert marker is not None
    assert marker.archive_name == "evil]0;title.tar.gz"


def test_read_does_not_hang_on_a_fifo(tmp_path: Path) -> None:
    path = marker_path(tmp_path)
    path.parent.mkdir(parents=True)
    os.mkfifo(path)

    with pytest.raises(ReplicaMarkerError):
        read_marker(tmp_path)
