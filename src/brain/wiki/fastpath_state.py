"""Watcher-owned advisory state persistence for the fast-path build system.

Stores the watcher PID, build counters, and last build timestamps in
``<fastpath_dir>/state.json``. Written atomically (tmp + rename) after every
successful build. Read on watcher startup for telemetry only — never used as
authoritative routing data (manifest + contentmap own that).

Design constraints:
- Single writer: the Python watcher process. Node processes never touch state.json.
- Atomic write: same tmp-uuid-rename pattern as fastpath_manifest._atomic_write_json.
- Stale detection: a ``watcher_pid`` that differs from ``os.getpid()`` means a
  previous watcher left the file — caller treats this as stale (returns None).
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brain.wiki.errors import BrainWikiError

_FASTPATH_STATE_VERSION: int = 1


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class FastpathStateError(BrainWikiError):
    """Raised on state.json corruption, version mismatch, or IO error."""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FastpathState:
    """Watcher-owned advisory state. Versioned. Atomic JSON.

    All timestamps are epoch-milliseconds (int). ``last_partial_slug`` is the
    slug of the last successfully partially-built file; used for telemetry only.
    """

    version: int
    """Must equal :data:`_FASTPATH_STATE_VERSION`."""

    watcher_pid: int
    """Process ID of the watcher that wrote this file."""

    last_partial_at_ms: int
    """Epoch ms of the last successful partial build (0 if none yet)."""

    last_full_at_ms: int
    """Epoch ms of the last successful full build (0 if none yet)."""

    last_partial_slug: str | None
    """Slug of the last successfully partially-built file (telemetry only)."""

    consecutive_partial_failures: int
    """Count of consecutive partial-build failures since the last success."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_state(fastpath_dir: Path) -> FastpathState | None:
    """Read ``state.json`` from ``fastpath_dir``.

    Returns:
        :class:`FastpathState` if the file exists, parses cleanly, version
        matches, **and** ``watcher_pid`` matches ``os.getpid()``.
        ``None`` if the file is missing or ``watcher_pid`` is stale (different
        from current PID).

    Raises:
        :class:`FastpathStateError`: On JSON parse error or version mismatch.
    """
    path = fastpath_dir / "state.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FastpathStateError(f"cannot read state.json at {path}: {exc}") from exc

    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FastpathStateError("malformed state.json") from exc

    if not isinstance(data, dict):
        raise FastpathStateError("malformed state.json")

    version = data.get("version")
    if version != _FASTPATH_STATE_VERSION:
        raise FastpathStateError(
            f"state.json version unsupported: got {version!r}, "
            f"expected {_FASTPATH_STATE_VERSION}"
        )

    try:
        state = FastpathState(
            version=int(data["version"]),
            watcher_pid=int(data["watcher_pid"]),
            last_partial_at_ms=int(data.get("last_partial_at_ms", 0)),
            last_full_at_ms=int(data.get("last_full_at_ms", 0)),
            last_partial_slug=data.get("last_partial_slug"),
            consecutive_partial_failures=int(
                data.get("consecutive_partial_failures", 0)
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FastpathStateError(f"malformed state.json: {exc}") from exc

    # Stale PID check: a different watcher wrote this file.
    if state.watcher_pid != os.getpid():
        return None

    return state


def write_state(fastpath_dir: Path, state: FastpathState) -> None:
    """Atomically write ``state.json`` to ``fastpath_dir``.

    Uses a UUID-suffixed temporary file + ``os.replace`` (POSIX rename) for
    atomicity — the same pattern as ``fastpath_manifest._atomic_write_json``.

    The ``fastpath_dir`` must already exist; this function does NOT create it.

    Raises:
        :class:`FastpathStateError`: On any IO failure.
    """
    payload: dict[str, Any] = {
        "version": state.version,
        "watcher_pid": state.watcher_pid,
        "last_partial_at_ms": state.last_partial_at_ms,
        "last_full_at_ms": state.last_full_at_ms,
        "last_partial_slug": state.last_partial_slug,
        "consecutive_partial_failures": state.consecutive_partial_failures,
    }
    content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    tmp_path = fastpath_dir / f"state.{uuid.uuid4().hex}.tmp"
    target = fastpath_dir / "state.json"
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, target)
    except OSError as exc:
        # Best-effort cleanup of the temp file on failure.
        with _suppress_os_error():
            tmp_path.unlink()
        raise FastpathStateError(f"cannot write state.json to {target}: {exc}") from exc


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


class _suppress_os_error:
    """Context manager that silently swallows OSError (used for tmp cleanup)."""

    def __enter__(self) -> _suppress_os_error:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        return isinstance(exc_val, OSError)
