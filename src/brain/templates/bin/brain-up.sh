#!/usr/bin/env bash
# Start the second-brain vault-sync watcher (and, on request, build the legacy wiki).
# Idempotent — safe to re-run.
#
# Reading surface (phase 7, R1 "Demoted", 2026-10-12): `brain ui` is where you
# read. `brain ui` is foreground-only by design (no daemon), so this script
# names it rather than starting it.
#
# The wiki is DORMANT (phase 8, R2): this script no longer builds it. Caddy
# keeps serving <vault>/.quartz/current/ (the last build) on :8080 +
# brain.test, but nothing rebuilds it unless asked: `brain-rebuild --only
# quartz` for a one-off rebuild, or BRAIN_WIKI_BUILD=1 to have this run apply
# the overlay and cold-start build as it used to. The Quartz rebuild daemon
# (com.brain.build) is retired, and the next brain-install-launchd removes it.
#
# Architecture at R2:
#   - The vault-sync watcher: `brain vault sync --watch` — DB <-> vault mirror,
#     supervised by launchd (com.brain.watcher, KeepAlive=true → respawn within
#     ~10s of any death). com.brain.brief is the 07:00 daily-brief job.
#   - BRAIN_WIKI_BUILD=1 only: apply the brain Quartz overlay
#     (`quartz_overrides/`) to the workspace, then, if <vault>/.quartz/current
#     is missing or unhealthy, run a one-shot `python -m brain.wiki.build_swap`
#     so the wiki has something to serve. BRAIN_NO_OVERLAY=1 skips the overlay.
#
# Daemon supervision: this script delegates to install-launchd
# unconditionally; install-launchd is idempotent (rewrites + reboots the plists
# every run, no-ops if already loaded), so re-running brain-up is safe and
# self-healing if launchd has booted out the agents for any reason. The
# pre-launchd nohup paths were removed because they silently lost daemons to
# terminal close / sleep-wake / unexplained crashes.
#
# Env overrides:
#   BRAIN_VAULT_PATH        (default: ~/brain-vault)
#   BRAIN_WIKI_PORT         (default: 8080 — for the URL we curl/open)
#   BRAIN_OPEN_BROWSER      (default: 0; set 1 to open the wiki — opt-in since R1)
#   BRAIN_WIKI_BUILD        (default: 0; set 1 to apply the overlay and run the
#                            cold-start wiki build — the wiki is dormant since R2)
#   BRAIN_NO_OVERLAY        (default: 0; set 1 to skip the overlay step)
#   BRAIN_WIKI_KEEP_BUILDS  (default: 3 — old build dirs retained per swap)
#   BRAIN_NO_BUILD_WATCHER  (default: 0; set 1 to skip launchd supervision —
#                            used by the bin-script tests so they don't
#                            install LaunchAgents on the developer's
#                            machine. Name kept for backward compat with
#                            the pre-launchd nohup era.)
#   BRAIN_LAUNCHCTL         (default: launchctl on PATH) — passed through
#                            to install-launchd; tests stub it.
#   BRAIN_LAUNCHD_DIR       (default: ~/Library/LaunchAgents) — same.

set -euo pipefail

VAULT="${BRAIN_VAULT_PATH:-$HOME/brain-vault}"
QUARTZ_DIR="$VAULT/.quartz"
PORT="${BRAIN_WIKI_PORT:-8080}"
URL="http://localhost:$PORT"
KEEP="${BRAIN_WIKI_KEEP_BUILDS:-3}"

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

# Pick the Python interpreter once. BRAIN_PY (env var) wins; otherwise
# prefer python3 on PATH. The installed-shim flow (in $BRAIN_HOME/bin/)
# always has BRAIN_PY exported by the Python launcher in
# src/brain/bin/_launcher.py — but we keep the python3 fallback for
# users who invoke the shims directly outside the launcher (e.g. via
# launchd plists that themselves set PATH but not BRAIN_PY).
if [[ -n "${BRAIN_PY:-}" ]]; then
    PY="$BRAIN_PY"
elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
else
    echo "no python interpreter found (set BRAIN_PY=… to override)" >&2
    exit 1
fi

