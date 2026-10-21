#!/usr/bin/env bash
# scripts/smoke-linux.sh — T4.5 best-effort smoke test for Linux (Ubuntu 22.04).
#
# Run this inside a fresh Ubuntu 22.04 VM or Docker container AFTER you have:
#   - installed Docker (the daemon, not just the CLI — `docker info` must work),
#   - installed Python 3.11+ (apt + deadsnakes or pyenv),
#   - tagged + pushed v0.2.1 (or set BRAIN_INSTALL_REF / BRAIN_INSECURE=1
#     to test against a branch / commit).
#
# Important difference from smoke-macos.sh:
#   `brain-up` is NOT used. Linux has no launchd, and `brain-up`
#   unconditionally delegates daemon supervision to `brain-install-launchd`
#   (a macOS-only helper that calls `launchctl bootstrap`). Calling brain-up
#   on Linux would therefore fail at supervisor install. Instead this script
#   starts the vault-sync watcher itself with `nohup`, tracks its PID, and
#   kills it via an EXIT trap when it finishes. That mirrors the
#   pre-launchd-era manual bring-up flow exactly.
#
#   This is also why T4.5 is documented as "best-effort": background
#   supervision on Linux is not yet shipped (systemd-user unit is a v0.2.1+
#   follow-up). The smoke proves the install path works end-to-end; the
#   long-lived daemon story still requires the user to run the watcher
#   inside tmux / systemd-user themselves.
#
# Re-runnable: yes. The EXIT trap stops the daemons this run started; the
# install itself is preserved.  Full reset:
#   brain uninstall --yes --remove-db --remove-vault
#   pipx uninstall secondbrain-py
#
# Env overrides:
#   BRAIN_INSTALL_REF   (default: v0.2.1)
#   BRAIN_INSECURE      (default: 0)
#   BRAIN_REPO          (default: https://github.com/mshtawythug/second-brain.git)

set -euo pipefail

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
INSTALL_SH="${INSTALL_SH:-$REPO_ROOT/install.sh}"
VAULT="${BRAIN_VAULT_PATH:-$HOME/brain-vault}"
SMOKE_NOTE_BASENAME="smoke-$(date +%Y%m%d-%H%M%S).md"
SYNC_WAIT_SECONDS="${SYNC_WAIT_SECONDS:-90}"

PASSED=0
FAILED=0
FAILURES=()

# PID of the daemon we start; killed by the EXIT trap.
WATCH_PID=""

_pass() { printf '\033[32m[PASS]\033[0m %s\n' "$*"; PASSED=$((PASSED + 1)); }
_fail() {
    printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2
    FAILED=$((FAILED + 1))
    FAILURES+=("$*")
}
_info() { printf '\033[36m[..]\033[0m %s\n' "$*"; }
_section() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

_cleanup() {
    local pid
    for pid in "$WATCH_PID"; do
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            _info "Stopping daemon pid=$pid"
            kill "$pid" 2>/dev/null || true
        fi
    done
}
trap _cleanup EXIT

# ---------------------------------------------------------------------------
# Step 1 — preflight
# ---------------------------------------------------------------------------
_section "1. Preflight"

if [[ "$(uname -s)" != "Linux" ]]; then
    _fail "Not running on Linux (uname -s = $(uname -s)). Use smoke-macos.sh instead."
    exit 1
fi
_pass "OS is Linux ($(lsb_release -d 2>/dev/null | cut -f2- || cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | head -1))"

if ! command -v python3 >/dev/null 2>&1; then
    _fail "python3 not on PATH. Install Python 3.11+ via apt + deadsnakes or pyenv."
    exit 1
fi
PY_VER="$(python3 --version 2>&1)"
PY_MINOR="$(echo "$PY_VER" | sed 's/Python 3\.\([0-9]*\).*/\1/')"
if [[ -z "$PY_MINOR" ]] || (( PY_MINOR < 11 )); then
    _fail "Python 3.11+ required (found $PY_VER)"
    exit 1
fi
_pass "Python: $PY_VER"

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    _fail "Docker daemon not reachable. On a fresh Ubuntu VM: install docker.io + add yourself to the docker group."
    exit 1
fi
_pass "Docker daemon reachable"

FREE_KB="$(df -P "$HOME" | awk 'NR==2 {print $4}')"
FREE_GB=$(( FREE_KB / 1024 / 1024 ))
if (( FREE_GB < 5 )); then
    _fail "Less than 5GB free in \$HOME (have ${FREE_GB}GB)."
    exit 1
fi
_pass "Free disk: ${FREE_GB}GB"

# ---------------------------------------------------------------------------
# Step 2 — install.sh
# ---------------------------------------------------------------------------
_section "2. install.sh"

if command -v brain >/dev/null 2>&1; then
    _info "brain already on PATH at $(command -v brain) — skipping install.sh"
else
    if [[ ! -x "$INSTALL_SH" ]]; then
        _fail "install.sh not found or not executable at $INSTALL_SH"
        exit 1
    fi
    _info "Running $INSTALL_SH"
    if bash "$INSTALL_SH" --non-interactive --skip-skill; then
        _pass "install.sh + brain setup completed"
    else
        _fail "install.sh failed (exit $?)"
        exit 1
    fi
