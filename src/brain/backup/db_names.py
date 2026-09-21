"""Database-identifier validation and the byte budget `brain restore` derives from."""
from __future__ import annotations

import re
from datetime import UTC, datetime

from ..errors import BackupError
from .create import TIMESTAMP_FORMAT

#: Generated database names are additionally regex-checked before being wrapped
#: in `sql.Identifier` — belt and braces, mirroring how `brain analyze`
#: validates against `list_public_tables` before quoting.
_DB_NAME_RE = re.compile(r"^[a-z0-9_]+$")

#: Postgres truncates identifiers at ``NAMEDATALEN - 1`` = 63 BYTES, and does
#: it SILENTLY: the over-long name is accepted, cut server-side, and every
#: later reference to the name Python still holds fails with
#: `database "..." does not exist` — pointing at nothing.
PG_IDENTIFIER_MAX_BYTES = 63

#: The longest name a restore DERIVES from the live database name.
#: :func:`brain.backup.restore._restore_database` builds both
#: ``{live}_restore_{suffix}`` and ``{live}_replaced_{suffix}``, where
#: ``suffix`` is a ``TIMESTAMP_FORMAT`` stamp with its dash swapped for an
#: underscore (same length either way). ``_replaced_`` is the binding one.
#: Spelled as an expression over the real format rather than a literal, so it
#: re-derives if either half moves.
RESTORE_DERIVED_SUFFIX_BYTES = len("_replaced_") + len(
    datetime(2026, 1, 1, tzinfo=UTC).strftime(TIMESTAMP_FORMAT)
)

#: Longest live database name `brain restore` can carry through a swap.
MAX_RESTORABLE_DB_NAME_BYTES = PG_IDENTIFIER_MAX_BYTES - RESTORE_DERIVED_SUFFIX_BYTES


def _validated_db_name(name: str) -> str:
    if not _DB_NAME_RE.match(name):
        raise BackupError(
            f"refusing to use {name!r} as a database name: expected only "
            "lowercase letters, digits and underscores"
        )
    return name


def _validated_restorable_db_name(name: str) -> str:
    """Character class AND length — the live name a restore derives from.

    :func:`_validated_db_name` checks the character class only. That is enough
    for a name Postgres will merely quote, and NOT enough for this one: a
    restore appends ``_replaced_<stamp>`` to it, and if the result crosses
    :data:`PG_IDENTIFIER_MAX_BYTES` Postgres truncates it without saying so.
    The failure then surfaces much later, as `database "..." does not exist`
    for a name that is right there in the command — the length never appears in
    the error at all. Refusing here turns that into one actionable sentence.
    """
    validated = _validated_db_name(name)
    # BYTES, not characters: the limit is NAMEDATALEN-1 bytes. `_DB_NAME_RE`
    # admits only ASCII today, so the two are equal — this is written for the
    # byte limit anyway so that widening that character class cannot quietly
    # turn a byte budget into a character one.
    size = len(validated.encode("utf-8"))
    if size > MAX_RESTORABLE_DB_NAME_BYTES:
        raise BackupError(
            f"database name {validated!r} is {size} bytes, which is "
            f"{size - MAX_RESTORABLE_DB_NAME_BYTES} over what `brain restore` "
            f"can handle. A restore derives "
            f"'{validated}_replaced_<stamp>' from it "
            f"(+{RESTORE_DERIVED_SUFFIX_BYTES} bytes), and Postgres truncates "
            f"identifiers at {PG_IDENTIFIER_MAX_BYTES} bytes SILENTLY — so "
            f"this would otherwise surface later as `database \"...\" does "
            f"not exist` with nothing pointing at the length. The budget for "
            f"the database name itself is "
            f"{MAX_RESTORABLE_DB_NAME_BYTES} bytes."
        )
    return validated
