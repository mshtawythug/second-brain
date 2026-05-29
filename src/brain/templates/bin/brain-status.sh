#!/usr/bin/env bash
# Report on what the brain is currently running (blue/green serve edition).

set -euo pipefail

VAULT="${BRAIN_VAULT_PATH:-$HOME/brain-vault}"
QUARTZ_DIR="$VAULT/.quartz"
PORT="${BRAIN_WIKI_PORT:-8080}"
URL="http://localhost:$PORT"

# Shim is installed at $BRAIN_HOME/.shims/brain-status, so $BRAIN_HOME is
# two parent-traversals up (.shims/ → $BRAIN_HOME). Honor an explicit
# BRAIN_HOME override if the launcher set one.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
BRAIN_HOME_RESOLVED="${BRAIN_HOME:-$( cd "$SCRIPT_DIR/.." && pwd )}"

# Pid paths default to $BRAIN_HOME/run/ — NOT /tmp. macOS's tmp_cleaner reaps
# files left untouched for 3 days; the build/watch daemons write their pid file
# once at launch and then run for weeks, so a /tmp pid file silently vanishes
# and this very script misreports a live daemon as "stopped". $BRAIN_HOME/run/
# is not reaped. Overridable (unset = unchanged) so the test suite can run this
# shim hermetically against a tmp pid dir, never colliding with a live
# brain-up install on a dev machine.
WATCH_PID="${BRAIN_WATCH_PID:-$BRAIN_HOME_RESOLVED/run/brain-watch.pid}"
BUILD_PID="${BRAIN_BUILD_PID:-$BRAIN_HOME_RESOLVED/run/brain-build.pid}"
WIKI_PID="${BRAIN_WIKI_PID:-$BRAIN_HOME_RESOLVED/run/brain-wiki.pid}"   # legacy quartz --serve install
# T1.7 plist templates redirect StandardOut to $BRAIN_HOME/logs/com.brain.*.out.log,
# not the legacy /tmp paths the pre-launchd nohup era used.
WATCH_LOG="$BRAIN_HOME_RESOLVED/logs/com.brain.watcher.out.log"
BUILD_LOG="$BRAIN_HOME_RESOLVED/logs/com.brain.build.out.log"

check_one() {
    local pid_file="$1"
    local label="$2"
    local extra="$3"
    if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        printf '  %-9s ✅ running  pid=%-7s %s\n' "$label:" "$(cat "$pid_file")" "$extra"
    else
        printf '  %-9s ⛔ stopped  %s\n' "$label:" "$extra"
    fi
}

echo '🧠 brain status'
check_one "$WATCH_PID" 'watcher' "(logs: $WATCH_LOG)"
check_one "$BUILD_PID" 'build'   "(logs: $BUILD_LOG)"

# Legacy row — only show if a stale pid file from the pre-blue/green
# install is still on disk. Helps diagnose half-migrated machines.
if [[ -f "$WIKI_PID" ]]; then
    if kill -0 "$(cat "$WIKI_PID")" 2>/dev/null; then
        printf '  %-9s ⚠️  legacy quartz --serve still running pid=%s — kill with brain-down\n' \
            'wiki:' "$(cat "$WIKI_PID")"
    else
        printf '  %-9s ⛔ stopped (legacy quartz --serve was here — pid file %s)\n' \
            'wiki:' "$WIKI_PID"
    fi
fi

# Current build pointer — what Caddy is serving right now.
CURRENT="$QUARTZ_DIR/current"
if [[ -L "$CURRENT" ]]; then
    target="$(readlink "$CURRENT")"
    if resolved="$(cd "$CURRENT" 2>/dev/null && pwd -P)"; then
        if [[ -f "$resolved/.build-id" ]]; then
            build_id="$(cat "$resolved/.build-id")"
        else
            build_id='(no .build-id)'
        fi
        printf '  current   → %s\n' "$target"
        printf '  build-id  %s\n' "$build_id"
    else
        printf '  current   ⚠️  symlink dangles → %s\n' "$target"
    fi
else
    printf '  current   ⛔ no symlink at %s (run brain-up to bootstrap)\n' "$CURRENT"
fi

# Quick health check on the wiki socket — Caddy now answers, not Quartz.
if curl -fsS -o /dev/null --max-time 2 "$URL/" 2>/dev/null; then
    echo '  wiki url  reachable ✅'
else
    echo '  wiki url  unreachable ⚠️ '
fi
