"""Guard: migration 025 applies cleanly no matter when it lands.

Wave 2 ships migration 025 while sibling worktrees are open, each of which may
add its own numbered file. The risk this module exists to kill is the ordering
one: a database that already carries every OTHER migration must still accept
025 afterwards and end up with a schema identical to one that applied the whole
set in numeric order.

Both tests carry ``@pytest.mark.fresh_schema`` — they mutate the schema itself,
which the TRUNCATE-only per-test reset cannot undo.
"""
from __future__ import annotations

import re
from typing import Any

import psycopg
import pytest

from brain.db import migrations_dir, run_migrations

_MIGRATION_025 = migrations_dir() / "025_documents_updated_at.sql"

SchemaFingerprint = tuple[tuple[tuple[Any, ...], ...], tuple[tuple[Any, ...], ...]]


#: Every migration filename must open with exactly three digits and an
#: underscore. Uniform width is the precondition that makes a lexicographic
#: sort equal a numeric one.
_PADDED_PREFIX_RE = re.compile(r"^\d{3}_")


def _numeric_prefix(name: str) -> int:
    """The integer prefix of a migration filename (``026_foo.sql`` -> ``26``)."""
    return int(name.split("_", 1)[0])


def _documents_fingerprint(conn: psycopg.Connection[Any]) -> SchemaFingerprint:
    """Every column definition + index definition on ``documents``.

    Two schemas with the same fingerprint are indistinguishable to any query
    this codebase issues, which is what "identical schema" needs to mean here.
    """
    columns = conn.execute(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = 'documents'
        ORDER BY column_name
        """
    ).fetchall()
    indexes = conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE tablename = 'documents' ORDER BY indexname"
    ).fetchall()
    return (
        tuple(tuple(r) for r in columns),
        tuple(tuple(r) for r in indexes),
    )


@pytest.mark.fresh_schema
def test_full_migration_set_applies_and_highest_number_is_head(
    test_db: psycopg.Connection[Any],
) -> None:
    """In-order application: every packaged .sql is recorded, highest number last.

    This assertion used to name ``025_documents_updated_at.sql`` literally, as a
    deliberate tripwire: a wave adding 026+ had to update it consciously rather
    than meet the break in production. It fired exactly as designed when 026
    landed.

    It is now generalized, for two reasons. The narrow one is churn — w2b's 027
    would trip it again, and an assertion edited once per wave stops being read
    and starts being rubber-stamped, which is how a tripwire dies. The better one
    is that naming a file pinned the wrong invariant. What actually matters is
    that ``run_migrations``' ``sorted(glob("*.sql"))`` — a LEXICOGRAPHIC sort —
    agrees with the numeric intent of the filenames.

    Lexicographic and numeric order coincide only while every prefix has the
    SAME digit width. This repo pads to three digits, so they agree across the
    whole ``001``–``999`` range (note ``'026_y.sql' < '100_x.sql'`` because
    ``'0' < '1'`` — there is nothing wrong at 100). Two things break the
    correspondence:

    - **A width change.** At migration 1000, ``'1000_x.sql' < '999_y.sql'``
      because ``'1' < '9'``, so the newest migration would be applied FIRST.
      Distant, but the invariant costs nothing to pin.
    - **An un-padded filename, which is a live risk today.** Adding
      ``26_foo.sql`` instead of ``026_foo.sql`` sorts it AFTER ``026_...``
      (``'0' < '2'``), silently reordering the set on the next fresh install.

    Both are caught below by asserting the property rather than a fact, so no
    edit is needed per wave.
    """
    # Arrange / Act — the fresh_schema fixture already ran the full set.
    expected = sorted(p.name for p in migrations_dir().glob("*.sql"))

    # Assert — everything packaged is recorded, in lexicographic order.
    rows = test_db.execute(
        "SELECT name FROM schema_migrations ORDER BY name"
    ).fetchall()
    applied = [str(r[0]) for r in rows]
    assert applied == expected

    # Uniform 3-digit padding is what MAKES lexicographic order equal numeric
    # order. Asserting it turns the width-change failure into an impossible
    # state rather than a detected one, and it catches an un-padded typo
    # (`26_foo.sql`) at the moment it is added rather than on the next fresh
    # install.
    unpadded = [name for name in applied if not _PADDED_PREFIX_RE.match(name)]
    assert not unpadded, (
        f"migration filenames must start with exactly three digits and an "
        f"underscore; got {unpadded}. Un-padded prefixes sort out of numeric "
        f"order ('026_a.sql' < '26_b.sql'), which silently reorders the set."
    )

    # THE ORDERING INVARIANT: lexicographic order == numeric order. This is what
    # `run_migrations`' sorted(glob(...)) relies on.
    numbers = [_numeric_prefix(name) for name in applied]
    assert numbers == sorted(numbers), (
        f"migration filenames must sort lexicographically in numeric order; "
        f"got {numbers}. A prefix-width change (the 999 -> 1000 rollover, or an "
        f"un-padded prefix) breaks this and applies migrations out of order."
    )

    # ...and the head really is the highest-numbered migration.
    assert applied[-1] == max(applied, key=_numeric_prefix)

    # Re-running the whole set changes nothing.
    assert run_migrations(test_db) == []


@pytest.mark.fresh_schema
def test_025_applies_cleanly_when_it_lands_last(
    test_db: psycopg.Connection[Any],
) -> None:
    """Every other migration first, then 025 — same schema, no errors.

    This is the concrete guard against "migrations land in several worktrees":
    whichever order the files reach a given database, the result must be the
    same schema.
    """
    # Arrange — snapshot the in-order result, then rewind 025 alone so the
    # database looks like "everything except 025 has been applied".
    in_order = _documents_fingerprint(test_db)
    test_db.execute("DROP INDEX IF EXISTS idx_documents_updated_at")
    test_db.execute("ALTER TABLE documents DROP COLUMN IF EXISTS updated_at")
    test_db.execute(
        "DELETE FROM schema_migrations WHERE name = %s", (_MIGRATION_025.name,)
    )
    before = test_db.execute("SELECT count(*) FROM schema_migrations").fetchone()
    assert before is not None

    # Act
    applied = run_migrations(test_db)

    # Assert — exactly one migration ran, and the schema reconverged.
    assert applied == [_MIGRATION_025.name]
    assert _documents_fingerprint(test_db) == in_order
    after = test_db.execute("SELECT count(*) FROM schema_migrations").fetchone()
    assert after is not None and after[0] == before[0] + 1
