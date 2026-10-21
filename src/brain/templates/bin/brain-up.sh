#!/usr/bin/env bash
# Start the second-brain vault-sync watcher (and the daily-brief job) under launchd.
# Idempotent — safe to re-run.
#
# Reading surface (phase 7, R1 "Demoted", 2026-10-12): `brain ui` is where you
# read. `brain ui` is foreground-only by design (no daemon), so this script
# names it rather than starting it.
#
# The Quartz wiki is DELETED (phase 9, R3, 2026-10-21). This script used to
# apply the Quartz overlay, run a cold-start build and open the wiki; all of
# that went with the wiki. Leftovers of an old install (a `<vault>/.quartz`
# workspace, a Caddy serving its last build) are inert and can be removed by
# hand. The retired build daemon (com.brain.build) is removed by
# brain-install-launchd, which this script runs.
#
# Architecture:
#   - The vault-sync watcher: `brain vault sync --watch` — DB <-> vault mirror,
#     supervised by launchd (com.brain.watcher, KeepAlive=true → respawn within
#     ~10s of any death). com.brain.brief is the 07:00 daily-brief job.
#
# Daemon supervision: this script delegates to install-launchd
# unconditionally; install-launchd is idempotent (rewrites + reboots the plists
# every run, no-ops if already loaded), so re-running brain-up is safe and
# self-healing if launchd has booted out the agents for any reason. The
# pre-launchd nohup paths were removed because they silently lost daemons to
# terminal close / sleep-wake / unexplained crashes.
#
# Env overrides:
#   BRAIN_NO_BUILD_WATCHER  (default: 0; set 1 to skip launchd supervision —
#                            used by the bin-script tests so they don't
#                            install LaunchAgents on the developer's
#                            machine. Name kept for backward compat with
#                            the pre-launchd nohup era.)
#   BRAIN_LAUNCHCTL         (default: launchctl on PATH) — passed through
#                            to install-launchd; tests stub it.
#   BRAIN_LAUNCHD_DIR       (default: ~/Library/LaunchAgents) — same.

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
# Resolve $BRAIN_HOME from this script's location ($BRAIN_HOME/.shims/ under a
# pipx install, $repo_root/bin/ in a dev checkout). Honor an explicit
# BRAIN_HOME override if the launcher set one.
BRAIN_HOME_RESOLVED="${BRAIN_HOME:-$( cd "$SCRIPT_DIR/.." && pwd )}"

# Pid paths default to $BRAIN_HOME/run/ — NOT /tmp. macOS's tmp_cleaner reaps
# files left untouched for 3 days, silently vanishing a long-lived daemon's
# pid file so brain-status misreports it as stopped; $BRAIN_HOME/run/ is not
# reaped, and must match the fg wrappers' defaults. Log paths keep their legacy
# /tmp fallback (the launchd plists redirect real output to $BRAIN_HOME/logs/).
# Overridable (unset = unchanged) so the test suite can run this shim
# hermetically against a tmp dir, never colliding with a live brain-up install.
WATCH_PID="${BRAIN_WATCH_PID:-$BRAIN_HOME_RESOLVED/run/brain-watch.pid}"
WATCH_LOG="${BRAIN_WATCH_LOG:-/tmp/brain-watch.log}"

ensure_supervised() {
    # brain (2026-05-08): single source of truth for daemon lifecycle.
    # `bin/brain-install-launchd` writes both LaunchAgent plists, boots out
    # any prior incarnation (so the script self-heals if launchd has the
    # labels in a half-loaded state), and bootstraps them into the
    # gui/$UID domain. From there on launchd's KeepAlive=true keeps both
    # daemons alive across crashes, terminal-close, sleep/wake — the very
    # failure modes the pre-launchd nohup paths used to lose.
    #
    # `BRAIN_NO_BUILD_WATCHER=1` keeps its name (back-compat with tests
    # that use the pre-launchd flag) but its meaning is now "don't install
    # LaunchAgents during this brain-up run." Tests rely on this so they
    # don't accidentally drop plists into the developer's
    # ~/Library/LaunchAgents.
    if [[ "${BRAIN_NO_BUILD_WATCHER:-0}" == '1' ]]; then
        echo 'launchd supervision: skipped (BRAIN_NO_BUILD_WATCHER=1)'
        return 0
    fi
    if ! "${BRAIN_INSTALL_LAUNCHD:-brain-install-launchd}"; then
        echo 'launchd supervision: brain-install-launchd failed — daemons NOT supervised' >&2
        return 1
    fi
}

ensure_supervised

# The banner can print before the launchd wrappers have written their pid
# files. Until phase 7 (R1) that was a rare race, masked by a 30s wait for the
# wiki's URL; that wait went with the wiki, so a missing pid file is the COMMON
# case. Say where to look rather than printing a bare '?'.
PID_PENDING='not written yet — check brain-status'
WATCH_PID_VALUE="$PID_PENDING"
[[ -f "$WATCH_PID" ]] && WATCH_PID_VALUE="$(cat "$WATCH_PID")"

# brain (2026-05-08): note the supervisor in the report so the user
# knows their daemons survive terminal close + crashes.
SUPERVISOR='launchd (KeepAlive)'
[[ "${BRAIN_NO_BUILD_WATCHER:-0}" == '1' ]] && SUPERVISOR='unsupervised (BRAIN_NO_BUILD_WATCHER=1)'

echo
echo '🧠 brain is up:'
echo '   read:       brain ui   (search, read and edit — opens a browser)'
echo "   vault:      re-syncing on file changes (pid $WATCH_PID_VALUE)"
echo "   supervisor: $SUPERVISOR"
echo '   stop:       brain-down'
echo '   status:     brain-status'
