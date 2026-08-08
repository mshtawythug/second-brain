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

from typer.core import TyperGroup

from .config import ConfigError


class BrainGroup(TyperGroup):
    """Root command group that renders :class:`ConfigError` like a usage error.

    Attached via ``typer.Typer(cls=BrainGroup)``. Only ``ConfigError`` is
    intercepted — every other exception keeps its existing behaviour, because
    a traceback is the right output for a genuine bug and suppressing those
    would trade one bad experience for a worse one.
    """

    # `ctx` is deliberately `Any` rather than `click.Context`. Typer changed
    # which Context class `TyperGroup.invoke` declares: releases before 0.26
    # use `click.Context`, 0.26+ a *vendored* `typer._click.core.Context` (a
    # module that does not even exist in the older package). Naming either one
    # here type-checks against that Typer and fails Liskov against the other,
    # so `Any` is the honest annotation, where a `# type: ignore` would be dead
    # weight on whichever version does not need it (`strict = true` flags
    # unused ignores).
    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except ConfigError as exc:
            # Deliberately only capture the message here and fail *below*,
            # outside the `except` block: that leaves `__context__` unset, so
            # the chained `ConfigError` traceback the user was seeing stays
            # suppressed without needing `from None`.
            message = str(exc)

        # `ctx.fail()`, NOT `raise click.UsageError(...)`.
        #
        # Typer 0.26 vendored the whole of Click into `typer._click`, so from
        # that release on `typer._click.exceptions.UsageError` and the stock
        # `click.UsageError` are two unrelated classes in disjoint hierarchies.
        # Typer's runner only recognises its own, so a hand-raised
        # `click.UsageError` stopped being a usage error to the framework
        # running it: exit 1 with a Rich traceback of *this file*, instead of
        # exit 2 with the boxed message — precisely the crash this module was
        # written to remove, silently restored on any machine whose resolver
        # picked a current Typer. `pyproject.toml` pinned only `typer>=0.13`,
        # so which behaviour a user got depended on the day they installed.
        #
        # `Context.fail()` is the framework's own "this is a usage error" entry
        # point and exists in every supported version, so it raises whichever
        # `UsageError` class the runner actually catches. Routing through it
        # keeps the two implementations from being able to disagree at all,
        # rather than us tracking Typer's internals by hand.
        ctx.fail(message)
