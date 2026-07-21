# ask.tape — VHS script for the CLI-reference `brain ask` GIF.
#
# Records the REGULAR `brain` CLI's ask flow — one `brain search` to show the
# corpus has material, then `brain ask "<question>" --explain` (the agentic
# plan → retrieve → reflect → synthesize loop that ends in a single CITED
# answer) — against a THROWAWAY, fully-isolated Postgres (compose project
# `brain-ask-gif`, port 55443, named volume, database `second_brain_ask_gif`).
# NEVER the prod stack (55432), the demo sandbox (55433), or the test DB (5434).
# Every seeded doc is synthetic (Larkspur compliance corpus; zero personal data).
#
# This tape is NOT safe to run on its own: it expects `bin/brain-ask-gif` to
# have provisioned the sandbox, seeded the corpus, pre-warmed Ollama, and pointed
# the CLI at it via the environment (DATABASE_URL / BRAIN_HOME / BRAIN_VAULT_PATH,
# inherited by vhs). The hidden setup below HARD-ABORTS unless DATABASE_URL is
# the :55443 sandbox, so a stray `vhs docs/assets/ask.tape` can never touch a
# real database. `brain ask` prints nothing until its LLM loop finishes, so the
# regenerator trims the long "thinking" freeze out of the raw recording with
# ffmpeg after the fact.
#
# Regenerate with:  bin/brain-ask-gif

Output docs/assets/ask.gif

# --- Recording settings (all Set directives must precede any command) -------
Set Shell bash
Set FontSize 16
Set Width 1200
Set Height 820
Set Padding 16
Set Margin 0
Set TypingSpeed 45ms
Set Theme "Catppuccin Mocha"
Set WindowBar Colorful

# --- Hidden setup: not part of the GIF -------------------------------------
# Put Docker + the CLI on PATH, FAIL CLOSED unless the CLI is pointed at the
# throwaway :55443 sandbox, move into a clean scratch dir, set a prompt that
# never leaks the real username/hostname, and clear the setup buffer. The
# settling Sleeps are load-bearing (mirrors usage.tape): the prompt redraw after
# the PS1 export and the `clear` must land while frames are still HIDDEN, or the
# first recorded frames leak the setup buffer / a doubled-letter prompt race.
Hide
Type "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH" Enter
Type 'case "${DATABASE_URL:-}" in *:55443/second_brain_ask_gif*) : ;; *) echo "ABORT: run via bin/brain-ask-gif (sandbox not active)"; exit 1 ;; esac' Enter
Type 'cd "${ASK_GIF_WORK_DIR:?run via bin/brain-ask-gif}"' Enter
Type 'export PS1="\[\033[1;35m\]brain\[\033[0m\] \[\033[38;5;245m\]❯\[\033[0m\] "' Enter
Sleep 500ms
Type "clear" Enter
Sleep 1200ms
Show

# --- Scene 1: the corpus has real material (hybrid search) ------------------
Sleep 500ms
Type "# brain ask: get a synthesized, CITED answer — not just a list of documents." Sleep 400ms Enter
Sleep 700ms
Type 'brain search "evidence automation" --limit 3' Sleep 300ms Enter
Sleep 3500ms

# Clear so the ask output (plan + answer + sources) renders from the top and is
# never pushed off-screen by the search table above it.
Type "clear" Sleep 200ms Enter
Sleep 600ms

# --- Scene 2: ask a question the corpus can genuinely answer -----------------
# --explain surfaces the sub-query plan + iteration count, then the cited answer.
# The long Sleep covers the plan/retrieve/reflect/synthesize LLM loop (~20s warm);
# bin/brain-ask-gif trims the resulting "thinking" freeze down in post.
Type 'brain ask "how are we reducing our audit evidence burden?" --explain' Sleep 300ms Enter
Sleep 30s
Sleep 2500ms
