"""RM-53: every example must name a real command with real flags, and be safe to paste.

These run against the real ``brain.cli.app``, so renaming a command or a flag fails here.
Nothing is invoked: the checks read the command tree only.
"""
from __future__ import annotations

import shlex
from typing import Any

import pytest
import typer.main

from brain.cli import app
from brain.examples import load_examples

pytestmark = pytest.mark.nodb

_SHELL_OPERATORS = frozenset({"|", "<", ">", ">>", "&&", ";"})

#: Every command an example may use, classified by hand: an unclassified one fails, so adding
#: an example means deciding which of these it is. Read-only here means it changes no document,
#: note or vault file; a search still logs its query.
_READ_ONLY = frozenset(
    {
        "ask",
        "doctor",
        "explain",
        "gaps",
        "graphrag communities list",
        "graphrag entities",
        "graphrag entity",
        "graphrag search",
        "graphrag stats",
        "graphrag themes",
        "list",
        "people",
        "resurface",
        "review list",
        "search",
        "show",
        "status",
        "timeline",
        "todo",
        "usage",
    }
)
#: These write. An example needs a preview flag, or a ``# Writes:`` comment directly above it.
#: ``brief`` is here because it checks saved watches and records their hits; ``review weekly``
#: because it writes a vault page unless ``--no-emit``.
_WRITES = frozenset(
    {
        "backup",
        "brief",
        "ingest",
        "ingest-calendar",
        "ingest-dir",
        "ingest-github",
        "ingest-gmail",
        "ingest-stdin",
        "reembed",
        "review scan",
        "review weekly",
        "vault sync",
    }
)
_PREVIEW_FLAGS = frozenset({"--dry-run", "--no-emit"})
#: Never in an example, on any command: each overrides a safety check, deletes, or sends something
#: off the machine (``brief --wiki`` delivers the digest when delivery is configured).
_BANNED_FLAGS = frozenset(
    {
        "--allow-remote",
        "--allow-secrets",
        "--apply",
        "--force",
        "--include-confidential",
        "--no-auth",
        "--prune",
        "--remove-db",
        "--remove-vault",
        "--reset",
        "--rotate",
        "--uninstall",
        "--wiki",
        "--yes",
    }
)
_GMAIL_SCOPE_FLAGS = frozenset({"--query", "--label", "--from", "--since", "--until"})


def _args(line: str) -> list[str]:
    """The tokens after ``brain``, up to the first shell operator."""
    tokens = shlex.split(line)
    assert tokens[0] == "brain"
    args: list[str] = []
    for token in tokens[1:]:
        if token in _SHELL_OPERATORS:
            break
        args.append(token)
    return args


def _flags(args: list[str]) -> list[str]:
    return [arg.split("=", 1)[0] for arg in args if arg.startswith("-")]


def _resolve(line: str) -> tuple[Any, list[str], list[str]] | str:
    """Walk the command tree: the command, its path and its arguments, or what is wrong."""
    command: Any = typer.main.get_command(app)
    args = _args(line)
    path: list[str] = []
    while hasattr(command, "commands") and args and not args[0].startswith("-"):
        if args[0] not in command.commands:
            return f"{' '.join([*path, args[0]])}: no such command"
        command = command.commands[args[0]]
        path.append(args[0])
        args = args[1:]
    if hasattr(command, "commands") and not command.invoke_without_command:
        return f"{' '.join(path)}: is a group and needs a subcommand"
    return command, path, args


def invocation_problem(line: str) -> str | None:
    """Why ``line`` would not run: a missing command, an unknown flag, a missing option."""
    resolved = _resolve(line)
    if isinstance(resolved, str):
        return resolved
    command, _path, args = resolved
    flags = set(_flags(args))
    known = {opt for param in command.params for opt in (*param.opts, *param.secondary_opts)}
    for flag in sorted(flags - known - {"--help"}):
        return f"{flag}: no such flag"
    for param in command.params:
        if param.param_type_name == "option" and param.required and not flags & set(param.opts):
            return f"{param.opts[0]}: required option missing"
    return None


def safety_problem(line: str, comment: str = "") -> str | None:
    """Why ``line`` is not safe to paste into a real brain, given the comment above it."""
    resolved = _resolve(line)
    if isinstance(resolved, str):
        return resolved
    _command, path, args = resolved
    flags = set(_flags(args))
    name = " ".join(path)
    if flags & _BANNED_FLAGS:
        return f"{sorted(flags & _BANNED_FLAGS)[0]}: banned in an example"
    if name == "ingest-gmail" and not flags & _GMAIL_SCOPE_FLAGS:
        return "ingest-gmail: needs a scope flag"
    if name in _READ_ONLY:
        return None
    if name not in _WRITES:
        return f"{name}: not classified as read-only or writing"
    if flags & _PREVIEW_FLAGS or comment.startswith("# Writes:"):
        return None
    return f"{name}: writes, so needs a preview flag or a '# Writes:' comment"


