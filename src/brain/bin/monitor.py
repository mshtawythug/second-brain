"""brain-monitor — diagnose the brain sync + build watchers."""

import argparse
import contextlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from types import FrameType

# ---------------------------------------------------------------------------
# Env defaults
# ---------------------------------------------------------------------------


def _vault() -> Path:
    return Path(os.environ.get("BRAIN_VAULT_PATH", str(Path.home() / "brain-vault")))


def _port() -> int:
    return int(os.environ.get("BRAIN_WIKI_PORT", "8080"))


# ---------------------------------------------------------------------------
# Fixed /tmp paths
# ---------------------------------------------------------------------------

WATCH_PID = Path("/tmp/brain-watch.pid")
WATCH_LOG = Path("/tmp/brain-watch.log")
BUILD_PID = Path("/tmp/brain-build.pid")
BUILD_LOG = Path("/tmp/brain-build.log")

# Directories to skip when walking the vault tree (mirrors the bash `find`
# -not -path invocations that used BSD-only stat -f flags).
_VAULT_SKIP_DIRS: frozenset[str] = frozenset({".quartz", ".git"})


# ---------------------------------------------------------------------------
# Build-log noise filter — mirrors the bash grep -Ev shape exactly.
# Also drops blank lines so trailing KaTeX bursts don't pollute the tail.
# ---------------------------------------------------------------------------

_BUILD_LOG_NOISE = re.compile(
    r"^\s*$"
    r"|LaTeX-incompatible input"
    r"|No character metrics for"
    r"|Warning: couldn't find git repository"
    r"|^Parsing input files using"
    r"|^Cleaned output directory"
)


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


def _current_build_id(vault: Path) -> str:
    """Return the current build ID from vault/.quartz/current/.build-id."""
    build_id_file = vault / ".quartz" / "current" / ".build-id"
    if not build_id_file.is_file():
        return "(none)"
    try:
        return build_id_file.read_text().strip()
    except OSError:
        return "(none)"


def _recent_builds(vault: Path, n: int = 5) -> list[str]:
    """Return the *n* most-recent build directory names (lexical == chronological)."""
    builds_dir = vault / ".quartz" / "builds"
    if not builds_dir.is_dir():
        return []
    names = sorted(p.name for p in builds_dir.iterdir())
    return names[-n:]


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


