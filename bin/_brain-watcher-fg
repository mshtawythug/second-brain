#!/usr/bin/env bash
# Foreground wrapper for the vault sync watcher, intended to run under launchd.
#
# Why a wrapper:
#   The historical `bin/brain-up` flow uses `nohup ... &` to background-spawn
#   the watcher, then writes the spawned pid into the pid file for
#   `brain-status` to pick up. That works in a shell session but doesn't
#   survive the next "what kills my daemons silently?" round (terminal close,
#   sleep/wake, OOM, …) because nothing supervises the daemon.
#
#   This script flips the pattern: it stays in the foreground, writes the
#   pid file, then `exec`s the watcher so the watcher inherits the same pid.
#   A LaunchAgent (com.brain.watcher) wraps THIS script with `KeepAlive=true`
#   so launchd respawns us within `ThrottleInterval` seconds whenever the
#   underlying watcher dies — no manual `brain-up` after every crash.
#
# PID-file contract:
#   We write `$$` into the pid file ($BRAIN_HOME/run/brain-watch.pid) BEFORE
#   the exec. `exec` replaces
#   the current process image but keeps the pid, so the file points at the
#   still-running watcher. `brain-status`'s `kill -0 $(cat …)` check then
#   reports "running" exactly as it does for the legacy nohup path. When
#   launchd kills us out-of-band the file becomes stale; that's fine, the
#   `kill -0` check fails and the row flips to "stopped".
#
# Env contract:
#   BRAIN_VAULT_PATH  (default: $HOME/brain-vault) — passed to `--vault`.
#   PATH must include the pipx bin dir (or wherever `brain` is installed)
#   so `brain` resolves; the LaunchAgent plist's EnvironmentVariables block
#   sets this explicitly.

set -euo pipefail

VAULT="${BRAIN_VAULT_PATH:-$HOME/brain-vault}"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
VENV_BIN="$PROJECT_ROOT/.venv/bin"
# Pid path defaults to $BRAIN_HOME/run/ — NOT /tmp. macOS's tmp_cleaner reaps
# files left untouched for 3 days; this daemon writes the pid once at launch
# then runs for weeks, so a /tmp pid file silently vanishes and brain-status
# misreports the live daemon as stopped. $BRAIN_HOME resolves to an explicit
# BRAIN_HOME override if set, else PROJECT_ROOT (== the script's parent: the
# script lives in $BRAIN_HOME/.shims/ or $repo_root/bin/); kept consistent with
# the build fg wrapper and brain-{status,down,up}. Overridable (unset =
# unchanged) so tests run this wrapper hermetically against a tmp pid dir.
WATCH_PID="${BRAIN_WATCH_PID:-${BRAIN_HOME:-$PROJECT_ROOT}/run/brain-watch.pid}"
# brain: tests set BRAIN_SKIP_VENV_AUTOLOAD=1 to keep the wrapper from
# silently prepending the developer's real `.venv/bin` (and thus the real
# `brain`) ahead of the test stub on PATH. Same shape as the BRAIN_PY
# override in `bin/brain-up`. In production the variable is unset and the
# venv prepend just works (for dev-checkout users with a local .venv;
# pipx-installed users have brain on PATH via the launchd plist already).
if [[ "${BRAIN_SKIP_VENV_AUTOLOAD:-0}" != "1" && -d "$VENV_BIN" ]]; then
    export PATH="$VENV_BIN:$PATH"
fi

if ! command -v brain >/dev/null 2>&1; then
    echo "brain CLI not on PATH (expected via launchd PATH or local .venv)" >&2
    exit 1
fi

mkdir -p "$(dirname "$WATCH_PID")"
echo "$$" >"$WATCH_PID"
exec brain vault sync --watch --vault "$VAULT"
