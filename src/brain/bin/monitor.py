"""brain-monitor — diagnose the brain vault-sync watcher.

Its Quartz build-watcher half (the build state, ``.quartz/current`` and
``builds/`` readout, the wiki URL, the build log, and the ``probe`` command
that timed a rebuild) was deleted with the wiki at R3 (wiki-to-ui phase 9).
"""

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import FrameType

from ..config import _brain_home_root

# ---------------------------------------------------------------------------
# Env defaults
# ---------------------------------------------------------------------------


def _vault() -> Path:
    return Path(os.environ.get("BRAIN_VAULT_PATH", str(Path.home() / "brain-vault")))


# ---------------------------------------------------------------------------
# Runtime state paths.
#
# The PID file lives under $BRAIN_HOME/run/, matching the foreground wrapper
# script (`_brain-watcher-fg.sh`). It used to default to /tmp/brain-watch.pid,
# but macOS's tmp_cleaner reaps files left untouched for 3 days — and the
# daemon writes its pid file once at launch then runs for weeks, so the /tmp pid
# file silently vanished and we misreported a live daemon as "stopped".
# $BRAIN_HOME/run/ is not reaped. The BRAIN_WATCH_PID env override mirrors the
# shell shims (and lets the test suite point it at a tmp dir). The log lives at
# `$BRAIN_HOME/logs/com.brain.watcher.{out,err}.log` (the launchd plist
# redirects StandardOut/StandardError there).
# ---------------------------------------------------------------------------

WATCH_PID = Path(
    os.environ.get("BRAIN_WATCH_PID", str(_brain_home_root() / "run" / "brain-watch.pid"))
)


def _watch_log() -> Path:
    return _brain_home_root() / "logs" / "com.brain.watcher.out.log"


# Directories to skip when walking the vault tree (mirrors the bash `find`
# -not -path invocations that used BSD-only stat -f flags). `.quartz` stays
# although the wiki is deleted (R3): an upgraded vault still holds the old
# workspace, with tens of thousands of files under node_modules, until the user
# removes it by hand (ruling R9-8).
_VAULT_SKIP_DIRS: frozenset[str] = frozenset({".quartz", ".git"})


# ---------------------------------------------------------------------------
# Cross-platform helpers (replace the BSD-only stat -f calls in the bash)
# ---------------------------------------------------------------------------


def _pid_alive(pid_file: Path) -> bool:
    """Return True if the process whose PID is stored in *pid_file* is running."""
    if not pid_file.is_file():
        return False
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)  # signal 0 = existence check only
    except (OSError, ProcessLookupError):
        return False
    return True


def _pid_or_dash(pid_file: Path) -> str:
    """Return the PID from *pid_file* as a string, or '-' if missing/unreadable."""
    if not pid_file.is_file():
        return "-"
    try:
        return pid_file.read_text().strip() or "-"
    except OSError:
        return "-"


def _recent_vault_files(vault: Path, n: int = 5) -> list[tuple[float, str]]:
    """Return the *n* most-recently-modified vault files as (mtime, path) tuples.

    Excludes .quartz/, .git/ sub-trees and .DS_Store files, matching the bash
    ``find … -not -path … -not -name .DS_Store`` invocation that used
    BSD-only ``stat -f '%m %N'`` for cross-platform mtime retrieval.
    """
    results: list[tuple[float, str]] = []
    for root, dirs, files in os.walk(str(vault)):
        # Prune skip-dirs in-place so os.walk won't descend into them.
        dirs[:] = [d for d in dirs if d not in _VAULT_SKIP_DIRS]
        for fname in files:
            if fname == ".DS_Store":
                continue
            p = Path(root) / fname
            try:
                mtime = p.stat().st_mtime
                results.append((mtime, str(p)))
            except OSError:
                continue
    results.sort(key=lambda x: x[0], reverse=True)
    return results[:n]