def _mtime_human(p: Path, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Return the mtime of *p* formatted according to *fmt*, or '?' on error.

    Replaces the BSD-only ``stat -f '%Sm' -t '<fmt>'`` calls in the bash.
    """
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).strftime(fmt)
    except OSError:
        return "?"


def _filter_build_log_line(line: str) -> bool:
    """Return True if *line* should be KEPT (i.e. it is not noise)."""
    return not bool(_BUILD_LOG_NOISE.search(line))


def _url_responds(url: str, timeout: float = 2.0) -> bool:
    """Return True if *url* returns a non-error response within *timeout* seconds."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return int(resp.status) < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


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


def _snapshot(vault: Path, port: int) -> int:
    """Print a status snapshot of both watchers and recent activity."""
    sync_pid = _pid_or_dash(WATCH_PID)
    build_pid = _pid_or_dash(BUILD_PID)
    sync_state = "running" if _pid_alive(WATCH_PID) else "stopped"
    build_state = "running" if _pid_alive(BUILD_PID) else "stopped"
    url = f"http://localhost:{port}"

    print(f"🧠 brain-monitor   vault={vault}")
    print()
    print(f"  sync watcher   {sync_state:<7}  pid={sync_pid:<7}  log={WATCH_LOG}")
    print(f"  build watcher  {build_state:<7}  pid={build_pid:<7}  log={BUILD_LOG}")

    # Cross-check: PID file says one thing; ps may say another. Surface mismatches.
    sync_seen = _pgrep_count("brain vault sync --watch")
    build_seen = _pgrep_count("brain.wiki.build_watcher")
    if sync_seen != 1 or build_seen != 1:
        print()
        print("  ⚠️  process count vs pid file:")
        print(f"       sync_watcher  pgrep={sync_seen}  pidfile={sync_pid}")
        print(f"       build_watcher pgrep={build_seen}  pidfile={build_pid}")
        print("       (>1 = orphan running; 0 with pidfile = stale pid)")

    # Current build
    print()
    print("  current build:")
    current = vault / ".quartz" / "current"
    if current.is_symlink():
        target = os.readlink(str(current))
        print(f"    symlink → {target}")
        print(f"    build-id {_current_build_id(vault)}")
        index_html = current / "index.html"
        if index_html.is_file():
            age = _mtime_human(index_html, "%Y-%m-%d %H:%M:%S")
            print(f"    last build {age}")
    else:
        print("    (no symlink — run brain-up to bootstrap)")

    # Recent builds
    builds_dir = vault / ".quartz" / "builds"
    if builds_dir.is_dir():
        print()
        print("  recent builds:")
        cur_id = _current_build_id(vault)
        for b in _recent_builds(vault, 5):
            mark = "→ " if b == cur_id else "  "
            mt = _mtime_human(builds_dir / b, "%H:%M:%S")
            print(f"    {mark}{mt}   {b}")

    # Recent vault edits
    print()
    print("  recent vault edits:")
    for mtime, path in _recent_vault_files(vault, 5):
        t = datetime.fromtimestamp(mtime).strftime("%H:%M:%S")
        print(f"  {t}  {path}")

    # Wiki URL
    print()
    print("  wiki url:")
    if _url_responds(f"{url}/"):
        print(f"    {url}  reachable ✅")
    else:
        print(f"    {url}  unreachable ⚠️")

    # Last 8 build-log lines (filtered)
    print()
    print("  last 8 build-log lines (filtered):")
    if BUILD_LOG.is_file() and BUILD_LOG.stat().st_size > 0:
        try:
            tail_lines = BUILD_LOG.read_text(errors="replace").splitlines()[-1000:]
            filtered = [ln for ln in tail_lines if _filter_build_log_line(ln)]
            last_8 = filtered[-8:]
        except OSError:
            last_8 = []
        if last_8:
            for ln in last_8:
                print(f"    {ln}")
        else:
            print(
                "    (only KaTeX/git-warnings in the tail window — filter saw nothing else)"
            )
    else:
        print("    (empty)")

    # Last 5 sync-log lines
    print()
    print("  last 5 sync-log lines:")
    if WATCH_LOG.is_file() and WATCH_LOG.stat().st_size > 0:
        try:
            watch_lines = WATCH_LOG.read_text(errors="replace").splitlines()[-5:]
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
    """Stream both watcher logs to stdout with source prefixes."""
    if not BUILD_LOG.is_file() and not WATCH_LOG.is_file():
        print("no logs to tail (neither watcher has started)", file=sys.stderr)
        return 1

    print("tailing watchers — Ctrl-C to stop")
    print(f"  sync:  {WATCH_LOG}")
    print(f"  build: {BUILD_LOG}  (KaTeX/git-warnings filtered)")
    print()

    procs: list[subprocess.Popen[str]] = []

    def _stream(log_path: Path, prefix: str, filtered: bool) -> None:
        """Tail *log_path* and print prefixed lines to stdout."""
        try:
            proc: subprocess.Popen[str] = subprocess.Popen(
                ["tail", "-n", "0", "-F", str(log_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            procs.append(proc)
            if proc.stdout is None:
                return
            for raw_line in proc.stdout:
                stripped = raw_line.rstrip("\n")
                if filtered and not _filter_build_log_line(stripped):
                    continue
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

    threads = [
        threading.Thread(target=_stream, args=(WATCH_LOG, "[sync ] ", False), daemon=True),
        threading.Thread(target=_stream, args=(BUILD_LOG, "[build] ", True), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return 0


# ---------------------------------------------------------------------------
# Probe mode
# ---------------------------------------------------------------------------


def _probe(vault: Path, timeout: int = 90) -> int:
    """Write a probe file into vault and time the build watcher's response."""
    if not vault.is_dir():
        print(f"vault not found at {vault}", file=sys.stderr)
        return 1
    if not _pid_alive(BUILD_PID):
        print("build watcher not running — start with brain-up", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    probe_id = f"probe-{stamp}-{os.getpid()}"
    probe_file = vault / f"_brain_monitor_{probe_id}.md"
    baseline = _current_build_id(vault)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    probe_file.write_text(
        f"---\n"
        f"title: brain-monitor probe {probe_id}\n"
        f"---\n"
        f"\n"
        f"This file was written by brain-monitor at {now_str}.\n"
        f"Marker: {probe_id}\n"
    )

    print(f"🧠 probe {probe_id}")
    print(f"  wrote   {probe_file}")
    print(f"  baseline build-id: {baseline}")
    print(f"  waiting up to {timeout}s for the build watcher to react...")
    print()

    started = time.monotonic()
    swap_elapsed: str | None = None
    rendered_elapsed: str | None = None
    current_dir = vault / ".quartz" / "current"

    try:
        while True:
            elapsed = int(time.monotonic() - started)
            cur = _current_build_id(vault)

            if swap_elapsed is None and cur != baseline and cur != "(none)":
                swap_elapsed = f"{elapsed}s"
                print(f"  +{elapsed}s  current swapped → {cur}")

            if rendered_elapsed is None:
                matches = list(current_dir.glob(f"_brain_monitor_{probe_id}*"))
                if matches:
                    rendered_elapsed = f"{elapsed}s"
                    print(f"  +{elapsed}s  probe HTML appeared in current/")
                    break

            if elapsed >= timeout:
                break

            time.sleep(1)
    finally:
        with contextlib.suppress(OSError):
            probe_file.unlink(missing_ok=True)

    print()
    print("  result:")
    if swap_elapsed is not None and rendered_elapsed is not None:
        print("    ✅ end-to-end OK")
        print(f"       swap to new build:    {swap_elapsed}")
        print(f"       probe content live:   {rendered_elapsed}")
    elif swap_elapsed is not None:
        print(f"    ⚠️ build ran ({swap_elapsed}) but probe HTML never appeared in current/")
        print("       likely a build_swap mismatch or the probe was filtered out")
        print(f"       inspect: tail -n 200 {BUILD_LOG} | grep -i {probe_id}")
    else:
        print(f"    ❌ no rebuild within {timeout}s — watcher is not seeing the vault")
        print("       things to check:")
        print(
            "         pgrep -af brain.wiki.build_watcher"
            "        # one process? right --vault?"
        )
        print("         pgrep -af 'brain vault sync --watch'      # same")
        print(f"         tail -n 50 {BUILD_LOG}")
        print(f"         tail -n 50 {WATCH_LOG}")
        print("         brain-down && brain-up                    # nuclear option")

    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brain-monitor",
        description=(
            "Diagnose the brain sync + build watchers.\n\n"
            "Modes:\n"
            "  (default)   Snapshot of both watchers and recent activity.\n"
            "  -f/--tail   Live stream of relevant events from both logs.\n"
            "  probe       Touch a vault file and time the rebuild.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Env vars:\n"
            "  BRAIN_VAULT_PATH   default ~/brain-vault\n"
            "  BRAIN_WIKI_PORT    default 8080"
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["probe", "snapshot", "status"],
        default=None,
        metavar="COMMAND",
        help="probe | snapshot | status (default: snapshot)",
    )
    parser.add_argument(
        "-f",
        "--tail",
        action="store_true",
        help="Live-stream both watcher logs (KaTeX noise filtered on build log).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=90,
        metavar="SECONDS",
        help="Timeout for probe mode in seconds (default: 90).",
    )
    return parser


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch to the appropriate mode. Returns exit code."""
    parser = _make_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        return 0

    vault = _vault()
    port = _port()

    if args.tail:
        return _tail(vault)

    cmd: str = str(args.command) if args.command else "snapshot"
    if cmd in ("snapshot", "status"):
        return _snapshot(vault, port)
    if cmd == "probe":
        return _probe(vault, int(args.timeout))

    # Defensive: argparse choices= already rejects unknown values above.
    print(f"unknown command: {cmd}", file=sys.stderr)
    parser.print_help(sys.stderr)
    return 2


def cli_main() -> None:
    """Console-script entry point. Calls sys.exit with main()'s return code."""
    sys.exit(main())
