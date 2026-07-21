# proactivity.tape — VHS script for the cli-reference "proactive half" GIF.
#
# Records two of the REGULAR `brain` CLI's proactive/assistant commands
# end-to-end — the daily digest (`brain brief`) and spaced-repetition
# resurfacing (`brain resurface`) — against a THROWAWAY, fully-isolated Postgres
# (compose project `brain-proactivity-gif`, port 55445, named volume, database
# `second_brain_proactivity_gif`). NEVER the prod stack (55432), the demo
# sandbox (55433), the test DB (5434), or the other GIF sandboxes (55440 /
# 55442). Every seeded doc is synthetic (Larkspur compliance corpus; zero
# personal data).
#
# Both recorded commands are pure-DB reads (no on-camera LLM inference), so their
# frames render instantly and deterministically. `brain brief` runs with
# `--no-enrich` for exactly that reason (mirrors bin/brain-usage-gif keeping
# unpredictable Ollama latency out of the fixed vhs Sleeps).
#
# This tape is NOT safe to run on its own: it expects `bin/brain-proactivity-gif`
# to have provisioned the sandbox, seeded the corpus, and pointed the CLI at it
# via the environment (DATABASE_URL / BRAIN_HOME / BRAIN_VAULT_PATH, inherited by
# vhs). The hidden setup below HARD-ABORTS unless DATABASE_URL is the :55445
# sandbox, so a stray `vhs docs/assets/proactivity.tape` can never touch a real
# database.
#
# Regenerate with:  bin/brain-proactivity-gif

Output docs/assets/proactivity.gif

# --- Recording settings (all Set directives must precede any command) -------
Set Shell bash
Set FontSize 16
Set Width 1200
Set Height 720
Set Padding 16
Set Margin 0
Set TypingSpeed 45ms
Set Theme "Catppuccin Mocha"
Set WindowBar Colorful

# --- Hidden setup: not part of the GIF -------------------------------------
# Put Docker + the CLI on PATH, FAIL CLOSED unless the CLI is pointed at the
# throwaway :55445 sandbox, move into the neutral work dir, set a clean prompt
# so the recording never leaks the real username/hostname, and clear the setup
# buffer. The settling Sleeps are load-bearing (mirrors usage.tape): the prompt
# redraw after the PS1 export and the `clear` must land while frames are still
# HIDDEN, or the first recorded frames leak the setup buffer / a doubled-letter
# prompt race.
Hide
Type "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH" Enter
Type 'case "${DATABASE_URL:-}" in *:55445/second_brain_proactivity_gif*) : ;; *) echo "ABORT: run via bin/brain-proactivity-gif (sandbox not active)"; exit 1 ;; esac' Enter
Type 'cd "${PROACTIVITY_GIF_WORK:?run via bin/brain-proactivity-gif}"' Enter
Type 'export PS1="\[\033[1;35m\]brain\[\033[0m\] \[\033[38;5;245m\]❯\[\033[0m\] "' Enter
Sleep 500ms
Type "clear" Enter
Sleep 1200ms
Show

# --- Scene 1: the daily digest (recent captures, open todos, pins) ----------
Sleep 500ms
Type "# brain's proactive half tells YOU what needs attention — no query needed." Sleep 400ms Enter
Sleep 700ms
Type "# 1. brain brief — today's digest: what landed, what's open, what you pinned." Sleep 400ms Enter
Sleep 700ms
Type "brain brief --no-enrich" Sleep 300ms Enter
Sleep 4s

# --- Scene 2: spaced-repetition resurfacing (older notes, scored + ranked) --
Type "clear" Enter
Sleep 500ms
Type "# 2. brain resurface — older notes you haven't revisited, scored + ranked." Sleep 400ms Enter
Sleep 700ms
Type "brain resurface" Sleep 300ms Enter
Sleep 4500ms