apply_overlay() {
    if [[ "${BRAIN_NO_OVERLAY:-0}" == '1' ]]; then
        echo 'overlay: skipped (BRAIN_NO_OVERLAY=1)'
        return 0
    fi
    if ! command -v brain >/dev/null 2>&1; then
        echo 'overlay: skipped (brain CLI not on PATH)'
        return 0
    fi
    # `--no-build` applies the overlay to the workspace without running
    # `npx quartz build` — the cold-start build below runs one if needed.
    if ! brain vault render --overlay --no-build --vault "$VAULT" 2>&1; then
        echo 'overlay: failed — wiki will start with whatever is in the workspace' >&2
        return 0
    fi
}

current_healthy() {
    # Healthy = `current` is a symlink, resolves to a directory, and that
    # directory contains both `index.html` AND `.build-id`. Anything else
    # (no symlink, dangling target, empty target, missing `.build-id`)
    # means we need a cold-start build.
    local cur="$QUARTZ_DIR/current"
    if [[ ! -L "$cur" ]]; then
        return 1
    fi
    local resolved
    resolved="$(cd "$cur" 2>/dev/null && pwd -P)" || return 1
    if [[ -z "$resolved" || ! -d "$resolved" ]]; then
        return 1
    fi
    if [[ ! -f "$resolved/index.html" || ! -f "$resolved/.build-id" ]]; then
        return 1
    fi
    return 0
}

cold_start_build() {
    if current_healthy; then
        echo 'cold-start build: skipping cold-start build (current/ is healthy)'
        return 0
    fi
    if [[ ! -d "$QUARTZ_DIR" ]]; then
        echo "quartz workspace not found at $QUARTZ_DIR" >&2
        echo 'set up once with:' >&2
        echo "  git clone https://github.com/jackyzha0/quartz.git \"$QUARTZ_DIR\"" >&2
        echo "  cd \"$QUARTZ_DIR\" && npm install" >&2
        echo "  cp \"$SCRIPT_DIR/../quartz.config.ts\" ./quartz.config.ts  # optional brain-tuned config" >&2
        return 1
    fi
    echo 'cold-start build: running first-time build, this takes ~40s...'
    local started
    started="$(date +%s)"
    if ! BRAIN_WIKI_RELOAD=1 "$PY" -m brain.wiki.build_swap \
            --vault "$VAULT" --keep "$KEEP"; then
        echo 'cold-start build: failed — aborting brain-up' >&2
        return 1
    fi
    local elapsed=$(( $(date +%s) - started ))
    echo "cold-start build: first build done in ${elapsed}s"
}

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

wait_for_url() {
    local tries=30
    while ((tries-- > 0)); do
        if curl -fsS -o /dev/null "$URL/"; then
            return 0
        fi
        sleep 1
    done
    echo "warning: $URL did not respond within 30s — is Caddy running and serving $QUARTZ_DIR/current?" >&2
    return 1
}

# R2 (phase 8): the wiki is dormant, so the Quartz steps run only on request.
if [[ "${BRAIN_WIKI_BUILD:-0}" == '1' ]]; then
    apply_overlay
    cold_start_build
else
    echo 'legacy wiki: not built — dormant since R2 (BRAIN_WIKI_BUILD=1 builds it here; brain-rebuild --only quartz rebuilds it)'
fi
ensure_supervised

# Opt-in since phase 7 (R1): the daily loop no longer points at the wiki.
if [[ "${BRAIN_OPEN_BROWSER:-0}" == '1' ]]; then
    if wait_for_url; then
        if command -v open >/dev/null 2>&1; then
            open "$URL"
        elif command -v xdg-open >/dev/null 2>&1; then
            xdg-open "$URL" >/dev/null 2>&1 &
        fi
    fi
fi

# The banner can print before the launchd wrappers have written their pid
# files. Until phase 7 (R1) that was a rare race, masked by wait_for_url's 30s
# wait; since R1 that wait only runs with BRAIN_OPEN_BROWSER=1, so on the
# default path a missing pid file is the COMMON case. Say where to look rather
# than printing a bare '?'.
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
echo "   wiki:       $URL  (legacy, dormant since R2: serves its last build — BRAIN_OPEN_BROWSER=1 opens it)"
echo "   vault:      re-syncing on file changes (pid $WATCH_PID_VALUE)"
echo '   build:      dormant since R2 — brain-rebuild --only quartz rebuilds the wiki'
echo "   supervisor: $SUPERVISOR"
echo '   stop:       brain-down'
echo '   status:     brain-status'
