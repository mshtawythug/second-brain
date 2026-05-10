"""Unit tests for brain.wiki.fastpath_state — state.json round-trip, error cases,
atomic-write contract.

Mocking strategy (per CLAUDE.md item 13):
    ``unittest.mock.patch`` / ``mocker.patch`` for process-level isolation.
    NO direct attribute assignment on imported modules (no monkey-patching).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from brain.wiki.fastpath_state import (
    _FASTPATH_STATE_VERSION,
    FastpathState,
    FastpathStateError,
    _suppress_os_error,
    read_state,
    write_state,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    *,
    pid: int | None = None,
    last_partial_at_ms: int = 0,
    last_full_at_ms: int = 0,
    last_partial_slug: str | None = None,
    consecutive_partial_failures: int = 0,
) -> FastpathState:
    """Build a :class:`FastpathState` using the current PID by default."""
    return FastpathState(
        version=_FASTPATH_STATE_VERSION,
        watcher_pid=pid if pid is not None else os.getpid(),
        last_partial_at_ms=last_partial_at_ms,
        last_full_at_ms=last_full_at_ms,
        last_partial_slug=last_partial_slug,
        consecutive_partial_failures=consecutive_partial_failures,
    )


# ---------------------------------------------------------------------------
# Round-trip: write → read → equal
# ---------------------------------------------------------------------------


def test_round_trip_minimal(tmp_path: Path) -> None:
    """A minimal state (all-zero counters, no slug) round-trips cleanly."""
    state = _make_state()
    write_state(tmp_path, state)
    loaded = read_state(tmp_path)
    assert loaded == state


def test_round_trip_full_fields(tmp_path: Path) -> None:
    """All non-default fields survive a round-trip."""
    state = _make_state(
        last_partial_at_ms=1_715_260_523_001,
        last_full_at_ms=1_715_260_400_000,
        last_partial_slug="notes/2026-05-09-standup",
        consecutive_partial_failures=3,
    )
    write_state(tmp_path, state)
    loaded = read_state(tmp_path)
    assert loaded == state


def test_round_trip_null_last_partial_slug(tmp_path: Path) -> None:
    """``last_partial_slug=None`` is preserved as ``null`` in JSON and reloaded as None."""
    state = _make_state(last_partial_slug=None)
    write_state(tmp_path, state)
    loaded = read_state(tmp_path)
    assert loaded is not None
    assert loaded.last_partial_slug is None


def test_round_trip_preserves_version(tmp_path: Path) -> None:
    """The ``version`` field is preserved verbatim."""
    state = _make_state()
    write_state(tmp_path, state)
    loaded = read_state(tmp_path)
    assert loaded is not None
    assert loaded.version == _FASTPATH_STATE_VERSION


# ---------------------------------------------------------------------------
# Missing file → None
# ---------------------------------------------------------------------------


def test_read_missing_returns_none(tmp_path: Path) -> None:
    """If ``state.json`` doesn't exist, ``read_state`` returns None (not an error)."""
    result = read_state(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# Stale PID → None (not an error)
# ---------------------------------------------------------------------------


def test_read_stale_pid_returns_none(tmp_path: Path, mocker: MockerFixture) -> None:
    """A file written by a different PID returns None (stale watcher state)."""
    stale_pid = os.getpid() + 99999
    state = _make_state(pid=stale_pid)
    write_state(tmp_path, state)
    result = read_state(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# JSON parse error → FastpathStateError
# ---------------------------------------------------------------------------


def test_read_malformed_json_raises(tmp_path: Path) -> None:
    """Truncated / non-JSON content raises ``FastpathStateError('malformed state.json')``."""
    (tmp_path / "state.json").write_text("not json{{{{", encoding="utf-8")
    with pytest.raises(FastpathStateError, match="malformed state.json"):
        read_state(tmp_path)


def test_read_json_array_root_raises(tmp_path: Path) -> None:
    """A JSON array root (not an object) raises FastpathStateError."""
    (tmp_path / "state.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(FastpathStateError, match="malformed state.json"):
        read_state(tmp_path)


# ---------------------------------------------------------------------------
# Version mismatch → FastpathStateError
# ---------------------------------------------------------------------------


def test_read_wrong_version_raises(tmp_path: Path) -> None:
    """A ``version`` field that doesn't match raises ``FastpathStateError``."""
    payload = {
        "version": 999,
        "watcher_pid": os.getpid(),
        "last_partial_at_ms": 0,
        "last_full_at_ms": 0,
        "last_partial_slug": None,
        "consecutive_partial_failures": 0,
    }
    (tmp_path / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FastpathStateError, match="version unsupported"):
        read_state(tmp_path)


def test_read_missing_version_raises(tmp_path: Path) -> None:
    """A state.json with no ``version`` key raises FastpathStateError."""
    payload = {
        "watcher_pid": os.getpid(),
        "last_partial_at_ms": 0,
        "last_full_at_ms": 0,
        "last_partial_slug": None,
        "consecutive_partial_failures": 0,
    }
    (tmp_path / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FastpathStateError, match="version unsupported"):
        read_state(tmp_path)


# ---------------------------------------------------------------------------
# Atomic write — verify tmp + rename pattern
# ---------------------------------------------------------------------------


def test_write_produces_state_json(tmp_path: Path) -> None:
    """After ``write_state``, ``state.json`` exists and is parseable."""
    write_state(tmp_path, _make_state())
    assert (tmp_path / "state.json").exists()
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data["version"] == _FASTPATH_STATE_VERSION


def test_write_leaves_no_tmp_file(tmp_path: Path) -> None:
    """After a successful write, no ``.tmp`` files remain in ``fastpath_dir``."""
    write_state(tmp_path, _make_state())
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert not tmp_files, f"Unexpected .tmp files left after write: {tmp_files}"


def test_write_tmp_has_uuid_in_name(tmp_path: Path, mocker: MockerFixture) -> None:
    """The intermediate temp file uses a UUID hex suffix before the rename.

    We intercept ``os.replace`` to capture the source path before it disappears.
    """
    captured_tmp: list[Path] = []

    real_replace = os.replace

    def capturing_replace(src: str, dst: str) -> None:
        captured_tmp.append(Path(src))
        real_replace(src, dst)

    mocker.patch("brain.wiki.fastpath_state.os.replace", side_effect=capturing_replace)

    write_state(tmp_path, _make_state())

    assert len(captured_tmp) == 1
    tmp_name = captured_tmp[0].name
    # Expect pattern: "state.<32-hex-chars>.tmp"
    assert re.match(r"^state\.[0-9a-f]{32}\.tmp$", tmp_name), (
        f"Unexpected tmp filename pattern: {tmp_name!r}"
    )


def test_write_is_idempotent(tmp_path: Path) -> None:
    """Writing the same state twice produces the same result."""
    state = _make_state(last_full_at_ms=1_000_000)
    write_state(tmp_path, state)
    write_state(tmp_path, state)
    loaded = read_state(tmp_path)
    assert loaded == state


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_read_valid_state_pid_matches(tmp_path: Path) -> None:
    """A file written with the current PID is returned (positive path)."""
    state = _make_state(last_full_at_ms=999)
    write_state(tmp_path, state)
    loaded = read_state(tmp_path)
    assert loaded is not None
    assert loaded.last_full_at_ms == 999


def test_consecutive_partial_failures_survives_roundtrip(tmp_path: Path) -> None:
    """A non-zero ``consecutive_partial_failures`` is preserved exactly."""
    state = _make_state(consecutive_partial_failures=7)
    write_state(tmp_path, state)
    loaded = read_state(tmp_path)
    assert loaded is not None
    assert loaded.consecutive_partial_failures == 7


# ---------------------------------------------------------------------------
# Error paths — OSError on read, wrong field type, OSError on write,
# _suppress_os_error helper
# ---------------------------------------------------------------------------


def test_read_state_raises_on_oserror(tmp_path: Path, mocker: MockerFixture) -> None:
    """A non-FileNotFoundError OSError during read_text surfaces as FastpathStateError."""
    # Ensure state.json exists so FileNotFoundError is not the cause.
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    mocker.patch.object(Path, "read_text", side_effect=PermissionError("permission denied"))
    with pytest.raises(FastpathStateError, match="cannot read state.json"):
        read_state(tmp_path)


def test_read_state_raises_on_wrong_field_type(tmp_path: Path) -> None:
    """A non-int ``watcher_pid`` (e.g., a string) raises FastpathStateError."""
    payload = {
        "version": _FASTPATH_STATE_VERSION,
        "watcher_pid": "not-an-int",
        "last_partial_at_ms": 0,
        "last_full_at_ms": 0,
        "last_partial_slug": None,
        "consecutive_partial_failures": 0,
    }
    (tmp_path / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FastpathStateError, match="malformed state.json"):
        read_state(tmp_path)


def test_write_state_raises_on_oserror(tmp_path: Path, mocker: MockerFixture) -> None:
    """An OSError during write_text surfaces as FastpathStateError."""
    mocker.patch.object(Path, "write_text", side_effect=PermissionError("disk full"))
    with pytest.raises(FastpathStateError, match="cannot write state.json"):
        write_state(tmp_path, _make_state())


def test_suppress_os_error_swallows_oserror() -> None:
    """_suppress_os_error swallows OSError and its subclasses; other exceptions propagate."""
    # Exact OSError is swallowed.
    with _suppress_os_error():
        raise OSError("simulated error")

    # OSError subclasses (e.g., FileNotFoundError, PermissionError) are also swallowed.
    with _suppress_os_error():
        raise FileNotFoundError("no such file")

    with _suppress_os_error():
        raise PermissionError("permission denied")

    # Non-OSError exceptions propagate normally.
    with pytest.raises(ValueError), _suppress_os_error():
        raise ValueError("should not be swallowed")
