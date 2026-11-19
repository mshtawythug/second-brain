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
and database, and an Ollama message can quote a response body. A DSN is reduced to
``host:port`` through :func:`brain.backup.pgtool.dsn_target`, which carries no password.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import typer

from .backup.pgtool import dsn_target
from .db import connect_raw
from .errors import EmbedError, SecretGuardError, SensitivityError, describe_failure
from .ingest.sent_at import parse_sent_at
from .ingest.text_safety import one_line

_DOCTOR_HINT = "Try: brain doctor"


def db_address(database_url: str) -> str:
    """``host:port`` of ``database_url``, or ``(unparseable)``. Never a credential."""
    try:
        return dsn_target(database_url).address
    except (psycopg.Error, ValueError):
        return "(unparseable)"


def unreachable_line(database_url: str, exc: BaseException) -> str:
    """The one line a failed connect prints: the address and the error class."""
    return (
        f"cannot reach DATABASE_URL {db_address(database_url)} ({describe_failure(exc)}). "
        f"Is Postgres running? {_DOCTOR_HINT}"
    )


@contextmanager
def connect_or_exit(database_url: str) -> Iterator[psycopg.Connection[Any]]:
    """:func:`brain.db.connect`, but a failed connect is one red line and exit 1 (QA F2).

    Only the connect is guarded. An error raised inside the ``with`` body propagates
    unchanged, because "cannot reach" would be a false diagnosis of it.
    """
    try:
        conn = connect_raw(database_url)
    except psycopg.Error as exc:
        typer.secho(unreachable_line(database_url, exc), fg="red", err=True)
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


def embed_failure_stops_walk(name: str, exc: EmbedError, *, index: int, total: int) -> bool:
    """Report ``ingest-dir``'s embed failure on one file; True when the walk must stop.

    The walk stops on a **transient** failure (a timeout, a dropped connection, HTTP
    408/429/5xx). The embedder is a run-wide dependency: every file after this one
    would wait out the full timeout and fail the same way. Stopping loses nothing. The
    failed file stored nothing, and the walk is idempotent, so a re-run skips what
    landed and resumes here.

    A **permanent** failure is plausibly file-specific and fails fast, so the walk goes
    on. Stopping on it would block every re-run at the same file.
    """
    typer.secho(f"  failed: {one_line(name)} ({describe_failure(exc)})", fg="red")
    if not exc.transient:
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


def _metadata_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise typer.BadParameter(
            f"not valid JSON: {e.msg} (line {e.lineno}, column {e.colno})",
            param_hint="--metadata",
        ) from None
    if not isinstance(parsed, dict):
        kind = "null" if parsed is None else type(parsed).__name__
        raise typer.BadParameter(
            f"must be a JSON object, got {kind}", param_hint="--metadata"
        )
    return parsed


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
