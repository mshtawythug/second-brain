#!/usr/bin/env bash
# One-shot wrapper for the drop-folder job (RM-33), run every 10 minutes by
# launchd's StartInterval in com.brain.dropfolders. Like _brain-brief-fg it
# supervises nothing and writes no pid file: it rotates the logs, then runs
# `brain ingest-folder --configured --since 2d` once and exits.
#
# Env contract:
#   BRAIN_HOME  where drop-folders.toml lives (exported by the plist).
#   BRAIN_PY    pins the Python interpreter that has `brain` importable
#               (exported by the launchd plist); falls back to python3.

set -euo pipefail

if [[ -n "${BRAIN_PY:-}" ]]; then
    PY="$BRAIN_PY"
elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
else
    echo "no python interpreter found (set BRAIN_PY=… to override)" >&2
    exit 1
fi

# Cap oversized logs before this run writes (see _brain-brief-fg).
"$PY" -m brain.log_rotation 2>/dev/null || true

exec "$PY" -m brain ingest-folder --configured --since 2d
