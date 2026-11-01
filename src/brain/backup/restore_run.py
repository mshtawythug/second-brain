"""Run ``pg_restore`` into a restore's staging database: list the TOC, prepare, restore.

Moved here from :mod:`brain.backup.restore` (roadmap RM-47, which had spent most
of that module's line budget) when the restore gained a step: before replaying
the dump it reads the dump's table of contents (``pg_restore -l``, no database
connection) and creates in staging only the extensions the archive itself
carries and the dump cannot replay on its own (:mod:`brain.backup.staging_prep`).
Reading the TOC first keeps an archive without AGE from newly needing a
superuser on an AGE-capable server (sec-119 I4).

The ``pg_restore`` invocation itself is unchanged: ``--no-owner``,
``--no-privileges`` and ``--exit-on-error``, run through the same tool plan.
"""
from __future__ import annotations

import contextlib
import subprocess
import uuid
from pathlib import Path

import psycopg

from ..db import connect
from .create import run_tool
from .pgtool import RESTORE_TIMEOUT_S, CommandRunner, PgToolPlan, password_env_file
from .staging_prep import prepare_staging_extensions, staging_needs


def _prepare_from_toc(
    plan: PgToolPlan,
    parts: dict[str, str],
    dump_arg: str,
    runner: CommandRunner,
) -> None:
    """Read the dump's TOC and create in staging what the archive needs first."""
    toc = run_tool(
        runner,
        plan.argv("-l", dump_arg),
        timeout=RESTORE_TIMEOUT_S,
        env=plan.env(None) if plan.source != "container" else None,
        what="pg_restore -l (reading the dump's contents)",
    ).stdout
    needed = staging_needs(toc or "")
    if needed:
        with connect(psycopg.conninfo.make_conninfo(**parts)) as staged:
            prepare_staging_extensions(staged, names=needed)


def restore_into(
    plan: PgToolPlan,
    parts: dict[str, str],
    dump_path: Path,
    target_db: str,
    runner: CommandRunner,
) -> None:
    """``pg_restore`` the dump into ``target_db`` with ``--exit-on-error``.

    ``--exit-on-error`` is the whole reason for the custom dump format: a
    half-applied restore fails loudly instead of printing errors and exiting 0.
    ``parts`` names ``target_db``, the staging database, never the live one.
    """
    restore_args = [
        "-U",
        parts.get("user", "brain"),
        "-d",
        target_db,
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
    ]
    password = parts.get("password")

    if plan.source != "container":
        _prepare_from_toc(plan, parts, str(dump_path), runner)
        run_tool(
            runner,
            plan.argv(
                *restore_args,
                "-h",
                parts.get("host", "localhost"),
                "-p",
                parts.get("port", "5432"),
                str(dump_path),
            ),
            timeout=RESTORE_TIMEOUT_S,
            env=plan.env(password),
            what="pg_restore",
        )
        return

    remote = f"/tmp/brain-restore-{uuid.uuid4().hex}.dump"  # noqa: S108 — in-container
    try:
        run_tool(
            runner,
            ["docker", "cp", str(dump_path), f"{plan.container}:{remote}"],
            timeout=RESTORE_TIMEOUT_S,
            what="docker cp of the dump",
        )
        _prepare_from_toc(plan, parts, remote, runner)
        with password_env_file(password) as env_file:
            run_tool(
                runner,
                plan.argv(*restore_args, remote, env_file=env_file),
                timeout=RESTORE_TIMEOUT_S,
                what="pg_restore",
            )
    finally:
        # Best-effort cleanup: a leftover temp file inside the container is
        # untidy, not dangerous, and must never mask the original failure.
        with contextlib.suppress(
            FileNotFoundError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ):
            runner.run(
                ["docker", "exec", str(plan.container), "rm", "-f", remote],
                timeout=RESTORE_TIMEOUT_S,
            )
