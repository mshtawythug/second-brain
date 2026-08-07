"""One place where a bad `BRAIN_*` environment variable becomes a clean error.

`Config.load()` raises :class:`~brain.errors.ConfigError` with a good message
("BRAIN_SECRET_GUARD must be one of warn/redact/reject/off (got 'block')"),
but nothing caught it, so the user got a Rich traceback and exit 1. A typo in
a *flag* printed a tidy boxed usage error and exited 2; a typo in an *env var*
printed a stack trace. Same class of mistake, two very different experiences.

**Why a Click group and not `except ConfigError` in each command.** Every
command calls `Config.load()` in its own body, so per-command handling would
mean ~50 identical blocks — and N call sites drifting apart is exactly how the
presentation diverged in the first place. Overriding `invoke()` on the root
group catches every command and every sub-app (`vault`, `note`, `graphrag`, …)
in one place that cannot disagree with itself, because sub-group invocation
nests inside the parent's.

Exit code is **2**, matching Typer's own convention for "you invoked this
wrong" and matching `--sensitivity`'s existing behaviour. A bad env var is a
usage error, not a runtime failure.
"""
from __future__ import annotations

from typing import Any

import click
from typer.core import TyperGroup

from .config import ConfigError


class BrainGroup(TyperGroup):
    """Root command group that renders :class:`ConfigError` like a usage error.

    Attached via ``typer.Typer(cls=BrainGroup)``. Only ``ConfigError`` is
    intercepted — every other exception keeps its existing behaviour, because
    a traceback is the right output for a genuine bug and suppressing those
    would trade one bad experience for a worse one.
    """

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except ConfigError as exc:
            # UsageError gives the same boxed presentation and exit code 2 as
            # a bad flag, so the two typo paths finally look alike. `from None`
            # suppresses the chained traceback the user was seeing.
            raise click.UsageError(str(exc), ctx=ctx) from None
