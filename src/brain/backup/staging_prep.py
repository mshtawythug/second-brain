"""Prepare a restore's staging database before ``pg_restore`` runs.

``brain backup`` dumps with ``--exclude-schema=ag_catalog`` (F3 §5.2: AGE's
graph cannot round-trip through ``pg_dump``), but the ``age`` EXTENSION entry is
still dumped. ``pg_restore`` replays it as ``CREATE EXTENSION IF NOT EXISTS age
WITH SCHEMA ag_catalog``, and on a fresh staging database that fails because the
schema does not exist, so with ``--exit-on-error`` every real restore of an AGE
brain aborted. Creating the extension first turns the dump's statement into the
no-op its ``IF NOT EXISTS`` intends.

Found while proving roadmap ticket RM-47's read-replica recipe end to end. Kept
out of :mod:`brain.backup.restore`, which is near its line budget (spec §2.2).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql

from ..errors import BackupError

#: Extensions whose dump entry names a schema the dump excludes.
STAGING_EXTENSIONS: tuple[str, ...] = ("age",)


def prepare_staging_extensions(
    conn: psycopg.Connection[Any], *, names: Sequence[str] = STAGING_EXTENSIONS
) -> tuple[str, ...]:
    """Create each extension in ``names`` that this server offers. Returns those ensured.

    An extension the server does not offer (a stock pgvector image has no
    ``age``) is skipped: its dump entry cannot exist either.
    """
    rows = conn.execute(
        "SELECT name FROM pg_available_extensions WHERE name = ANY(%s)", (list(names),)
    ).fetchall()
    offered = {str(row[0]) for row in rows}
    ensured = tuple(name for name in names if name in offered)
    try:
        for name in ensured:
            conn.execute(
                sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(name))
            )
        conn.commit()  # brain.db.connect never commits on close
    except psycopg.Error as exc:
        raise BackupError(
            "Could not prepare the staging database for pg_restore (CREATE EXTENSION "
            "failed). Nothing was changed; your database is untouched."
        ) from exc
    return ensured
