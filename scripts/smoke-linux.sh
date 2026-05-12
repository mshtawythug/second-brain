#!/usr/bin/env bash
# scripts/smoke-linux.sh — T4.5 best-effort smoke test for Linux (Ubuntu 22.04).
#
# Run this inside a fresh Ubuntu 22.04 VM or Docker container AFTER you have:
#   - installed Docker (the daemon, not just the CLI — `docker info` must work),
#   - installed Python 3.11+ (apt + deadsnakes or pyenv), AND
#   - tagged + pushed v0.2.0 (or set BRAIN_INSTALL_REF / BRAIN_INSECURE=1
#     to test against a branch / commit).
#
# Differences from smoke-macos.sh:
#   - No launchd. The watcher + build daemon are started via `brain-up` in
#     a background nohup so the rebuild check can verify them.
#   - The script tears down `brain-down` at the end so re-runs are clean.
#   - The Claude Code skill step is identical — the skill is OS-agnostic.
#
# Re-runnable: yes. Cleanup at end stops the daemons but keeps the install.
# Full reset: `brain uninstall --yes --remove-db --remove-vault && pipx uninstall second-brain`.
#
# Env overrides:
#   BRAIN_INSTALL_REF   (default: v0.2.0)
#   BRAIN_INSECURE      (default: 0)
#   BRAIN_REPO          (default: https://github.com/mshtawythug/second-brain.git)

set -euo pipefail

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
INSTALL_SH="${INSTALL_SH:-$REPO_ROOT/install.sh}"
VAULT="${BRAIN_VAULT_PATH:-$HOME/brain-vault}"
WIKI_URL="${WIKI_URL:-http://localhost:8080}"
SMOKE_NOTE_BASENAME="smoke-$(date +%Y%m%d-%H%M%S).md"
REBUILD_WAIT_SECONDS="${REBUILD_WAIT_SECONDS:-90}"

PASSED=0
FAILED=0
FAILURES=()
TEARDOWN_DAEMONS=0

_pass() { printf '\033[32m[PASS]\033[0m %s\n' "$*"; PASSED=$((PASSED + 1)); }
_fail() {
    printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2
    FAILED=$((FAILED + 1))
    FAILURES+=("$*")
}
_info() { printf '\033[36m[..]\033[0m %s\n' "$*"; }
_section() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

_cleanup() {
    if (( TEARDOWN_DAEMONS == 1 )); then
        _info "Tearing down brain daemons (brain-down)"
        brain-down >/dev/null 2>&1 || true
    fi
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
# Step 4 — start daemons via brain-up (no launchd on Linux)
# ---------------------------------------------------------------------------
_section "4. Start daemons (brain-up — Linux has no launchd)"

if command -v brain-up >/dev/null 2>&1; then
    _info "Running brain-up to start watcher + build daemons"
    if brain-up >/tmp/brain-up-smoke.log 2>&1; then
        TEARDOWN_DAEMONS=1
        _pass "brain-up completed (see /tmp/brain-up-smoke.log)"
    else
        _fail "brain-up failed (see /tmp/brain-up-smoke.log)"
    fi
else
    _fail "brain-up not on PATH (expected via pipx shim dir)"
fi

_info "Waiting 5s for daemons to settle…"
sleep 5

if command -v brain-status >/dev/null 2>&1; then
    STATUS_OUT="$(brain-status 2>&1 || true)"
    echo "$STATUS_OUT"
    if echo "$STATUS_OUT" | grep -q 'running'; then
        _pass "brain-status reports at least one daemon running"
    else
        _fail "brain-status shows no daemons running"
    fi
fi

# ---------------------------------------------------------------------------
# Step 5 — wiki HTTP probe
# ---------------------------------------------------------------------------
_section "5. Wiki at $WIKI_URL"

HTTP_CODE="$(curl -s -o /tmp/brain-smoke-wiki.html -w '%{http_code}' "$WIKI_URL" || echo 000)"
if [[ "$HTTP_CODE" == "200" ]]; then
    _pass "Wiki returned HTTP 200"
else
    _fail "Wiki returned HTTP $HTTP_CODE (expected 200)"
fi
if grep -qiE 'quartz|brain' /tmp/brain-smoke-wiki.html 2>/dev/null; then
    _pass "Wiki HTML contains expected markers"
else
    _fail "Wiki HTML missing 'quartz' or 'brain' markers (see /tmp/brain-smoke-wiki.html)"
fi

# ---------------------------------------------------------------------------
# Step 6 — drop a markdown file, observe rebuild
# ---------------------------------------------------------------------------
_section "6. Vault watcher + rebuild"

if [[ ! -d "$VAULT" ]]; then
    _fail "Vault directory missing at $VAULT"
else
    _pass "Vault exists at $VAULT"

    BUILD_ID_BEFORE="$(curl -fsS "$WIKI_URL/.build-id" 2>/dev/null || echo none)"
    _info "Build id before: $BUILD_ID_BEFORE"

    SMOKE_PATH="$VAULT/$SMOKE_NOTE_BASENAME"
    cat >"$SMOKE_PATH" <<EOF
---
title: Smoke note $SMOKE_NOTE_BASENAME
content_type: note
kind: vault
---

# Smoke test

This note was generated by scripts/smoke-linux.sh at $(date -u +%Y-%m-%dT%H:%M:%SZ).
EOF
    _info "Wrote $SMOKE_PATH; waiting up to ${REBUILD_WAIT_SECONDS}s for rebuild"

    DEADLINE=$(( $(date +%s) + REBUILD_WAIT_SECONDS ))
    REBUILT=0
    while (( $(date +%s) < DEADLINE )); do
        sleep 2
        BUILD_ID_NOW="$(curl -fsS "$WIKI_URL/.build-id" 2>/dev/null || echo none)"
        if [[ "$BUILD_ID_NOW" != "$BUILD_ID_BEFORE" && "$BUILD_ID_NOW" != "none" ]]; then
            REBUILT=1
            _info "Build id after: $BUILD_ID_NOW"
            break
        fi
    done

    if (( REBUILT == 1 )); then
        _pass "Wiki rebuilt within ${REBUILD_WAIT_SECONDS}s of file drop"
    else
        _fail "Wiki did NOT rebuild within ${REBUILD_WAIT_SECONDS}s (build id unchanged)"
    fi

    rm -f "$SMOKE_PATH"
fi

# ---------------------------------------------------------------------------
# Step 7 — Claude Code skill
# ---------------------------------------------------------------------------
_section "7. Claude Code skill (opt-in)"

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

Known Linux gaps (document in plan if any reproduce):
  - No launchd supervision — the script started daemons via brain-up.
    For a long-lived install, run brain-up inside a tmux/systemd-user
    unit, or contribute a systemd-user template (post-v0.2.0 follow-up).
  - Caddy must be installed manually (apt install caddy or the official repo).
    brain setup refuses with remediation if Caddy is missing.

To clean up the install entirely:
  brain uninstall --yes --remove-db --remove-vault
  pipx uninstall second-brain
EOF
