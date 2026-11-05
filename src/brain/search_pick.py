"""`brain search --pick` (RM-52): choose one search result in fzf, print its id, record the click.

Measured against real ``fzf`` (0.29.0, 0.44.1, 0.72.0), which is what shapes this module:

- ``fzf`` draws on **stderr** and reads keys from ``/dev/tty``. A piped stdout is fine, so
  ``id=$(brain search --pick q)`` works, and those two things are what :func:`has_terminal` probes.
  Without them ``fzf`` can **hang** rather than fail, and :func:`run_fzf` has no timeout because a
  human is choosing, so the preflight is the only thing standing between a script and a hang.
- Stdout is matched against the ids offered (:func:`parse_selection`) and never taken on trust.

Titles and bodies are other people's text (email subjects, web pages), and they reach the terminal
by two paths. The list: titles go to ``fzf`` only over stdin, cleaned by :func:`result_lines`, and
never in an argv or a shell string. The preview: ``brain show`` prints a document raw and ``fzf``
passes some sequences straight to the terminal, so :func:`_preview_command` pipes it through
:mod:`brain.preview_filter`. ``fzf`` quotes field 1, a UUID from the database. There is no
``--query``, because ``brain show --query`` would write an ``opened`` row per cursor move.

The child's environment is fixed (:func:`child_env`). Every ``FZF_*`` variable is cleared:
``FZF_DEFAULT_OPTS`` can pick with no human (``--select-1``, ``--filter``), run commands
(``--bind=...:execute``), or serve the list of titles over HTTP (``--listen``), and the user's theme
is not worth any of those here. ``SHELL`` is ``/bin/sh``, because ``fzf`` runs the preview with
``$SHELL -c`` and the pipe and ``2>&1`` need a POSIX shell, which fish, csh and nu are not all.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import NoReturn, Protocol

import psycopg
import typer

from .db import connect
from .errors import InteractionError, describe_failure
from .ingest.text_safety import one_line
from .interactions import record_interaction
from .preview_filter import strip_sequences
from .search import SearchResult

logger = logging.getLogger(__name__)

#: The lowest version run end to end, by hand on macOS. CI runs the real test on apt's build.
MIN_FZF_VERSION: tuple[int, int] = (0, 29)
NO_TERMINAL_MESSAGE = "--pick needs a terminal"
NO_FZF_MESSAGE = "--pick needs fzf on PATH; install it with brew install fzf or your OS's package"
OLD_FZF_MESSAGE = (
    f"--pick needs fzf {MIN_FZF_VERSION[0]}.{MIN_FZF_VERSION[1]} or newer; upgrade fzf"
)

_TITLE_MAX_CHARS = 200
_VERSION_PROBE_SECONDS = 5.0
_STOP_SECONDS = 2.0
_EXIT_ABORTED = 130
_EXIT_NO_MATCH = 1
_VERSION_RE = re.compile(r"^(\d{1,4})\.(\d{1,4})")

class _Tty(Protocol):
    def isatty(self) -> bool: ...


@dataclass(frozen=True)
class FzfRun:
    """What ``fzf`` did. ``returncode`` is ``None`` when it could not be started."""

    returncode: int | None
    stdout: str


@dataclass(frozen=True)
class PickOutcome:
    """The decision a run comes to: an id or nothing, an exit code, and any message."""

    document_id: str | None
    exit_code: int
    message: str | None = None


def child_env() -> dict[str, str]:
    """This environment with no ``FZF_*`` and ``SHELL=/bin/sh`` (the module docstring says why)."""
    kept = {name: value for name, value in os.environ.items() if not name.startswith("FZF_")}
    return {**kept, "SHELL": "/bin/sh"}


def _open_controlling_tty() -> None:
    os.close(os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC))


def has_terminal(
    *, stderr: _Tty | None = None, open_tty: Callable[[], None] = _open_controlling_tty
) -> bool:
    """True when ``fzf`` can draw (stderr is a terminal) and read keys (``/dev/tty`` opens)."""
    try:
        if not (stderr if stderr is not None else sys.stderr).isatty():
            return False
        open_tty()
    except (OSError, ValueError):
        return False
    return True


def installed_fzf_version(
    binary: str, *, timeout: float = _VERSION_PROBE_SECONDS
) -> tuple[int, int] | None:
    """``(major, minor)`` from ``fzf --version``; ``None`` when it cannot be read."""
    try:
        done = subprocess.run(
            [binary, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="ascii",
            errors="replace",
            env=child_env(),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.match(done.stdout)
    return (int(match[1]), int(match[2])) if match else None


def pick_refusal(
    *,
    terminal: Callable[[], bool] | None = None,
    which: Callable[[str], str | None] | None = None,
    version: Callable[[str], tuple[int, int] | None] | None = None,
) -> str | None:
    """The fixed message when ``--pick`` cannot run here, else ``None``.

    An unreadable version proceeds: a devel build should not be refused on a guess, and a truly
    unusable ``fzf`` still ends in the fixed "fzf failed" message.
    """
    if not (terminal or has_terminal)():
        return NO_TERMINAL_MESSAGE
    binary = (which or shutil.which)("fzf")
    if binary is None:
        return NO_FZF_MESSAGE
    found = (version or installed_fzf_version)(binary)
    if found is not None and found < MIN_FZF_VERSION:
        return OLD_FZF_MESSAGE
    return None


def refuse_unpickable(*, json_output: bool, facets: bool) -> None:
    """Stop before any work: exit 2 on competing output flags, exit 1 when it cannot run here."""
    if json_output or facets:
        raise typer.BadParameter("--pick cannot be combined with --json or --facets")
    message = pick_refusal()
    if message is not None:
        typer.echo(message, err=True)
        raise typer.Exit(code=1)


def _field(text: str) -> str:
    return one_line(strip_sequences(text))


def result_lines(results: Sequence[SearchResult]) -> str:
    """``<id>\\t<title>\\t<source>`` per result, cleaned so a title cannot add a field or a line."""
    lines = []
    for result in results:
        title = _field(result.title)[:_TITLE_MAX_CHARS] or "(untitled)"
        source = _field(result.source_kind or "manual") or "manual"
        lines.append(f"{result.document_id}\t{title}\t{source}\n")
    return "".join(lines)


def _preview_command() -> str:
    """``brain show``, cleaned. ``-I`` keeps a planted ``brain`` in the cwd from being imported.

    If the filter cannot run the pane is empty, because the pipe never lets raw output through.
    """
    return f"brain show {{1}} 2>&1 | {shlex.quote(sys.executable)} -I -m brain.preview_filter"


def fzf_argv(binary: str) -> list[str]:
    """The fixed argv: id hidden, one selection, preview by field index only."""
    return [
        binary,
        "--delimiter",
        "\t",
        "--with-nth",
        "2..",
        "--no-multi",
        "--preview",
        _preview_command(),
    ]


def _stop_gently(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM, so ``fzf`` can restore the terminal; SIGKILL only if it ignores that (sec-121 F2).

    A killed ``fzf`` leaves the alternate screen up, with a preview's body still on it.
    """
    proc.terminate()
    try:
        proc.wait(timeout=_STOP_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_fzf(argv: Sequence[str], stdin_text: str) -> FzfRun:
    """Run ``fzf`` with stderr inherited, because that is where it draws.

    Deliberately no timeout: a human is choosing, for as long as they like. :func:`has_terminal`
    is what makes that safe. The echo is one line (``--no-multi``, and no user options reach it).
    Ctrl-C at the keyboard is a key to ``fzf``; only a signal sent to this process lands here.
    """
    try:
        proc = subprocess.Popen(
            list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=child_env()
        )
    except OSError:
        return FzfRun(None, "")
    with proc:
        try:
            stdout, _ = proc.communicate(stdin_text.encode("utf-8", "replace"))
        except KeyboardInterrupt:
            _stop_gently(proc)
            return FzfRun(_EXIT_ABORTED, "")
    return FzfRun(proc.returncode, stdout.decode("utf-8", "replace"))


def parse_selection(stdout: str, offered: Collection[str]) -> str | None:
    """The id of the only non-empty line, if it was offered; anything else fails closed (``None``).

    Split on ``\\n`` alone: ``splitlines`` also splits on a vertical tab and its relatives.
    """
    lines = [line for line in stdout.split("\n") if line]
    if len(lines) != 1:
        return None
    candidate = lines[0].split("\t", 1)[0]
    return candidate if candidate in offered else None


def interpret_run(run: FzfRun, offered: Collection[str]) -> PickOutcome:
    """Map what ``fzf`` did to an id, an exit code and a fixed message."""
    if run.returncode is None:
        return PickOutcome(None, 1, "fzf could not start")
    if run.returncode == _EXIT_ABORTED:
        return PickOutcome(None, _EXIT_ABORTED)
    if run.returncode == _EXIT_NO_MATCH:
        return PickOutcome(None, 1, "nothing picked")
    if run.returncode != 0:
        return PickOutcome(None, 1, f"fzf failed (exit {run.returncode})")
    document_id = parse_selection(run.stdout, offered)
    if document_id is None:
        return PickOutcome(None, 1, "fzf returned a line that was not offered")
    return PickOutcome(document_id, 0)


def _record_click(
    database_url: str,
    *,
    document_id: str,
    query: str,
    session_id: uuid.UUID,
    agent_id: str | None,
) -> None:
    """Best-effort ``clicked`` row on a new connection; the search's closed before ``fzf`` ran."""
    try:
        with connect(database_url) as conn:
            conn.autocommit = True
            record_interaction(
                conn,
                document_id=document_id,
                action="clicked",
                source="cli",
                query=query,
                session_id=session_id,
                agent_id=agent_id,
            )
    except (psycopg.Error, InteractionError) as exc:
        logger.warning("pick logging failed: %s", describe_failure(exc))


def run_pick(
    results: Sequence[SearchResult],
    *,
    query: str,
    session_id: uuid.UUID,
    database_url: str,
    agent_id: str | None,
) -> NoReturn:
    """Run the picker, print the chosen id, record the click, and exit.

    Called after the search's connection has closed, so none is held while a human chooses. The
    id is printed before the click is written, so a logging failure can never lose it.
    """
    if not results:
        typer.echo("(no results)", err=True)
        raise typer.Exit(code=1)
    run = run_fzf(fzf_argv(shutil.which("fzf") or "fzf"), result_lines(results))
    outcome = interpret_run(run, {result.document_id for result in results})
    if outcome.message is not None:
        typer.echo(outcome.message, err=True)
    if outcome.document_id is not None:
        typer.echo(outcome.document_id)
        _record_click(
            database_url,
            document_id=outcome.document_id,
            query=query,
            session_id=session_id,
            agent_id=agent_id,
        )
    raise typer.Exit(code=outcome.exit_code)