fi

_info "Re-running brain setup --non-interactive (idempotency check)"
if brain setup --non-interactive >/tmp/brain-setup-smoke.log 2>&1; then
    _pass "brain setup is idempotent on second run"
else
    _fail "brain setup failed on second run (see /tmp/brain-setup-smoke.log)"
fi

# ---------------------------------------------------------------------------
# Step 3 — brain doctor
# ---------------------------------------------------------------------------
_section "3. brain doctor"

DOCTOR_OUT="$(brain doctor 2>&1 || true)"
echo "$DOCTOR_OUT"
if echo "$DOCTOR_OUT" | grep -qE '\[(fail|error)\]'; then
    _fail "brain doctor reported failures"
else
    _pass "brain doctor: all checks green"
fi

# ---------------------------------------------------------------------------
# Step 4 — bring up the vault-sync watcher MANUALLY.
#
# Linux has no launchd; we cannot use `brain-up` (it execs
# brain-install-launchd, which only works on macOS). The pre-launchd
# `nohup ... &` shape below is exactly what brain-up did before the
# 2026-05-08 launchd handoff. (Caddy, the cold-start Quartz build and the
# build watcher this step used to start were deleted with the wiki at R3,
# wiki-to-ui phase 9.)
# ---------------------------------------------------------------------------
_section "4. Start the vault sync watcher (manual, no launchd)"

_info "Starting vault sync watcher in background"
nohup brain vault sync --watch --vault "$VAULT" \
        >/tmp/brain-watch-smoke.log 2>&1 &
WATCH_PID=$!
sleep 2
if kill -0 "$WATCH_PID" 2>/dev/null; then
    _pass "Vault sync watcher running (pid=$WATCH_PID)"
else
    _fail "Vault sync watcher died (see /tmp/brain-watch-smoke.log)"
    WATCH_PID=""
fi

# ---------------------------------------------------------------------------
# Step 5 — drop a markdown file, observe the watcher sync it into the brain
# ---------------------------------------------------------------------------
_section "5. Vault watcher sync"

if [[ ! -d "$VAULT" ]]; then
    _fail "Vault directory missing at $VAULT"
else
    _pass "Vault exists at $VAULT"

    SMOKE_TOKEN="smoketoken$(date +%s)"
    SMOKE_PATH="$VAULT/$SMOKE_NOTE_BASENAME"
    cat >"$SMOKE_PATH" <<EOF
---
title: Smoke note $SMOKE_TOKEN
content_type: note
kind: vault
---

# Smoke test

This note was generated by scripts/smoke-linux.sh at $(date -u +%Y-%m-%dT%H:%M:%SZ).
EOF
    _info "Wrote $SMOKE_PATH; waiting up to ${SYNC_WAIT_SECONDS}s for the watcher to sync it"

    DEADLINE=$(( $(date +%s) + SYNC_WAIT_SECONDS ))
    SYNCED=0
    while (( $(date +%s) < DEADLINE )); do
        sleep 2
        if brain search "$SMOKE_TOKEN" 2>/dev/null | grep -q "$SMOKE_TOKEN"; then
            SYNCED=1
            break
        fi
    done

    if (( SYNCED == 1 )); then
        _pass "Watcher synced the note within ${SYNC_WAIT_SECONDS}s of file drop"
    else
        _fail "Watcher did NOT sync the note within ${SYNC_WAIT_SECONDS}s (brain search found no match)"
    fi

    rm -f "$SMOKE_PATH"
fi

# ---------------------------------------------------------------------------
# Step 6 — Claude Code skill
# ---------------------------------------------------------------------------
_section "6. Claude Code skill (opt-in)"

SKILL_PATH="$HOME/.claude/skills/brain/SKILL.md"
if [[ -f "$SKILL_PATH" ]]; then
    _pass "Skill installed at $SKILL_PATH"
else
    _info "Skill not installed (we ran with --skip-skill). To install:"
    _info "  brain claude install-skill"
fi

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
_section "Summary"
printf 'Passed: %d\nFailed: %d\n' "$PASSED" "$FAILED"
if (( FAILED > 0 )); then
    printf '\nFailures:\n'
    for f in "${FAILURES[@]}"; do printf '  - %s\n' "$f"; done
    printf '\n\033[31mSMOKE FAILED\033[0m\n'
    exit 1
fi

cat <<'EOF'

[32mSMOKE PASSED[0m

Manual step that cannot be automated:
  Open a fresh Claude Code conversation in this Linux user account.
  Ask: "do I have anything about <topic you ingested>"
  Confirm Claude invokes `brain search` via the skill.
  (If you skipped the skill above: run `brain claude install-skill` first.)

Known Linux gaps:
  - No launchd → no auto-supervision. This script started the vault-sync
    watcher in background and will tear it down on exit. For a real
    long-lived install on Linux, run `brain vault sync --watch` inside a
    tmux pane or a systemd-user unit. A first-class systemd-user template
    is a post-v0.2.0 follow-up.

To clean up the install entirely:
  brain uninstall --yes --remove-db --remove-vault
  pipx uninstall secondbrain-py
EOF
