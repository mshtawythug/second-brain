#!/usr/bin/env bash
# Report on what the brain is currently running.
#
# The Quartz build-watcher row, the `<vault>/.quartz/current` build pointer and
# the wiki-URL probe were deleted with the wiki at R3 (wiki-to-ui phase 9). The
# legacy `quartz --serve` row stays: it only appears when an old install left
# its pid file behind (ruling R9-1, legacy cleanup survives).

set -euo pipefail

# Shim is installed at $BRAIN_HOME/.shims/brain-status, so $BRAIN_HOME is
# two parent-traversals up (.shims/ → $BRAIN_HOME). Honor an explicit
# BRAIN_HOME override if the launcher set one.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
BRAIN_HOME_RESOLVED="${BRAIN_HOME:-$( cd "$SCRIPT_DIR/.." && pwd )}"
# BRAIN_PY is exported by the console-script launcher (src/brain/bin/_launcher.py)
# and is by construction an interpreter with `brain` importable.
PY="${BRAIN_PY:-python3}"

# Pid paths default to $BRAIN_HOME/run/ — NOT /tmp. macOS's tmp_cleaner reaps
# files left untouched for 3 days; the watch daemon writes its pid file
# once at launch and then runs for weeks, so a /tmp pid file silently vanishes
# and this very script misreports a live daemon as "stopped". $BRAIN_HOME/run/
# is not reaped. Overridable (unset = unchanged) so the test suite can run this
# shim hermetically against a tmp pid dir, never colliding with a live
# brain-up install on a dev machine.
WATCH_PID="${BRAIN_WATCH_PID:-$BRAIN_HOME_RESOLVED/run/brain-watch.pid}"
WIKI_PID="${BRAIN_WIKI_PID:-$BRAIN_HOME_RESOLVED/run/brain-wiki.pid}"   # legacy quartz --serve install
# T1.7 plist templates redirect StandardOut to $BRAIN_HOME/logs/com.brain.*.out.log,
# not the legacy /tmp paths the pre-launchd nohup era used.
WATCH_LOG="$BRAIN_HOME_RESOLVED/logs/com.brain.watcher.out.log"

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

# The vault the installers resolve, and the one brain-up bakes into the plists
# (brain.install_config: exported BRAIN_VAULT_PATH, else $BRAIN_HOME/.env, else
# the default). Ask that resolver rather than restate it here. It is resolved
# NOW: if the config changed since the last brain-up, the daemons still watch
# the value baked then, until the next brain-up re-bakes it. Resolved after the
# daemon rows on purpose: a broken interpreter costs the vault row, never the
# daemon report.
if VAULT="$("$PY" -m brain.install_config "$BRAIN_HOME_RESOLVED")" && [[ -n "$VAULT" ]]; then
    printf '  vault     %s\n' "$VAULT"
else
    printf '  vault     ⚠️  unresolved — %s -m brain.install_config failed\n' "$PY"
fi
