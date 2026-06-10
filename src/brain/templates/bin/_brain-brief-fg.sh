#!/usr/bin/env bash
# One-shot wrapper for the daily brief, intended to run under launchd's
# StartCalendarInterval (07:00 local). Unlike _brain-watcher-fg / _brain-build-fg
# this does NOT supervise a long-running daemon and writes no pid file — it runs
# `brain brief --wiki` once and exits. launchd's StandardOut/ErrorPath capture
# the digest + any warnings.
#
# Env contract:
#   BRAIN_VAULT_PATH  (default: $HOME/brain-vault) — passed through to brief.
#   BRAIN_PY          pins the Python interpreter that has `brain` importable
#                     (exported by the launchd plist); falls back to python3.

set -euo pipefail

# Pick the Python interpreter once. BRAIN_PY wins; otherwise prefer python3.
if [[ -n "${BRAIN_PY:-}" ]]; then
    PY="$BRAIN_PY"
elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
else
    echo "no python interpreter found (set BRAIN_PY=… to override)" >&2
    exit 1
fi

exec "$PY" -m brain brief --wiki
