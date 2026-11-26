"""The ingest commands' refusals: one line and a deliberate exit code, never a traceback.

The 0.4.0 manual QA found the ingest surface leaking Rich tracebacks where a dependency was
down or an argument was bad (F1, F2, F3, F4). Each refusal lives here, so every ingest
command words it the same way, and so ``cli_ingest.py`` (over the 800-line ceiling)
carries only the calls.

Exit codes:

- **1, an environment failure.** Postgres unreachable, the embedder not answering, or a
  guard refusal (the secret guard, a bad ``--sensitivity``). Those last two moved here
  from ``cli_ingest.py`` verbatim.
- **2, a usage error.** ``--metadata`` that is not a JSON object, or a ``--date`` the
  pipeline cannot parse.

What is printed is content-free. An error's class goes through
:func:`brain.errors.describe_failure`, never its message: a psycopg message names the user
and database, and an Ollama message can quote a response body. The unreachable-database
line names no address at all, not even a host or port. Showing one safely meant copying
libpq's DSN parser, and four review rounds kept finding edge cases in that copy.
``brain doctor`` shows the target.
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import typer

from .db import connect_raw
from .errors import (
    BrainError,
    EmbedError,
    SecretGuardError,
    SensitivityError,
    describe_failure,
)
from .ingest.sent_at import parse_sent_at
from .ingest.text_safety import encodes_utf8, one_line

_DOCTOR_HINT = "Try: brain doctor"


def unreachable_line(exc: BaseException) -> str:
    """The one line a failed connect prints: fixed text and the error class, nothing else."""
    return (
        f"cannot reach the database ({describe_failure(exc)}). Is Postgres running? "
        f"{_DOCTOR_HINT}"
    )


@contextmanager
def connect_or_exit(database_url: str) -> Iterator[psycopg.Connection[Any]]:
    """:func:`brain.db.connect`, but a failed connect is one red line and exit 1 (QA F2).

    Only the connect is guarded. An error raised inside the ``with`` body propagates
    unchanged, because "cannot reach" would be a false diagnosis of it. A DSN that cannot
    be encoded (a lone surrogate) raises ``UnicodeEncodeError`` before libpq sees it, and
    is refused the same way.
    """
    try:
        conn = connect_raw(database_url)
    except (psycopg.Error, UnicodeError) as exc:  # a DSN that cannot be encoded fails pre-libpq
        typer.secho(unreachable_line(exc), fg="red", err=True)
        raise typer.Exit(code=1) from None
    try:
        yield conn
    finally:
        conn.close()


def _embed_failure_line(name: str, exc: EmbedError) -> str:
    return (
        f"embedding failed for {one_line(name)} ({describe_failure(exc)}); nothing was stored. "
        f"Is Ollama running? {_DOCTOR_HINT}"
    )


@contextmanager
def exit_on_refusal(name: str) -> Iterator[None]:
    """A single-document ingest's refusals: one red line on stderr, exit 1.

    - **SecretGuardError.** The refusal is the whole outcome. Nothing was written: the
      guard raises before the content hash and the write transaction.
    - **SensitivityError.** An invalid ``--sensitivity`` is a usage error, raised before
      any write for exactly that reason. A user who mistyped the level must not end up
      with a document they believe is protected.
    - **EmbedError (QA F1).** The embedder failed. Nothing was stored, because the
      pipeline embeds before it opens its write transaction (perf-T3).
    """
    try:
        yield
    except (SecretGuardError, SensitivityError) as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(code=1) from e
    except EmbedError as e:
        typer.secho(_embed_failure_line(name, e), fg="red", err=True)
        raise typer.Exit(code=1) from None


def file_failure_stops_walk(name: str, exc: Exception, *, index: int, total: int) -> bool:
    """Report ``ingest-dir``'s failure on one file; True when the walk must stop.

    The failure line names the error **class only** (review rev-137 #6). A parser's
    message can quote the file's bytes, and a psycopg DETAIL can quote row values.

    The walk stops only on a **transient** :class:`EmbedError` (QA F1): a timeout, a
    dropped connection, HTTP 408/429/5xx. The embedder is a run-wide dependency, so
    every file after this one would wait out the full timeout and fail the same way.
    Stopping loses nothing: the failed file stored nothing, and the walk is idempotent,
    so a re-run skips what landed and resumes here.

    Every other failure is plausibly file-specific, so the walk goes on. That includes a
    permanent embed error, which also fails fast. Stopping on it would block every re-run
    at the same file.
    """
    typer.secho(f"  failed: {one_line(name)} ({describe_failure(exc)})", fg="red")
    if not (isinstance(exc, EmbedError) and exc.transient):
        return False
    typer.secho(
        f"ingest-dir: stopped at file {index} of {total}, the embedder is not answering "
        f"({describe_failure(exc)}); nothing was stored for {one_line(name)}. "
        f"Check Ollama ({_DOCTOR_HINT.lower()}), then re-run: files already ingested are "
        "skipped.",
        fg="red",
        err=True,
    )
    return True


class MetadataValueError(BrainError):
    """A ``--metadata`` value that parses as JSON but that Postgres would refuse."""


def _check_storable(value: Any) -> None:
    """Refuse what ``jsonb`` / ``text`` refuse: a non-finite float (``NaN``, ``Infinity``,
    ``1e400``), a NUL, or a string that is not UTF-8, i.e. a lone surrogate (rev-137 #2).

    Iterative, not recursive: a nesting depth ``json.loads`` accepted must not be the
    one that overflows this walk's stack.
    """
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise MetadataValueError("a number is not finite (out of range)")
        if isinstance(item, str) and "\x00" in item:
            raise MetadataValueError("a string contains a NUL character")
        if isinstance(item, str) and not encodes_utf8(item):
            raise MetadataValueError("a string is not valid UTF-8 (a lone surrogate)")
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _metadata_object(raw: str) -> dict[str, Any]:
    """Parse ``--metadata``; every refusal is a ``BadParameter`` (exit 2), never a traceback.

    ``JSONDecodeError`` is caught first, because it is a ``ValueError`` and keeps its
    position. Any other ``ValueError`` (an integer literal past Python's digit limit) and
    a ``RecursionError`` (deep nesting) are named by class only.
    """
    try:
        parsed = json.loads(raw)
        _check_storable(parsed)
    except json.JSONDecodeError as e:
        reason = f"not valid JSON: {e.msg} (line {e.lineno}, column {e.colno})"
    except MetadataValueError as e:
        reason = str(e)
    except (ValueError, RecursionError) as e:
        reason = f"not valid JSON ({type(e).__name__})"
    else:
        if isinstance(parsed, dict):
            return parsed
        reason = f"must be a JSON object, got {'null' if parsed is None else type(parsed).__name__}"
    raise typer.BadParameter(reason, param_hint="--metadata")


def stdin_metadata_or_exit(metadata: str | None, date: str | None) -> dict[str, Any]:
    """``ingest-stdin``'s ``--metadata`` merged with ``--date``, or a usage error, exit 2.

    QA F3: ``--metadata`` must be a JSON object. QA F4: ``--date`` must parse with the
    pipeline's own :func:`~brain.ingest.sent_at.parse_sent_at`, so it is accepted
    exactly when it will be promoted to ``sent_at``. Before this check, a bad date was
    stored with only a warning. The raw value is never echoed back.
    """
    meta = _metadata_object(metadata) if metadata else {}
    if date:
        if parse_sent_at(date) is None:
            raise typer.BadParameter(
                "not a date (use ISO 8601, e.g. 2026-11-11, or RFC 2822)",
                param_hint="--date",
            )
        meta.setdefault("date", date)
    return meta
