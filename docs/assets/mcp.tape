# mcp.tape — VHS script for the README "Claude integrations" GIF.
#
# Records the `claude` CLI answering a question BY CALLING the bundled brain-mcp
# MCP server: search the second brain, then answer citing the notes. Everything
# runs against a THROWAWAY, fully-isolated Postgres (compose project
# `brain-mcp-gif`, port 55441, named volume, database `second_brain_mcp_gif`).
# NEVER the prod stack (55432), the demo sandbox (55433), or the test DB (5434).
# Every seeded doc is synthetic (Larkspur compliance corpus; zero personal data).
#
# This tape is NOT safe to run on its own: it expects `bin/brain-mcp-gif` to have
# provisioned the sandbox, seeded it, generated the MCP config + `ask-brain`
# wrapper in $MCP_GIF_STAGE, and exported DATABASE_URL for it. The hidden setup
# below HARD-ABORTS unless DATABASE_URL is the :55441 sandbox, so a stray
# `vhs docs/assets/mcp.tape` can never touch a real database.
#
# Regenerate with:  bin/brain-mcp-gif

Output docs/assets/mcp.gif

# --- Recording settings (all Set directives must precede any command) -------
Set Shell bash
Set FontSize 16
Set Width 1200
Set Height 720
Set Padding 16
Set Margin 0
Set TypingSpeed 50ms
Set Theme "Catppuccin Mocha"
Set WindowBar Colorful

# --- Hidden setup: not part of the GIF -------------------------------------
# Put Docker + the CLI on PATH, FAIL CLOSED unless the shell is pointed at the
# throwaway :55441 sandbox, add the staged wrapper dir to PATH, move into it, set
# a clean prompt so the recording never leaks the real username/hostname, and
# clear the setup buffer. The settling Sleeps are load-bearing (mirrors
# usage.tape): the prompt redraw after the PS1 export and the `clear` must land
# while frames are still HIDDEN, or the first recorded frames leak the setup
# buffer / a doubled-letter prompt race.
Hide
Type "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH" Enter
Type 'case "${DATABASE_URL:-}" in *:55441/second_brain_mcp_gif*) : ;; *) echo "ABORT: run via bin/brain-mcp-gif (sandbox not active)"; exit 1 ;; esac' Enter
Type 'cd "${MCP_GIF_STAGE:?run via bin/brain-mcp-gif}"' Enter
Type 'export PATH="$MCP_GIF_STAGE:$PATH"' Enter
Type 'export PS1="\[\033[1;35m\]brain\[\033[0m\] \[\033[38;5;245m\]❯\[\033[0m\] "' Enter
Sleep 500ms
Type "clear" Enter
Sleep 1200ms
Show

# --- Scene: Claude answers from the brain via the brain-mcp MCP server -------
# `ask-brain` wraps a single print-mode turn:
#   claude -p "<q>" --mcp-config brain.json --allowedTools "mcp__brain__*"
# and pretty-prints the streamed tool call + answer. Its only tool is brain-mcp,
# so the answer can only come from the seeded notes.
Sleep 600ms
Type "# Ask Claude — its only tool is the brain-mcp server. It searches, then answers." Sleep 400ms Enter
Sleep 700ms
Type 'ask-brain "what did we decide about vendor risk?"' Sleep 300ms Enter
# Covers the live print-mode turn: a dim status line shows immediately, then the
# brain_search tool call streams in, then the cited answer. Sized to cover the
# worst-case latency (~18s) AND hold the finished answer on screen to read.
Sleep 25s
