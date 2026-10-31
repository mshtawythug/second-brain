"""The ``git`` reader for `brain ingest-code` (RM-43): which tracked files the owner touched.

Path (c) of the new-source protocol, local files, with ``git`` only to decide
WHICH files: :mod:`.code_files` reads them and the CLI writes them. Every call
goes through an injectable :data:`GitRunner`; the production one,
:func:`git_runner`, runs ``git`` by its absolute path under
:func:`brain.ingest.capped_process.run_capped` (a timeout, a stdout cap, no
stdin, stderr discarded unread, the process group killed on the way out).

**Hardening (sec-113).** The scanned repository is untrusted: its
``.git/config`` (and every file it includes) is honoured by every call, and
it can name commands for git to run. What is defended, and how:

- *Config sources.* System config is off (``GIT_CONFIG_NOSYSTEM``) and global
  config is off (``GIT_CONFIG_GLOBAL=/dev/null``, which also covers
  ``$XDG_CONFIG_HOME/git/config`` and ``~/.config/git/config``). ``HOME`` is
  inherited only so the default global ignore file, ``~/.config/git/ignore``,
  still applies; a custom ``core.excludesFile`` in the owner's global config
  does not. Repository config is the only file source left, and every
  ``-c`` in :data:`_SAFE_CONFIG` outranks it, includes included.
- *Transports* (``remote.*.uploadpack``, ``core.sshCommand``, ``remote.*.vcs``,
  ``credential.helper``, ``core.askPass``, ``core.gitProxy``): reached only by a
  fetch, and the one fetch our reads could start is a partial clone's lazy
  fetch of a missing object. A partial clone is refused outright
  (:func:`find_work_tree`), lazy fetch is off (``GIT_NO_LAZY_FETCH``), and
  every transport is off (``protocol.allow=never``); ``credential.helper`` is
  cleared as well.
- *Forced off per call:* ``core.fsmonitor`` (``ls-files`` would run it),
  ``core.hooksPath`` (none of our commands runs a hook; set anyway),
  signature display (``gpg.program``), the pager (``--no-pager``), and, on
  ``log``, external diff and textconv (``--no-ext-diff``, ``--no-textconv``;
  ``--name-only`` produces no patch, so neither would be reached).
- *Not reachable from our commands:* filter drivers (``rev-parse``,
  ``ls-files --cached`` and ``log`` never read the work tree through
  ``.gitattributes``), ``core.editor``, aliases (a built-in cannot be aliased),
  submodules (no recursion), and ``trace2.*`` (read only from system and global
  config, both off).
- *Where the files are.* ``core.worktree`` can point the reported top level at
  any directory, so the directory the owner named must lie inside it
  (:func:`find_work_tree`), and :mod:`.code_files` reads only files that resolve
  inside that directory.

Paths are read NUL-separated with ``core.quotePath`` off, so any byte in a
name survives. No :class:`CodeRepoError` message quotes git's output; each
names only the subcommand and an exit code, a timeout or an exception class.

**Which files.** ``tracked`` is ``ls-files --cached`` minus the tracked files
that ``.gitignore`` matches (a force-added ignored file is still ignored, per
the ticket's "anything git ignores"); untracked files are never listed.
``touched`` is every path in a commit on ``HEAD`` authored by one of the given
addresses since local midnight of ``since``. Each address is matched as the
whole ``<address>`` with ``--fixed-strings``, case-insensitively, so
``ann@x.test`` never matches ``joann@x.test``. The midnight is explicit
because git reads a bare date as that day at the current time of day.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..errors import BrainError
from .capped_process import run_capped

#: One ``git`` call: arguments after ``git`` in, stdout out; raises :class:`CodeRepoError`.
GitRunner = Callable[[list[str]], bytes]

#: Per call. A ``log --name-only`` bounded by ``--since`` on a large repository fits.
GIT_TIMEOUT_SECONDS = 120
#: The largest answer read; ``git`` is killed past it.
GIT_MAX_STDOUT_BYTES = 64 * 1024 * 1024

#: Forced on every call; command-line config outranks every file (module docstring).
_SAFE_CONFIG = (
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.quotePath=false",
    "-c", "log.showSignature=false",
    "-c", "protocol.allow=never",
    "-c", "credential.helper=",
)
#: Config names that make a repository a partial clone, matched casefolded:
#: ``extensions.partialclone``, or a ``remote.<name>.`` key named here.
_PARTIAL_CLONE_KEY = "extensions.partialclone"
_PROMISOR_REMOTE_KEYS = frozenset({"promisor", "partialclonefilter"})
_INSTALL_HINT = "git not found on PATH: install it (xcode-select --install, or brew install git)"
_EMAIL = re.compile(r"[^\s<>@]+@[^\s<>@]+")


class CodeRepoError(BrainError):
    """A ``git`` call failed, or the directory is not a work tree. Quotes no git output."""


class GitNotFoundError(CodeRepoError):
    """``git`` is not on ``PATH``; the message is the install hint."""


@dataclass(frozen=True)
class WorkTree:
    """A work tree's resolved top level; ``prefix``, the POSIX path of the scanned
    directory under it (``""`` at the top, else ending in ``/``); and ``scope``,
    that directory resolved, the bound every file read must stay inside."""

    root: Path
    prefix: str
    scope: Path

    @property
    def name(self) -> str:
        """The top level's directory name, which prefixes every document title."""
        return self.root.name


def is_valid_author_email(email: str) -> bool:
    """One address, ``local@domain``: no whitespace, no ``<`` or ``>``, one ``@``."""
    return _EMAIL.fullmatch(email) is not None


