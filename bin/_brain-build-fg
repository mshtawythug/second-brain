#!/usr/bin/env bash
# Foreground wrapper for the Quartz build watcher, intended to run under
# launchd. Mirrors `_brain-watcher-fg`'s contract — see that file's header
# for the rationale + pid-file semantics.
#
# This wrapper specifically supervises `python -m brain.wiki.build_watcher`,
# which re-renders the Quartz site into a fresh `<vault>/.quartz/builds/<id>/`
# and atomically retargets `current/` on every vault change. Pre-launchd it
# was started via `nohup ... &` from `bin/brain-up`, with the same drift
# problem (silent death + no supervisor).
#
# Env contract:
#   BRAIN_VAULT_PATH        (default: $HOME/brain-vault) — passed to `--vault`.
#   BRAIN_WIKI_KEEP_BUILDS  (default: 3) — old build dirs retained per swap.
#   The wrapper sets BRAIN_WIKI_RELOAD=1 itself, matching the bin/brain-up
#   invocation, so the LaunchAgent plist doesn't have to know about it.

set -euo pipefail

VAULT="${BRAIN_VAULT_PATH:-$HOME/brain-vault}"
KEEP="${BRAIN_WIKI_KEEP_BUILDS:-3}"
# Resolve $BRAIN_HOME from this script's location ($BRAIN_HOME/.shims/ under a
# pipx install, $repo_root/bin/ in a dev checkout) so the pid file lands in a
# stable, non-reaped dir. Honor an explicit BRAIN_HOME override if set.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
BRAIN_HOME_RESOLVED="${BRAIN_HOME:-$( cd "$SCRIPT_DIR/.." && pwd )}"
# Pid path defaults to $BRAIN_HOME/run/ — NOT /tmp. macOS's tmp_cleaner reaps
# files left untouched for 3 days; this daemon writes the pid once at launch
# then runs for weeks, so a /tmp pid file silently vanishes and brain-status
# misreports the live daemon as stopped. Overridable (unset = unchanged) so
# tests run this wrapper hermetically against a tmp pid dir.
BUILD_PID="${BRAIN_BUILD_PID:-$BRAIN_HOME_RESOLVED/run/brain-build.pid}"

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

export BRAIN_WIKI_RELOAD=1

mkdir -p "$(dirname "$BUILD_PID")"
echo "$$" >"$BUILD_PID"
exec "$PY" -m brain.wiki.build_watcher --vault "$VAULT" --keep "$KEEP"
