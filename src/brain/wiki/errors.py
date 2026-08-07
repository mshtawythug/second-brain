"""Wiki-pipeline exceptions."""
from brain.errors import BrainError


class BrainWikiError(BrainError):
    """Base class for wiki-pipeline failures (build, swap, watch)."""


class BrainWikiBuildError(BrainWikiError):
    """The Quartz build subprocess exited non-zero, timed out, or the workspace is broken."""


class BrainWikiConfigError(BrainWikiError):
    """``Config.load`` failed, so the build has no DB to refresh its surfaces from.

    Deliberately a *separate* class from :class:`BrainWikiBuildError`, and
    mapped to a *separate* process exit code by
    :func:`brain.wiki.build_swap.main` (``3`` vs ``1``). The two failures
    call for different repairs and must not be collapsed:

    * :class:`BrainWikiBuildError` — the Quartz build itself broke. Often
      transient (a bad note, a timeout); retrying can fix it.
    * :class:`BrainWikiConfigError` — the *deployment* is misconfigured
      (e.g. ``DATABASE_URL`` unset for the user running the build). It can
      never heal on its own; every subsequent scheduled build fails the
      same way until a human sets the variable.

    Note what this is NOT: a DB that is reachable but holds **no documents**
    is a legitimate, fully-supported state (a fresh brain). That produces an
    empty refresh, not this error — see the best-effort branch in
    :func:`brain.wiki.build_swap._refresh_pre_build_adornments`.
    """