def git_env(base_env: Mapping[str, str]) -> dict[str, str]:
    """The minimal environment ``git`` runs under (module docstring)."""
    env = {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
    }
    if base_env.get("HOME"):
        env["HOME"] = base_env["HOME"]
    return env


def git_runner(
    args: list[str],
    *,
    repo: Path,
    which: Callable[[str], str | None] | None = None,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    base_env: Mapping[str, str] | None = None,
    timeout: float = GIT_TIMEOUT_SECONDS,
    max_stdout_bytes: int = GIT_MAX_STDOUT_BYTES,
) -> bytes:
    """The production :data:`GitRunner`, bound to ``repo`` with ``functools.partial``.

    ``which``, ``popen`` and ``base_env`` are test seams; the first and last are
    resolved at call time (``shutil.which``, ``os.environ``).
    """
    if not args:
        raise CodeRepoError("git runner invoked with no arguments")
    binary = (which or shutil.which)("git")
    if binary is None:
        raise GitNotFoundError(_INSTALL_HINT)
    label = f"`git {args[0]}`"
    try:
        run = run_capped(
            [binary, "--no-pager", "-C", str(repo), *_SAFE_CONFIG, *args],
            timeout=timeout,
            max_stdout_bytes=max_stdout_bytes,
            env=git_env(os.environ if base_env is None else base_env),
            popen=popen,
        )
    except OSError as exc:
        raise CodeRepoError(f"{label} could not run ({type(exc).__name__})") from None
    if run.oversized:
        raise CodeRepoError(f"{label} output exceeded {max_stdout_bytes} bytes; narrow the scope")
    if run.timed_out:
        raise CodeRepoError(f"{label} timed out after {timeout:g}s")
    if run.returncode != 0:
        raise CodeRepoError(f"{label} failed (exit {run.returncode})")
    return run.stdout


def find_work_tree(directory: Path, *, runner: GitRunner) -> WorkTree:
    """The work tree ``directory`` is in; ``runner`` is bound to ``directory``.

    A directory outside any repository, inside ``.git``, or in a bare repository
    is refused: ``rev-parse --show-toplevel`` fails there. A missing ``git``
    keeps its :class:`GitNotFoundError` and install hint. So is a top level that
    does not contain ``directory`` (a ``core.worktree`` pointing elsewhere), and
    a partial clone (:func:`refuse_partial_clone`), both before any file is listed.
    """
    if not directory.is_dir():
        raise CodeRepoError(f"{directory.name or directory}: not a directory")
    try:
        out = runner(["rev-parse", "--show-toplevel", "--show-prefix"])
    except GitNotFoundError:
        raise  # the install hint, not "not a work tree"
    except CodeRepoError:
        raise CodeRepoError("not inside a git work tree") from None
    lines = os.fsdecode(out).split("\n")
    if len(lines) < 2 or not lines[0]:
        raise CodeRepoError("git rev-parse answered something unusable")
    root = Path(lines[0]).resolve()
    scope = directory.resolve()
    if not scope.is_relative_to(root):
        raise CodeRepoError(
            "the repository's work tree is configured outside this directory "
            "(core.worktree); refusing to read it"
        )
    refuse_partial_clone(runner)
    return WorkTree(root=root, prefix=lines[1], scope=scope)


def refuse_partial_clone(runner: GitRunner) -> None:
    """Refuse a partial clone: reading its history could fetch, and run a command, to get it.

    Reads the effective config's names only (``config --list --name-only``), so
    no value, and nothing git would run, is touched.
    """
    for raw in runner(["config", "--list", "--name-only", "-z"]).split(b"\0"):
        name = os.fsdecode(raw).casefold()
        section, _, key = name.rpartition(".")
        if name == _PARTIAL_CLONE_KEY or (
            section.startswith("remote.") and key in _PROMISOR_REMOTE_KEYS
        ):
            raise CodeRepoError(
                "a partial clone (it has a promisor remote) is not supported; "
                "use a full clone"
            )


def _paths(out: bytes) -> set[str]:
    return {os.fsdecode(raw) for raw in out.split(b"\0") if raw}


def tracked_files(runner: GitRunner) -> set[str]:
    """Tracked paths (repo-relative POSIX) that ``.gitignore`` rules do not match."""
    every = _paths(runner(["ls-files", "-z", "--cached", "--full-name"]))
    ignored = _paths(
        runner(["ls-files", "-z", "--cached", "--ignored", "--exclude-standard", "--full-name"])
    )
    return every - ignored


def owner_touched_files(runner: GitRunner, *, emails: Iterable[str], since: date) -> set[str]:
    """Every path a ``HEAD`` commit by one of ``emails`` touched since midnight of ``since``."""
    authors = [f"--author=<{email}>" for email in emails]
    if not authors:
        raise CodeRepoError("at least one author address is required")
    return _paths(
        runner(
            [
                "log", "-z", "--name-only", "--format=", "--no-renames",
                "--no-ext-diff", "--no-textconv",
                "--regexp-ignore-case", "--fixed-strings", *authors,
                f"--since={since.isoformat()} 00:00:00", "HEAD", "--",
            ]
        )
    )


def owned_files(
    tree: WorkTree, *, runner: GitRunner, emails: Iterable[str], since: date
) -> list[str]:
    """The tracked, not-ignored files under ``tree.prefix`` the owner touched, sorted.

    ``runner`` is bound to ``tree.root``. A repository with nothing tracked
    returns ``[]`` without reading the log, which an empty repository cannot answer.
    """
    tracked = {p for p in tracked_files(runner) if p.startswith(tree.prefix)}
    if not tracked:
        return []
    touched = owner_touched_files(runner, emails=emails, since=since)
    return sorted(tracked & touched)

