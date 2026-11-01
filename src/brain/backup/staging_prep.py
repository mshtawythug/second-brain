"""Prepare a restore's staging database before ``pg_restore`` runs.

``brain backup`` dumps with ``--exclude-schema=ag_catalog`` (F3 §5.2: AGE's
graph cannot round-trip through ``pg_dump``), but the ``age`` EXTENSION entry is
still dumped. ``pg_restore`` replays it as ``CREATE EXTENSION IF NOT EXISTS age
WITH SCHEMA ag_catalog``, and on a fresh staging database that fails because the
schema does not exist, so with ``--exit-on-error`` every real restore of an AGE
brain aborted. Creating the extension first turns the dump's statement into the
no-op its ``IF NOT EXISTS`` intends.

``CREATE EXTENSION age`` needs a superuser, so it runs only when the archive
itself carries the ``age`` EXTENSION entry, read from the dump's table of
contents (``pg_restore -l``) by :func:`staging_needs`. An archive without AGE,
restored onto an AGE-capable server, creates nothing here (sec-119 I4).

Found while proving roadmap ticket RM-47's read-replica recipe end to end. Kept
out of :mod:`brain.backup.restore`, which is near its line budget (spec §2.2).
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql

from ..errors import BackupError

#: Extensions whose dump entry names a schema the dump excludes.
STAGING_EXTENSIONS: tuple[str, ...] = ("age",)

#: One ``pg_restore -l`` line for an EXTENSION entry: ``<id>; <oid> <oid> EXTENSION - <name>``.
#: Anchored, so ``COMMENT - EXTENSION age``, a table named ``age`` and a commented-out
#: (``;``) line are not mistaken for one.
_TOC_EXTENSION = re.compile(r"^\d+;\s+\d+\s+\d+\s+EXTENSION\s+-\s+(\S+)\s*$")


def extensions_in_toc(toc: str) -> frozenset[str]:
    """The extension names a ``pg_restore -l`` listing says the dump creates."""
    found = (_TOC_EXTENSION.match(line) for line in toc.splitlines())
    return frozenset(match.group(1) for match in found if match is not None)


def staging_needs(toc: str) -> tuple[str, ...]:
    """The :data:`STAGING_EXTENSIONS` this dump carries, in that order."""
    present = extensions_in_toc(toc)
    return tuple(name for name in STAGING_EXTENSIONS if name in present)


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
