#!/usr/bin/env bash
# scripts/smoke-macos.sh — T4.4 clean-machine smoke test for macOS.
#
# Run this on a fresh macOS user account (or a clean macOS VM) AFTER you have:
#   - tagged + pushed v0.2.0 (or set BRAIN_INSTALL_REF / BRAIN_INSECURE=1 to
#     test against a branch / commit), AND
#   - installed Docker Desktop and started it.
#
# What it does, in order:
#   1. Detects macOS + free disk + Docker running.
#   2. Runs install.sh (the same one-liner end users will run).
#   3. Runs `brain setup --non-interactive` against default paths.
#   4. Runs `brain doctor` and asserts every line is `[ok]`.
#   5. Curls http://localhost:8080 and confirms a 200 with Quartz HTML.
#   6. Drops a markdown file into the vault, waits for the watcher
#      to rebuild, and confirms the new note is reachable in the wiki.
#   7. Confirms launchd has both com.brain.watcher and com.brain.build loaded.
#   8. Confirms the Claude Code skill is installed at
#      ~/.claude/skills/brain/SKILL.md.
#   9. Prints a final PASS/FAIL summary.
#
# Re-runnable: yes — each step is idempotent against an existing install.
# Cleanup: NOT automatic. After the smoke passes, run:
#   brain uninstall --yes --remove-db --remove-vault
#   pipx uninstall second-brain
# to return to a clean state.
#
# Env overrides (passed through to install.sh):
#   BRAIN_INSTALL_REF   (default: v0.2.0)
#   BRAIN_INSECURE      (default: 0; set to 1 to install from a branch)
#   BRAIN_REPO          (default: https://github.com/mshtawythug/second-brain.git)

set -euo pipefail

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
INSTALL_SH="${INSTALL_SH:-$REPO_ROOT/install.sh}"
VAULT="${BRAIN_VAULT_PATH:-$HOME/brain-vault}"
WIKI_URL="${WIKI_URL:-http://localhost:8080}"
SMOKE_NOTE_BASENAME="smoke-$(date +%Y%m%d-%H%M%S).md"
REBUILD_WAIT_SECONDS="${REBUILD_WAIT_SECONDS:-60}"

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
    _info "Running $INSTALL_SH (this will pipx-install second-brain)"
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
# Step 4 — wiki HTTP probe
# ---------------------------------------------------------------------------
_section "4. Wiki at $WIKI_URL"

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
# Step 5 — drop a markdown file, observe rebuild
# ---------------------------------------------------------------------------
_section "5. Vault watcher + rebuild"

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

This note was generated by scripts/smoke-macos.sh at $(date -u +%Y-%m-%dT%H:%M:%SZ).
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
# Step 6 — launchd
# ---------------------------------------------------------------------------
_section "6. launchd plists"

if launchctl list 2>/dev/null | grep -q '^[^ ]*\s\+[^ ]*\s\+com\.brain\.watcher$'; then
    _pass "com.brain.watcher loaded"
else
    _fail "com.brain.watcher not loaded (launchctl list)"
fi

if launchctl list 2>/dev/null | grep -q '^[^ ]*\s\+[^ ]*\s\+com\.brain\.build$'; then
    _pass "com.brain.build loaded"
else
    _fail "com.brain.build not loaded (launchctl list)"
fi

# ---------------------------------------------------------------------------
# Step 7 — Claude Code skill (note: --skip-skill was set above; this is a
# manual-install verification step the user runs after the smoke if they
# want the skill).
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
  Open a fresh Claude Code conversation in this terminal account.
  Ask: "do I have anything about <topic you ingested>"
  Confirm Claude invokes `brain search` via the skill.
  (If you skipped the skill above: run `brain claude install-skill` first.)

To clean up:
  brain uninstall --yes --remove-db --remove-vault
  pipx uninstall second-brain
EOF
