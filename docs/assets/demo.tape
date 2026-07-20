# demo.tape — VHS script for the README hero GIF.
#
# Records the REAL `brain demo` flow end-to-end against the isolated demo
# Postgres sandbox (compose project `brain-demo`, port 55433, container
# `second-brain-demo-postgres` — NEVER the prod stack on 55432). No mocks: the
# ranked tables below are genuine hybrid-search output over the 22-doc synthetic
# Larkspur corpus (zero Ollama, zero personal data).
#
# Regenerate with:  bin/brain-demo-gif   (or: vhs docs/assets/demo.tape)

Output docs/assets/demo.gif

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
# Resolve the worktree's own `brain` (its venv), put Docker on PATH, set a
# clean prompt so the recording never leaks the real username/hostname, and
# start from a torn-down sandbox so the visible run is a genuine first-run.
#
# The settling Sleeps below are load-bearing: the prompt redraw after the PS1
# export and the `clear` must fully land while frames are still HIDDEN, or the
# first recorded frames leak the setup buffer / a doubled-letter prompt race.
Hide
Type "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH" Enter
Type "source .venv/bin/activate 2>/dev/null || true" Enter
Type 'export PS1="\[\033[1;35m\]brain-demo\[\033[0m\] \[\033[38;5;245m\]❯\[\033[0m\] "' Enter
Sleep 500ms
Type "docker image inspect pgvector/pgvector:pg16 >/dev/null 2>&1 || docker pull pgvector/pgvector:pg16" Enter
Type "brain demo teardown >/dev/null 2>&1 || true" Enter
Type "clear" Enter
Sleep 1500ms
Show

# --- Scene 1: spin up + seed + hero query ----------------------------------
Sleep 500ms
Type "# Zero-Ollama taste test: throwaway Postgres, 22 synthetic docs, real search." Sleep 400ms Enter
Sleep 600ms
Type "brain demo" Sleep 400ms Enter
Sleep 8s

# --- Scene 2: a targeted follow-up query -----------------------------------
Type "brain demo query 'PCI scope creep'" Sleep 300ms Enter
Sleep 3s

# --- Scene 3: the same query, narrowed by --source -------------------------
Type "brain demo query 'vendor risk' --source gmail" Sleep 300ms Enter
Sleep 3500ms

# --- Scene 4: tear the sandbox back down -----------------------------------
Type "brain demo teardown" Sleep 300ms Enter
Sleep 2500ms
