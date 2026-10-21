#!/usr/bin/env bash
# scripts/smoke-macos.sh — T4.4 clean-machine smoke test for macOS.
#
# Run this on a fresh macOS user account (or a clean macOS VM) AFTER you have:
#   - tagged + pushed v0.2.1 (or set BRAIN_INSTALL_REF / BRAIN_INSECURE=1 to
#     test against a branch / commit), AND
#   - installed Docker Desktop and started it.
#
# What it does, in order:
#   1. Detects macOS + free disk + Docker running.
#   2. Runs install.sh (the same one-liner end users will run).
#   3. Runs `brain setup --non-interactive` against default paths.
#   4. Runs `brain doctor` and asserts every line is `[ok]`.
#   5. Drops a markdown file into the vault, waits for the watcher to sync
#      it, and confirms `brain search` finds it.
#   6. Confirms launchd has com.brain.watcher and com.brain.brief loaded, and
#      the retired com.brain.build not loaded.
#   7. Confirms the Claude Code skill is installed at
#      ~/.claude/skills/brain/SKILL.md.
#   8. Prints a final PASS/FAIL summary.
# (The wiki HTTP probe and rebuild check were deleted with the Quartz wiki at
# R3, wiki-to-ui phase 9.)
#
# Re-runnable: yes — each step is idempotent against an existing install.
# Cleanup: NOT automatic. After the smoke passes, run:
#   brain uninstall --yes --remove-db --remove-vault
#   pipx uninstall secondbrain-py
# to return to a clean state.
#
# Env overrides (passed through to install.sh):
#   BRAIN_INSTALL_REF   (default: v0.2.1)
#   BRAIN_INSECURE      (default: 0; set to 1 to install from a branch)
#   BRAIN_REPO          (default: https://github.com/mshtawythug/second-brain.git)

set -euo pipefail

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
INSTALL_SH="${INSTALL_SH:-$REPO_ROOT/install.sh}"
VAULT="${BRAIN_VAULT_PATH:-$HOME/brain-vault}"
SMOKE_NOTE_BASENAME="smoke-$(date +%Y%m%d-%H%M%S).md"
SYNC_WAIT_SECONDS="${SYNC_WAIT_SECONDS:-60}"

PASSED=0
FAILED=0
FAILURES=()

_pass() { printf '\033[32m[PASS]\033[0m %s\n' "$*"; PASSED=$((PASSED + 1)); }
_fail() {
    printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2
    FAILED=$((FAILED + 1))
    FAILURES+=("$*")
}
_info() { printf '\033[36m[..]\033[0m %s\n' "$*"; }
_section() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
# Step 1 — preflight
# ---------------------------------------------------------------------------
_section "1. Preflight"

if [[ "$(uname -s)" != "Darwin" ]]; then
    _fail "Not running on macOS (uname -s = $(uname -s)). Use smoke-linux.sh instead."
    exit 1
fi
_pass "OS is macOS ($(sw_vers -productVersion 2>/dev/null || echo unknown))"

if ! command -v docker >/dev/null 2>&1; then
    _fail "docker CLI not on PATH. Install Docker Desktop and re-run."
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    _fail "Docker daemon is not running. Start Docker Desktop and re-run."
    exit 1
fi
_pass "Docker daemon reachable"

FREE_GB="$(df -g "$HOME" | awk 'NR==2 {print $4}')"
if (( FREE_GB < 5 )); then
    _fail "Less than 5GB free in \$HOME (have ${FREE_GB}GB). Postgres + embeddings need room."
    exit 1
fi
_pass "Free disk: ${FREE_GB}GB"

# ---------------------------------------------------------------------------
# Step 2 — run install.sh
# ---------------------------------------------------------------------------
_section "2. install.sh"

if command -v brain >/dev/null 2>&1; then
    _info "brain already on PATH at $(command -v brain) — skipping install.sh"
else
    if [[ ! -x "$INSTALL_SH" ]]; then
        _fail "install.sh not found or not executable at $INSTALL_SH"
        exit 1
    fi
    _info "Running $INSTALL_SH (this will pipx-install secondbrain-py)"
    if bash "$INSTALL_SH" --non-interactive --skip-skill; then
        _pass "install.sh + brain setup completed"
    else
        _fail "install.sh failed (exit $?)"
        exit 1
    fi
fi

# install.sh exec's into `brain setup`. If we got here, setup ran. Re-run it
# explicitly to be safe (idempotent + ensures non-interactive defaults).
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
# Step 4 — drop a markdown file, observe the watcher sync it into the brain.
# (The wiki HTTP probe and rebuild check that stood here were deleted with the
# Quartz wiki at R3, wiki-to-ui phase 9.)
# ---------------------------------------------------------------------------
_section "4. Vault watcher sync"

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

This note was generated by scripts/smoke-macos.sh at $(date -u +%Y-%m-%dT%H:%M:%SZ).
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
# Step 5 — launchd
# ---------------------------------------------------------------------------
_section "5. launchd plists"

if launchctl list 2>/dev/null | grep -q '^[^ ]*\s\+[^ ]*\s\+com\.brain\.watcher$'; then
    _pass "com.brain.watcher loaded"
else
    _fail "com.brain.watcher not loaded (launchctl list)"
fi

if launchctl list 2>/dev/null | grep -q '^[^ ]*\s\+[^ ]*\s\+com\.brain\.brief$'; then
    _pass "com.brain.brief loaded"
else
    _fail "com.brain.brief not loaded (launchctl list)"
fi

# com.brain.build was retired at R2 (phase 8) and its code deleted at R3
# (phase 9): it must NOT be loaded.
if launchctl list 2>/dev/null | grep -q '^[^ ]*\s\+[^ ]*\s\+com\.brain\.build$'; then
    _fail "retired com.brain.build is still loaded (run brain-install-launchd)"
else
    _pass "retired com.brain.build not loaded"
fi

# ---------------------------------------------------------------------------
# Step 6 — Claude Code skill (note: --skip-skill was set above; this is a
# manual-install verification step the user runs after the smoke if they
# want the skill).
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
  Open a fresh Claude Code conversation in this terminal account.
  Ask: "do I have anything about <topic you ingested>"
  Confirm Claude invokes `brain search` via the skill.
  (If you skipped the skill above: run `brain claude install-skill` first.)

To clean up:
  brain uninstall --yes --remove-db --remove-vault
  pipx uninstall secondbrain-py
EOF
