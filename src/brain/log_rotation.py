"""Size-capped copy-truncate rotation for launchd-owned daemon log files."""
import logging
import os
import shutil
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Default per-file cap. A crash-looping daemon under launchd (ThrottleInterval
# 10s) writes one ~2 KB Rich traceback per restart; left uncapped this produced
# a 496 MB com.brain.watcher.err.log over 12 days. 8 MiB still holds ~4000
# tracebacks — plenty to debug with — and bounds worst-case disk to
# 8 MiB x 2 (live + .1) x 6 daemon streams ~= 96 MiB.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

# Env override, read directly rather than via Config: rotation must work even
# when Config.load() is the thing that is failing (that is precisely the crash
# loop this module exists to bound). Consolidating this into Config would
# reintroduce the dependency we are trying to break.
MAX_BYTES_ENV = "BRAIN_LOG_MAX_BYTES"

# Suffix for the single retained previous generation.
ROTATED_SUFFIX = ".1"

# How often the in-process background rotator re-checks size.
DEFAULT_INTERVAL_SECONDS = 300.0

# Only launchd-owned daemon streams are ours to rotate. Deliberately NOT a bare
# "*.log": $BRAIN_HOME can legitimately resolve to a dev checkout, whose logs/
# dir also collects unrelated operator artefacts (concept-backfill runs, ad-hoc
# reruns). Truncating those as a side effect of starting a daemon would be a
# nasty surprise, so the glob is pinned to the six com.brain.* streams the
# plists actually declare.
DAEMON_LOG_GLOB = "com.brain.*.log"


def resolve_max_bytes() -> int:
    """Return the configured per-file cap in bytes.

    Reads ``BRAIN_LOG_MAX_BYTES``; falls back to :data:`DEFAULT_MAX_BYTES` when
    unset, non-numeric, or negative. A value of ``0`` explicitly disables
    rotation (documented escape hatch for users who pipe logs elsewhere).
    """
    raw = os.environ.get(MAX_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer — falling back to %d bytes",
            MAX_BYTES_ENV,
            raw,
            DEFAULT_MAX_BYTES,
        )
        return DEFAULT_MAX_BYTES
    if value < 0:
        logger.warning(
            "%s=%d is negative — falling back to %d bytes",
            MAX_BYTES_ENV,
            value,
            DEFAULT_MAX_BYTES,
        )
        return DEFAULT_MAX_BYTES
    return value


def default_log_dir() -> Path:
    """Return ``$BRAIN_HOME/logs`` — the directory launchd writes daemon logs to.

    ``_brain_home_root`` is imported lazily so that importing this module can
    never be blocked by a problem in the config layer; rotation has to keep
    working precisely when config loading is what is broken.
    """
    from .config import _brain_home_root

    return _brain_home_root() / "logs"


def rotate_if_oversized(path: Path, *, max_bytes: int) -> bool:
    """Copy-truncate *path* if it exceeds *max_bytes*. Returns True if rotated.

    **Copy-truncate, never rename.** launchd opens ``StandardErrorPath`` /
    ``StandardOutPath`` once at spawn time and hands the daemon the resulting
    descriptor as fd 1/2. That descriptor tracks the *inode*, not the path — so
    a rename/unlink style rotation leaves the daemon happily writing into the
    rotated-away file while the new log stays empty forever. Truncating in place
    keeps the inode (and therefore launchd's fd) valid; because launchd opens
    these with ``O_APPEND``, the next write lands at offset 0 rather than
    recreating a sparse 496 MB hole.

    This is also why rotation must live at the *file* layer rather than in a
    Python ``logging.RotatingFileHandler``: the bytes we need to bound are
    written straight to fd 2 by Rich's traceback renderer and by grandchild
    processes (esbuild's Go runtime dumps its goroutine trace there), neither of
    which routes through Python's logging module.

    Best-effort by contract — any OSError is logged and swallowed so a rotation
    failure can never take down the daemon it is protecting.
    """
    if max_bytes <= 0:
        return False
    try:
        size = path.stat().st_size
    except OSError:
        # Missing / unreadable log is not an error: the daemon may not have
        # written anything yet.
        return False
    if size <= max_bytes:
        return False

    rotated = path.with_name(path.name + ROTATED_SUFFIX)
    try:
        shutil.copyfile(path, rotated)
        os.truncate(path, 0)
    except OSError as exc:
        logger.warning("log rotation failed for %s: %s", path, exc)
        return False
    logger.info("rotated %s (%d bytes) -> %s", path, size, rotated.name)
    return True


def rotate_daemon_logs(log_dir: Path, *, max_bytes: int | None = None) -> list[Path]:
    """Rotate every oversized daemon log in *log_dir*. Returns the rotated paths.

    Matches :data:`DAEMON_LOG_GLOB` only — so unrelated ``*.log`` files sharing
    the directory are never touched, and the retained ``.log.1`` generation is
    never re-rotated into itself. Best-effort: a missing directory yields ``[]``.
    """
    cap = resolve_max_bytes() if max_bytes is None else max_bytes
    try:
        candidates = sorted(log_dir.glob(DAEMON_LOG_GLOB))
    except OSError as exc:
        logger.warning("could not list %s: %s", log_dir, exc)
        return []
    return [path for path in candidates if rotate_if_oversized(path, max_bytes=cap)]


def start_background_rotator(
    log_dir: Path,
    *,
    max_bytes: int | None = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    """Start a daemon thread that re-rotates *log_dir* every *interval_seconds*.

    Shim-time rotation (see ``brain.templates.bin``) bounds the crash-loop
    shape, where the process restarts constantly. This covers the complementary
    case: a long-lived daemon that stays up but writes errors in an internal
    loop, which no amount of restart-time checking would ever catch.

    The thread is a daemon thread and waits on an ``Event``, so it never blocks
    interpreter shutdown. Tests pass their own *stop_event* to drive it
    deterministically.
    """
    event = stop_event if stop_event is not None else threading.Event()

    def _loop() -> None:
        while not event.wait(interval_seconds):
            rotate_daemon_logs(log_dir, max_bytes=max_bytes)

    thread = threading.Thread(target=_loop, name="brain-log-rotator", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    """Console entry — ``python -m brain.log_rotation [--log-dir DIR]``.

    Invoked by the launchd shim wrappers *before* they exec the daemon, so an
    oversized log from a previous crash-loop generation is capped on every
    restart. Always exits 0: log rotation must never be the reason a daemon
    fails to start.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="brain.log_rotation",
        description="Copy-truncate oversized brain daemon logs.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="Directory of *.log files (default: $BRAIN_HOME/logs).",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=None,
        help=f"Per-file cap (default: ${MAX_BYTES_ENV} or {DEFAULT_MAX_BYTES}).",
    )
    args = parser.parse_args(argv)

    log_dir: Path = args.log_dir if args.log_dir is not None else default_log_dir()

    try:
        rotated = rotate_daemon_logs(log_dir, max_bytes=args.max_bytes)
    except Exception as exc:  # noqa: BLE001 — best-effort, never fail daemon start
        logger.warning("log rotation sweep failed for %s: %s", log_dir, exc)
        return 0
    for path in rotated:
        print(f"rotated {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    raise SystemExit(main())
