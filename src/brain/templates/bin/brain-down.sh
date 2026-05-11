#!/usr/bin/env bash
# Stop the second-brain wiki build watcher + sync watcher (blue/green serve edition).
# Idempotent — safe to re-run.
#
# Caddy is intentionally left running: it keeps serving the last-good build
# from <vault>/.quartz/current so brain.test stays healthy across restarts.

set -euo pipefail

WATCH_PID='/tmp/brain-watch.pid'   # `brain vault sync --watch`
BUILD_PID='/tmp/brain-build.pid'   # `python -m brain.wiki.build_watcher`
WIKI_PID='/tmp/brain-wiki.pid'     # legacy: pre-blue/green `npx quartz --serve`

LAUNCHD_DIR="${BRAIN_LAUNCHD_DIR:-$HOME/Library/LaunchAgents}"
LAUNCHCTL="${BRAIN_LAUNCHCTL:-launchctl}"

# brain: if the user installed launchd supervision via
# `bin/brain-install-launchd`, plain `kill $pid` is ineffective — launchd
# respawns each daemon within ThrottleInterval seconds. Bootout the
# LaunchAgents first so the kills below actually stick. We don't remove
# the .plist files here (that's `bin/brain-uninstall-launchd`'s job);
# bootout-only means a subsequent `bin/brain-up` won't re-supervise, but
# `launchctl bootstrap gui/$UID <plist>` (or rerunning the install
# script) will restore supervision without rewriting the plists.
bootout_one() {
    local label="$1"
    local domain="gui/$UID/${label}"
    if "$LAUNCHCTL" print "$domain" >/dev/null 2>&1; then
        "$LAUNCHCTL" bootout "$domain" 2>/dev/null || true
        echo "unloaded launchd: $domain"
    fi
}

bootout_one com.brain.watcher
bootout_one com.brain.build

stop_one() {
    local pid_file="$1"
    local label="$2"
    if [[ ! -f "$pid_file" ]]; then
        return 0
    fi
    local pid
    pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        sleep 1
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        echo "stopped $label (pid $pid)"
    fi
    rm -f "$pid_file"
}

stop_one "$WATCH_PID" 'watcher (vault sync)'
stop_one "$BUILD_PID" 'build watcher'
# Belt-and-suspenders: only stop a legacy wiki pid file if a stale install
# left one behind. Brand-new clones won't have this file.
stop_one "$WIKI_PID"  'wiki (legacy quartz --serve)'

# Belt + suspenders: any orphaned processes from prior crashed runs.
pkill -f 'brain.wiki.build_watcher' 2>/dev/null || true
pkill -f 'brain vault sync --watch' 2>/dev/null || true

echo '🧠 brain is down'
echo '   (caddy left running — brain.test continues to serve last good build)'
