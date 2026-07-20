# graphrag.tape — VHS script for the docs/graphrag.md "entity-graph retrieval" GIF.
#
# Records the `brain graphrag …` CLI — "themes with a person" then a fused
# graph+vector search — against a THROWAWAY, fully-isolated Apache-AGE Postgres
# (compose project `brain-graphrag-gif`, port 55442, named volume, database
# `second_brain_graphrag_gif`). NEVER the prod stack (55432), the demo sandbox
# (55433), the test DB (5434), or the usage-gif sandbox (55440). Every seeded
# doc is synthetic (Larkspur compliance corpus; zero personal data).
#
# This tape is NOT safe to run on its own: it expects `bin/brain-graphrag-gif`
# to have provisioned the sandbox, seeded + built the graph, and pointed the CLI
# at it via the environment (DATABASE_URL / BRAIN_HOME / BRAIN_VAULT_PATH /
# BRAIN_GRAPH_*, inherited by vhs). The hidden setup below HARD-ABORTS unless
# DATABASE_URL is the :55442 sandbox, so a stray `vhs docs/assets/graphrag.tape`
# can never touch a real database.
#
# Regenerate with:  bin/brain-graphrag-gif

Output docs/assets/graphrag.gif

# --- Recording settings (all Set directives must precede any command) -------
Set Shell bash
Set FontSize 15
Set Width 1240
Set Height 940
Set Padding 16
Set Margin 0
Set TypingSpeed 45ms
Set Theme "Catppuccin Mocha"
Set WindowBar Colorful

# --- Hidden setup: not part of the GIF -------------------------------------
# Put Docker + the CLI on PATH, FAIL CLOSED unless the CLI is pointed at the
# throwaway :55442 sandbox, move into the sandbox workdir, set a clean prompt so
# the recording never leaks the real username/hostname, and clear the setup
# buffer. The settling Sleeps are load-bearing (mirrors usage.tape): the prompt
# redraw after the PS1 export and the `clear` must land while frames are still
# HIDDEN, or the first recorded frames leak the setup buffer / a doubled-letter
# prompt race.
Hide
Type "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH" Enter
Type 'case "${DATABASE_URL:-}" in *:55442/second_brain_graphrag_gif*) : ;; *) echo "ABORT: run via bin/brain-graphrag-gif (sandbox not active)"; exit 1 ;; esac' Enter
Type 'cd "${GRAPHRAG_GIF_WORK:?run via bin/brain-graphrag-gif}"' Enter
Type 'export PS1="\[\033[1;35m\]brain\[\033[0m\] \[\033[38;5;245m\]❯\[\033[0m\] "' Enter
Sleep 500ms
Type "clear" Enter
Sleep 1200ms
Show

# --- Intro -----------------------------------------------------------------
Sleep 500ms
Type "# GraphRAG: entity-graph retrieval — the people + topics that connect your corpus." Sleep 400ms Enter
Sleep 900ms

# --- Scene 1: themes with a person (the headline use case) ------------------
Type '# Themes in my conversations with a person — grouped by who + what co-occurs.' Sleep 300ms Enter
Sleep 400ms
Type 'brain graphrag themes --person "Sam Rivera" --limit 3' Sleep 300ms Enter
Sleep 6s

Type "clear" Enter
Sleep 500ms

# --- Scene 2: fused graph + vector search -----------------------------------
Type '# Fuse mode — RRF of the entity-graph leg with the vector/FTS hybrid leg.' Sleep 300ms Enter
Sleep 400ms
Type 'brain graphrag search "vendor risk" --mode fuse --limit 4' Sleep 300ms Enter
Sleep 6s
