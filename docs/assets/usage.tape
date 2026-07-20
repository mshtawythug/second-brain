# usage.tape — VHS script for the README "daily workflow" GIF.
#
# Records the REGULAR `brain` CLI end-to-end — ingest a note, search it back,
# read the top hit, check the corpus — against a THROWAWAY, fully-isolated
# Postgres (compose project `brain-usage-gif`, port 55440, named volume,
# database `second_brain_usage_gif`). NEVER the prod stack (55432), the demo
# sandbox (55433), or the test DB (5434). Every seeded doc is synthetic
# (Larkspur compliance corpus; zero personal data).
#
# This tape is NOT safe to run on its own: it expects `bin/brain-usage-gif` to
# have provisioned the sandbox and pointed the CLI at it via the environment
# (DATABASE_URL / BRAIN_HOME / BRAIN_VAULT_PATH, inherited by vhs). The hidden
# setup below HARD-ABORTS unless DATABASE_URL is the :55440 sandbox, so a stray
# `vhs docs/assets/usage.tape` can never touch a real database.
#
# Regenerate with:  bin/brain-usage-gif

Output docs/assets/usage.gif

# --- Recording settings (all Set directives must precede any command) -------
Set Shell bash
Set FontSize 16
Set Width 1200
Set Height 700
Set Padding 16
Set Margin 0
Set TypingSpeed 50ms
Set Theme "Catppuccin Mocha"
Set WindowBar Colorful

# --- Hidden setup: not part of the GIF -------------------------------------
# Put Docker + the CLI on PATH, FAIL CLOSED unless the CLI is pointed at the
# throwaway :55440 sandbox, move into the seeded notes dir, set a clean prompt
# so the recording never leaks the real username/hostname, and clear the setup
# buffer. The settling Sleeps are load-bearing (mirrors demo.tape): the prompt
# redraw after the PS1 export and the `clear` must land while frames are still
# HIDDEN, or the first recorded frames leak the setup buffer / a doubled-letter
# prompt race.
Hide
Type "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH" Enter
Type 'case "${DATABASE_URL:-}" in *:55440/second_brain_usage_gif*) : ;; *) echo "ABORT: run via bin/brain-usage-gif (sandbox not active)"; exit 1 ;; esac' Enter
Type 'cd "${USAGE_GIF_NOTES_DIR:?run via bin/brain-usage-gif}"' Enter
Type 'export PS1="\[\033[1;35m\]brain\[\033[0m\] \[\033[38;5;245m\]❯\[\033[0m\] "' Enter
Sleep 500ms
Type "clear" Enter
Sleep 1200ms
Show

# --- Scene 1: capture today's note (real ingest: chunk + embed + index) -----
Sleep 500ms
Type "# A day with brain: capture a note, search it back, read it, check the corpus." Sleep 400ms Enter
Sleep 700ms
Type "brain ingest weekly-standup.md --tag compliance" Sleep 300ms Enter
Sleep 4s

# --- Scene 2: hybrid search — the note you just added ranks top -------------
Type 'brain search "evidence automation"' Sleep 300ms Enter
Sleep 3s

# --- Scene 3: read the full top hit (auto-summary + body) -------------------
Type 'brain show $(brain search "evidence automation" --limit 1 --json | jq -r ".[0].id")' Sleep 300ms Enter
Sleep 3s

# --- Scene 4: the corpus at a glance ---------------------------------------
Type "brain status" Sleep 300ms Enter
Sleep 2500ms