def _commented_invocations() -> list[tuple[str, str]]:
    """Each invocation in the real resource, with the comment line directly above it."""
    found: list[tuple[str, str]] = []
    for topic in load_examples():
        previous = ""
        for line in topic.lines:
            if line.startswith("brain "):
                found.append((line, previous))
            previous = line
    return found


@pytest.mark.parametrize(
    "line",
    [
        'brain search "x"',
        "brain graphrag communities build",
        "brain graphrag communities",
        'brain search "x" --json',
        'brain search "x" --limit=5',
        'brain gaps -n 5',
        'brain graphrag themes --person "Jane Doe"',
        "brain ingest-stdin --source slack --external-id t-1 --title T < thread.txt",
        "brain ingest-stdin --source slack --external-id t-1 --title T < --not-a-brain-flag",
        'brain search "x" --json | jq --nosuchflag',
    ],
)
def test_a_real_invocation_has_no_problem(line: str) -> None:
    assert invocation_problem(line) is None


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ("brain nosuchcommand", "no such command"),
        ("brain vault nosuchsub", "no such command"),
        ("brain graphrag communities nosuchsub", "no such command"),
        ("brain vault", "needs a subcommand"),
        ('brain search "x" --nosuchflag', "no such flag"),
        ('brain search "x" --nosuchflag=5', "no such flag"),
        ("brain graphrag themes", "required option missing"),
        ("brain ingest-stdin --source slack --external-id t-1", "required option missing"),
        ("brain ingest-stdin --nosuchflag x < thread.txt", "no such flag"),
    ],
)
def test_a_rotten_invocation_is_named(line: str, problem: str) -> None:
    found = invocation_problem(line)
    assert found is not None
    assert problem in found


@pytest.mark.parametrize(("line", "comment"), _commented_invocations())
def test_every_packaged_example_runs_and_is_safe_to_paste(line: str, comment: str) -> None:
    assert invocation_problem(line) is None
    assert safety_problem(line, comment) is None


@pytest.mark.parametrize(
    ("line", "comment"),
    [
        ('brain search "x"', ""),
        ("brain ingest-dir ./notes --dry-run", ""),
        ("brain ingest ./notes/plan.pdf", "# Writes: stores one file"),
        ("brain ingest-gmail --from someone@example.com --dry-run", ""),
        ("brain ingest-gmail --label receipts --dry-run", ""),
        ("brain review weekly --no-emit", ""),
        ("brain brief", "# Writes: records watch hits"),
    ],
)
def test_a_safe_example_passes_the_safety_check(line: str, comment: str) -> None:
    assert safety_problem(line, comment) is None


@pytest.mark.parametrize(
    ("line", "comment", "problem"),
    [
        ("brain rm 1a2b3c", "# Writes: deletes it", "not classified"),
        ("brain restore backup.tar.gz", "# Writes: replaces it", "not classified"),
        ("brain capture review", "", "not classified"),
        ("brain ingest ./notes/plan.pdf", "", "writes, so needs"),
        ("brain ingest ./notes/plan.pdf", "# Stores one file", "writes, so needs"),
        ("brain ingest ./notes/plan.pdf --force", "# Writes: it", "banned"),
        ("brain ingest-dir ./notes --sync --apply", "# Writes: it", "banned"),
        ('brain search "x" --yes', "", "banned"),
        ("brain brief", "", "writes, so needs"),
        ("brain brief --wiki", "# Writes: a page", "banned"),
        ("brain review weekly", "", "writes, so needs"),
        ("brain ingest-gmail --dry-run", "", "needs a scope flag"),
        ("brain ingest-gmail --tag x --max 5", "# Writes: everything", "needs a scope flag"),
    ],
)
def test_an_unsafe_example_is_refused(line: str, comment: str, problem: str) -> None:
    found = safety_problem(line, comment)
    assert found is not None
    assert problem in found


def test_the_gmail_scope_list_matches_what_the_command_requires() -> None:
    """The refusal text in ``cli_ingest`` names the flags; a new scope flag must land here too."""
    resolved = _resolve("brain ingest-gmail --from someone@example.com")
    assert not isinstance(resolved, str)
    known = {opt for param in resolved[0].params for opt in param.opts}
    assert known >= _GMAIL_SCOPE_FLAGS


def test_the_classified_commands_all_exist() -> None:
    """A stale classification would stop refusing anything, so each entry must still resolve."""
    for name in _READ_ONLY | _WRITES:
        assert not isinstance(_resolve(f"brain {name}"), str), name
