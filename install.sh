#!/usr/bin/env bash
# install.sh — one-liner installer for second-brain / brain CLI.
# Usage: curl -fsSL https://raw.githubusercontent.com/mshtawythug/second-brain/main/install.sh | bash
# Env overrides: BRAIN_INSTALL_REF  BRAIN_REPO  BRAIN_INSECURE  BRAIN_INSTALL_SH_DRY_RUN
set -euo pipefail

BRAIN_INSTALL_REF="${BRAIN_INSTALL_REF:-v0.2.0}"
BRAIN_REPO="${BRAIN_REPO:-https://github.com/mshtawythug/second-brain.git}"
BRAIN_INSECURE="${BRAIN_INSECURE:-0}"
DRY="${BRAIN_INSTALL_SH_DRY_RUN:-0}"

_ok()   { printf '[ok]   %s\n' "$*"; }
_fail() { printf '[fail] %s\n' "$*" >&2; exit 1; }

detect_os() {
  local os="${OSTYPE:-$(uname -s 2>/dev/null || true)}"
  case "$os" in
    cygwin*|msys*|mingw*|win32*|CYGWIN*|MINGW*|MSYS*)
      _fail "Windows is not supported. Use WSL2 + Ubuntu and re-run from inside WSL." ;;
    darwin*|Darwin) _ok "OS: macOS" ;;
    linux*|Linux)   _ok "OS: Linux (best-effort)" ;;
    *)              _ok "OS: ${os} (unknown — proceeding)" ;;
  esac
}

require_python311() {
  [[ "$DRY" == "1" ]] && { _ok "Python >=3.11 [dry-run]"; return; }
  local py; py="$(command -v python3 2>/dev/null || true)"
  [[ -z "$py" ]] && _fail "python3 not found. Install 3.11+ via pyenv (macOS) or deadsnakes PPA (Ubuntu)."
  local ver; ver="$("$py" --version 2>&1)"
  local minor; minor="$(echo "$ver" | sed 's/Python 3\.\([0-9]*\).*/\1/')"
  { [[ -z "$minor" ]] || (( minor < 11 )); } && \
    _fail "Python 3.11+ required (found: $ver). Install via pyenv or deadsnakes PPA."
  _ok "Python: $ver"
}

ensure_pipx() {
  [[ "$DRY" == "1" ]] && { _ok "pipx [dry-run]"; return; }
  if ! command -v pipx &>/dev/null; then
    if [[ "$(uname -s)" == "Darwin" ]] && command -v brew &>/dev/null; then
      brew install pipx
    elif command -v python3 &>/dev/null; then
      python3 -m pip install --user pipx
    else
      _fail "Cannot install pipx: brew and python3 -m pip both unavailable."
    fi
  fi
  pipx ensurepath --quiet 2>/dev/null || true
  _ok "pipx: $(pipx --version 2>/dev/null || echo found)"
}

install_brain() {
  [[ "$BRAIN_REPO" == *"<"* ]] && \
    _fail "BRAIN_REPO still contains a placeholder. Set BRAIN_REPO=https://github.com/<your-fork>/second-brain.git"
  [[ "$BRAIN_INSTALL_REF" != v* ]] && [[ "$BRAIN_INSECURE" != "1" ]] && \
    _fail "Refusing to install from non-tag ref '${BRAIN_INSTALL_REF}' (installs pin a release tag like v0.2.0 by default). To install an untagged ref such as master, opt in explicitly: BRAIN_INSTALL_REF=master BRAIN_INSECURE=1"
  [[ "$DRY" == "1" ]] && { _ok "brain install [dry-run] ref=${BRAIN_INSTALL_REF}"; return; }
  pipx install --pip-args "--no-cache-dir" "git+${BRAIN_REPO}@${BRAIN_INSTALL_REF}"
  _ok "brain installed from ${BRAIN_INSTALL_REF}"
}

exec_setup() {
  [[ "$DRY" == "1" ]] && { _ok "exec brain setup [dry-run]"; exit 0; }
  local brain_bin; brain_bin="$(pipx environment --value PIPX_BIN_DIR)/brain"
  [[ ! -x "$brain_bin" ]] && \
    _fail "brain not found at $brain_bin after install. Check pipx output above."
  exec "$brain_bin" setup "$@"
}

detect_os
require_python311
ensure_pipx
install_brain
exec_setup "$@"
