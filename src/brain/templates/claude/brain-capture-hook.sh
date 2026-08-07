#!/bin/sh
# brain capture nudge — installed by `brain claude install-hooks`.
# Fails open: a missing `brain` or any crash must never block the session.
#
# `|| exit 0` rather than `exec`: exec would surface a Python traceback's exit
# code to Claude Code, and a non-zero Stop-hook exit is user-visible noise.
# Stdout is inherited, so the decision JSON still reaches Claude Code unmodified.
command -v brain >/dev/null 2>&1 || exit 0
brain claude capture-hook || exit 0
