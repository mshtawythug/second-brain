"""Canonical per-document sensitivity tier — the trust-boundary model (F6)."""
from __future__ import annotations

from typing import Literal, get_args

from brain.errors import SensitivityError

# ---------------------------------------------------------------------------
# Why this module exists at all, rather than a bare string in ingest/queries.
#
# ``documents.sensitivity`` is constrained in TWO places that cannot see each
# other: a named SQL CHECK in migration 026, and the Python literal set below.
# Migration 024 exists precisely because that shape drifted once already --
# ``interactions.py`` mirrored a SQL CHECK in a Python frozenset, the Python
# mirror raised BEFORE the INSERT was attempted, and so fixing only the SQL
# looked correct while the Python side still refused the new value.
#
# The lesson applied here: there is exactly ONE Python definition of the level
# set (:data:`VALID_SENSITIVITY_LEVELS`, derived from the ``Literal`` via
# ``get_args`` so the type and the runtime set cannot disagree), and
# ``tests/test_migration_026_sensitivity.py`` reads ``pg_get_constraintdef`` off
# the live constraint named by :data:`SENSITIVITY_CHECK_CONSTRAINT` and asserts
# it covers exactly this set. Add a level and BOTH the migration (a new one --
# never an edit to 026) and this literal must change or the suite goes red.
#
# TWO LEVELS BY DESIGN (spec F4-F6 section 5.6). Three levels
# (public/internal/secret) implies a lattice, and a lattice needs comparison
# operators, per-boundary thresholds, and a policy language -- none of which a
# single-user local knowledge base has any use for. Each boundary asks exactly
# one question ("may this body leave?"), so exactly one bit is needed. The
# column is TEXT rather than BOOLEAN so a future third level is a named-CHECK
# swap in a later migration instead of a type change, and the values read
# correctly in frontmatter and JSON.
# ---------------------------------------------------------------------------

SensitivityLevel = Literal["normal", "confidential"]
"""The two values ``documents.sensitivity`` accepts.

Kept in lockstep with migration 026's ``documents_sensitivity_check`` by
``tests/test_migration_026_sensitivity.py``.
"""

VALID_SENSITIVITY_LEVELS: frozenset[str] = frozenset(get_args(SensitivityLevel))

#: The default for every existing and every new row. Equal to the column
#: DEFAULT in migration 026, which is what makes the migration a behavioural
#: no-op: pre-026 rows all read ``normal`` and no boundary refuses anything.
DEFAULT_SENSITIVITY: SensitivityLevel = "normal"

#: The level that engages all three egress boundaries.
CONFIDENTIAL: SensitivityLevel = "confidential"

#: Name of the SQL CHECK constraint migration 026 installs. Named (not
#: anonymous) so a future third level is a DROP/ADD of a KNOWN name in a later
#: migration. The lockstep test resolves the live definition through it.
SENSITIVITY_CHECK_CONSTRAINT = "documents_sensitivity_check"

#: Name of the partial index migration 026 installs, mirroring
#: ``idx_documents_draft`` from migration 007.
SENSITIVITY_INDEX = "idx_documents_sensitivity"


def is_confidential(level: str | None) -> bool:
    """True iff ``level`` engages the egress boundaries.

    ``None`` (a projection that did not select the column) and every
    unrecognized value read as NOT confidential. That direction is deliberate
    for a *read* helper: this function is called on rows already in the
    database, where the CHECK constraint has already guaranteed validity, so an
    unexpected value means a projection bug rather than untrusted input -- and
    failing open on a read matches the pre-026 behaviour of every caller.
    Validation of *incoming* values is :func:`normalize_level`'s job, and it
    fails closed instead.
    """
    return level == CONFIDENTIAL


def normalize_level(value: str | None, *, strict: bool = True) -> SensitivityLevel:
    """Validate an incoming sensitivity level.

    ``None`` and the empty string mean "unspecified" and resolve to
    :data:`DEFAULT_SENSITIVITY` under both modes -- an omitted CLI flag is not
    an error.

    ``strict=True`` (the default, used by the CLI and the ingest pipeline)
    raises :class:`~brain.errors.SensitivityError` on anything else, so a typo
    surfaces at the boundary instead of silently downgrading a document the
    user meant to protect.

    ``strict=False`` COERCES an unrecognized value to
    :data:`DEFAULT_SENSITIVITY` and returns it. That mode exists for exactly
    one caller shape: reading a value a human hand-typed into a vault note's
    YAML during ``brain vault sync``, where one bad note must not abort a pass
    over the whole corpus. Callers using it are expected to log a WARNING --
    this function deliberately does no logging so it stays pure and testable.

    Note the asymmetry with :func:`is_confidential`: coercion always lands on
    ``normal``, never on ``confidential``. A typo can therefore cost protection
    the user intended but can never fabricate protection they did not -- and
    under ``strict`` (every path where the user is present to be told) it costs
    nothing at all, because it raises.
    """
    if value is None or value == "":
        return DEFAULT_SENSITIVITY
    if value in VALID_SENSITIVITY_LEVELS:
        # Narrow str -> SensitivityLevel. The membership test above is the
        # runtime proof; mypy cannot infer it from a frozenset[str].
        return value  # type: ignore[return-value]
    if strict:
        raise SensitivityError(
            f"sensitivity must be one of "
            f"{'/'.join(sorted(VALID_SENSITIVITY_LEVELS))} (got {value!r})"
        )
    return DEFAULT_SENSITIVITY