def _pgrep_count(pattern: str) -> int:
    """Return the number of running processes matching *pattern* (0 on any error)."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return len([ln for ln in result.stdout.splitlines() if ln.strip()])
    except (OSError, subprocess.TimeoutExpired):
        return 0


# ---------------------------------------------------------------------------
# Snapshot mode
# ---------------------------------------------------------------------------


def _snapshot(vault: Path) -> int:
    """Print a status snapshot of the sync watcher and recent activity."""
    sync_pid = _pid_or_dash(WATCH_PID)
    sync_state = "running" if _pid_alive(WATCH_PID) else "stopped"

    print(f"🧠 brain-monitor   vault={vault}")
    print()
    print(f"  sync watcher   {sync_state:<12}  pid={sync_pid:<7}  log={_watch_log()}")

    # Cross-check: PID file says one thing; ps may say another. Surface mismatches.
    sync_seen = _pgrep_count("brain vault sync --watch")
    if sync_seen != 1:
        print()
        print("  ⚠️  process count vs pid file:")
        print(f"       sync_watcher  pgrep={sync_seen}  pidfile={sync_pid}")
        print("       (>1 = orphan running; 0 with pidfile = stale pid)")

    # Recent vault edits
    print()
    print("  recent vault edits:")
    for mtime, path in _recent_vault_files(vault, 5):
        t = datetime.fromtimestamp(mtime).strftime("%H:%M:%S")
        print(f"  {t}  {path}")

    # Last 5 sync-log lines
    print()
    print("  last 5 sync-log lines:")
    if _watch_log().is_file() and _watch_log().stat().st_size > 0:
        try:
            watch_lines = _watch_log().read_text(errors="replace").splitlines()[-5:]
            for ln in watch_lines:
                print(f"    {ln}")
        except OSError:
            print("    (error reading watch log)")
    else:
        print("    (empty)")

    return 0


# ---------------------------------------------------------------------------
# Live tail mode
# ---------------------------------------------------------------------------


def _tail(vault: Path) -> int:
    """Stream the sync-watcher log to stdout with a source prefix."""
    if not _watch_log().is_file():
        print("no log to tail (the sync watcher has not started)", file=sys.stderr)
        return 1

    print("tailing the sync watcher — Ctrl-C to stop")
    print(f"  sync:  {_watch_log()}")
    print()

    procs: list[subprocess.Popen[str]] = []

    def _stream(log_path: Path, prefix: str) -> None:
        """Tail *log_path* and print prefixed lines to stdout."""
        try:
            proc: subprocess.Popen[str] = subprocess.Popen(
                ["tail", "-n", "0", "-F", str(log_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,  # line-buffered: each line flushed immediately (matches sed -u)
            )
            procs.append(proc)
            if proc.stdout is None:
                return
            for raw_line in proc.stdout:
                stripped = raw_line.rstrip("\n")
                print(f"{prefix}{stripped}", flush=True)
        except (OSError, ValueError):
            pass

    def _cleanup(signum: int, frame: FrameType | None) -> None:
        for p in procs:
            with contextlib.suppress(OSError):
                p.terminate()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    thread = threading.Thread(target=_stream, args=(_watch_log(), "[sync ] "), daemon=True)
    thread.start()
    thread.join()

    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brain-monitor",
        description=(
            "Diagnose the brain vault-sync watcher.\n\n"
            "Modes:\n"
            "  (default)   Snapshot of the sync watcher and recent activity.\n"
            "  -f/--tail   Live stream of the sync-watcher log.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("Env vars:\n  BRAIN_VAULT_PATH   default ~/brain-vault"),
    )
    # Accept all bash-compatible positional aliases.  "tail" and "help" are
    # kept as aliases so existing scripts using the bash form still work.
    parser.add_argument(
        "command",
        nargs="?",
        choices=["snapshot", "status", "tail", "help"],
        default=None,
        metavar="COMMAND",
        help="snapshot | status | tail | help (default: snapshot)",
    )
    parser.add_argument(
        "-f",
        "--tail",
        action="store_true",
        help="Live-stream the sync-watcher log.",
    )
    return parser


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch to the appropriate mode. Returns exit code."""
    parser = _make_parser()
    # parse_known_args used to carve out `brain-monitor probe 60` (a positional
    # timeout). `probe` was deleted with the wiki at R3, so every leftover token
    # is now an error — `brain-monitor probe` itself is rejected by `choices=`.
    try:
        args, extra = parser.parse_known_args(argv)
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        return 0

    vault = _vault()

    cmd: str = str(args.command) if args.command else "snapshot"

    # Validate leftover tokens BEFORE dispatching to any mode (including
    # --tail), so e.g. `brain-monitor --tail --bogus` or `brain-monitor
    # snapshot --x` fail loudly rather than being silently swallowed.
    if extra:
        print(
            f"brain-monitor: error: unrecognized arguments: {' '.join(extra)}",
            file=sys.stderr,
        )
        parser.print_usage(sys.stderr)
        return 2

    # -f / --tail flag takes precedence over any positional.
    if args.tail:
        return _tail(vault)

    if cmd in ("snapshot", "status"):
        return _snapshot(vault)

    # "tail" as a bare positional: bash-compat alias for -f/--tail.
    if cmd == "tail":
        return _tail(vault)

    # "help" as a bare positional: bash-compat alias for -h/--help.
    if cmd == "help":
        parser.print_help()
        return 0

    # Defensive: argparse choices= already rejects truly unknown values.
    print(f"unknown command: {cmd}", file=sys.stderr)
    parser.print_help(sys.stderr)
    return 2


def cli_main() -> None:
    """Console-script entry point. Calls sys.exit with main()'s return code."""
    sys.exit(main())
